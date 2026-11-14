"""质控区间分析测试：判定语义、快照一致性、并发不阻塞、重投不重复计算。"""
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, qc
from app.collector import collect, make_mailbox_key
from app.main import app
from imap_server import mailbox
from imap_server import seed as seed_mod
from imap_server.server import ThreadedImapServer
from tests.conftest import PASSWORD, USER

QC = "QC-Pb"
ITEM = "铅Pb"
UNIT = "mg/kg"
LOW, HIGH = 0.010, 0.050


def _append_mail(root, message_id, rows):
    mail = mailbox.build_message(
        message_id=message_id,
        subject=f"测试邮件 {message_id}",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[("batch.csv", seed_mod.csv_text(rows))],
    )
    return mailbox.append_message(root, "INBOX", mail)


@pytest.fixture()
def qc_env(tmp_path, monkeypatch):
    """空邮箱 + 独立 IMAP 服务 + 独立数据库。"""
    root = tmp_path / "mail"
    mailbox.ensure_folder(str(root), "INBOX")
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("COLLECTOR_DB", db_path)
    server = ThreadedImapServer(("127.0.0.1", 0), str(root), USER, PASSWORD)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    time.sleep(0.05)
    yield SimpleNamespace(root=str(root), host=host, port=port, db_path=db_path)
    server.shutdown()
    server.server_close()


def _collect(env):
    return collect(
        host=env.host,
        port=env.port,
        folder="INBOX",
        username=USER,
        password=PASSWORD,
        use_ssl=False,
    )


def _mailbox_key(env):
    return make_mailbox_key(env.host, env.port, "INBOX")


def _analyze(env, **overrides):
    params = qc.QcParams(
        mailbox_key=_mailbox_key(env),
        qc_sample_no=QC,
        test_item=ITEM,
        unit=UNIT,
        lower_limit=LOW,
        upper_limit=HIGH,
        **overrides,
    )
    conn = db.connect(env.db_path)
    try:
        conn.execute("BEGIN")
        mails = qc.load_timeline(conn, params.mailbox_key, params.test_item)
        report = qc.analyze(mails, params)
        conn.rollback()
    finally:
        conn.close()
    return report


def _verdicts(report):
    return {r["sample_no"]: r["verdict"] for r in report["results"]}


def _by_sample(report, sample_no):
    return next(r for r in report["results"] if r["sample_no"] == sample_no)


# ---------------------------------------------------------------------------
# 判定语义
# ---------------------------------------------------------------------------


def test_closed_interval_results_are_usable(qc_env):
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", UNIT)])
    _append_mail(qc_env.root, "m2", [("S-2", ITEM, "0.033", UNIT)])
    _append_mail(qc_env.root, "m3", [(QC, ITEM, "0.025", UNIT)])
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)
    assert _verdicts(report) == {"S-1": "usable", "S-2": "usable"}
    assert report["summary"]["usable_count"] == 2
    interval = report["intervals"][0]
    assert interval["status"] == "closed_ok"
    assert interval["opened_by"]["message_id"] == "m1"
    assert interval["closed_by"]["message_id"] == "m3"
    assert len(interval["result_ids"]) == 2


def test_out_of_limits_retroactive_retest_until_recovery(qc_env):
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", UNIT)])
    _append_mail(qc_env.root, "m2", [(QC, ITEM, "0.080", UNIT)])  # 超限
    _append_mail(qc_env.root, "m3", [("S-2", ITEM, "0.029", UNIT)])  # 异常后恢复前
    _append_mail(qc_env.root, "m4", [(QC, ITEM, "0.022", UNIT), ("S-3", ITEM, "0.030", UNIT)])
    _append_mail(qc_env.root, "m5", [(QC, ITEM, "0.024", UNIT)])
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)
    verdicts = _verdicts(report)
    assert verdicts == {"S-1": "pending_retest", "S-2": "pending_retest", "S-3": "usable"}

    s1 = _by_sample(report, "S-1")
    assert s1["reason"] == "interval_invalidated_by_anomaly"
    assert s1["trigger"]["message_id"] == "m2"  # 触发邮件
    s2 = _by_sample(report, "S-2")
    assert s2["reason"] == "after_anomaly_before_recovery"
    assert s2["trigger"]["message_id"] == "m2"

    anomaly = report["anomalies"][0]
    assert anomaly["type"] == "out_of_limits"
    assert anomaly["message_id"] == "m2"
    assert anomaly["affected_result_ids"] == [s1["result_id"]]  # 追溯结果 ID

    # 恢复后区间闭合 -> 可用；事件顺序：pass, out_of_limits, pass, pass
    assert [e["verdict"] for e in report["qc_events"]] == [
        "pass", "out_of_limits", "pass", "pass",
    ]


def test_non_numeric_qc_is_anomaly(qc_env):
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT)])
    _append_mail(qc_env.root, "m2", [("S-1", ITEM, "0.031", UNIT)])
    _append_mail(qc_env.root, "m3", [(QC, ITEM, "ND", UNIT)])  # 非数值
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)
    assert report["anomalies"][0]["type"] == "non_numeric"
    assert _by_sample(report, "S-1")["verdict"] == "pending_retest"


def test_duplicate_qc_in_same_mail_is_anomaly(qc_env):
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", UNIT)])
    _append_mail(
        qc_env.root, "m2",
        [(QC, ITEM, "0.023", UNIT), (QC, ITEM, "0.024", UNIT)],  # 同封重复质控
    )
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)
    anomaly = report["anomalies"][0]
    assert anomaly["type"] == "duplicate_in_mail"
    assert len(anomaly["qc_result_ids"]) == 2
    assert [e["verdict"] for e in report["qc_events"]] == [
        "pass", "duplicate_in_mail", "duplicate_in_mail",
    ]
    assert _by_sample(report, "S-1")["verdict"] == "pending_retest"


def test_unclosed_interval_is_pending_review(qc_env):
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", UNIT)])
    _append_mail(qc_env.root, "m2", [("S-2", ITEM, "0.033", UNIT)])
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)
    assert _verdicts(report) == {"S-1": "pending_review", "S-2": "pending_review"}
    assert report["intervals"][0]["status"] == "open"
    assert report["anomalies"] == []


def test_results_before_any_qc_are_pending_retest(qc_env):
    _append_mail(qc_env.root, "m1", [("S-1", ITEM, "0.031", UNIT)])
    _append_mail(qc_env.root, "m2", [(QC, ITEM, "0.020", UNIT), ("S-2", ITEM, "0.030", UNIT)])
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)
    s1 = _by_sample(report, "S-1")
    assert s1["verdict"] == "pending_retest"
    assert s1["reason"] == "no_preceding_qc"
    assert s1["trigger"] is None
    assert _by_sample(report, "S-2")["verdict"] == "pending_review"


def test_same_mail_qc_judged_before_samples(qc_env):
    """同封邮件先判质控：合格质控与样品同封时，样品归属新开启的区间。"""
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", UNIT)])
    _append_mail(qc_env.root, "m2", [(QC, ITEM, "0.025", UNIT)])
    assert _collect(qc_env).stopped is None
    report = _analyze(qc_env)
    assert _by_sample(report, "S-1")["verdict"] == "usable"


def test_unit_mismatch(qc_env):
    # 质控单位不符 -> 异常；样品单位不符 -> 即使区间闭合也降格待核实
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", "g/kg")])
    _append_mail(qc_env.root, "m2", [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", "g/kg")])
    _append_mail(qc_env.root, "m3", [(QC, ITEM, "0.025", UNIT)])
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)
    assert report["anomalies"][0]["type"] == "unit_mismatch"
    s1 = _by_sample(report, "S-1")
    assert s1["verdict"] == "pending_review"
    assert s1["reason"] == "unit_mismatch"


def test_other_test_items_are_ignored(qc_env):
    _append_mail(
        qc_env.root, "m1",
        [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", UNIT), ("S-1", "镉Cd", "0.008", UNIT)],
    )
    _append_mail(qc_env.root, "m2", [(QC, ITEM, "0.025", UNIT)])
    assert _collect(qc_env).stopped is None

    report = _analyze(qc_env)  # 只分析 铅Pb
    assert report["summary"]["sample_result_count"] == 1
    assert _by_sample(report, "S-1")["test_item"] == ITEM


# ---------------------------------------------------------------------------
# 重投 / UIDVALIDITY 重扫不重复计算
# ---------------------------------------------------------------------------


def test_redelivery_and_rebuild_do_not_double_count(qc_env):
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT), ("S-1", ITEM, "0.031", UNIT)])
    _append_mail(qc_env.root, "m2", [(QC, ITEM, "0.025", UNIT)])
    assert _collect(qc_env).stopped is None
    baseline = _analyze(qc_env)
    assert baseline["summary"]["usable_count"] == 1

    # 原样重投 m1 -> 跳过，不重复入库
    raw1 = mailbox.read_message(qc_env.root, "INBOX", 1)
    mailbox.append_message(qc_env.root, "INBOX", raw1)
    report = _collect(qc_env)
    assert report.stopped is None
    assert len(report.skipped_duplicates) == 1

    # 邮箱重建（UIDVALIDITY 改变、UID 重排）-> 整箱重扫，全部识别为重投
    with db.connect(qc_env.db_path) as conn:
        old_validity = db.get_state(conn, _mailbox_key(qc_env))["uidvalidity"]
    mailbox.reset_uidvalidity(qc_env.root, "INBOX", old_validity + 999)
    report = _collect(qc_env)
    assert report.stopped is None
    assert report.ingested == []
    assert len(report.skipped_duplicates) == 3

    after = _analyze(qc_env)
    assert after["summary"] == baseline["summary"]
    assert [r["result_id"] for r in after["results"]] == [
        r["result_id"] for r in baseline["results"]
    ]
    assert after["qc_events"] == baseline["qc_events"]


# ---------------------------------------------------------------------------
# 快照一致性与并发不阻塞（SQLite WAL 语义）
# ---------------------------------------------------------------------------


def _insert_delivery(conn, key, message_id, uid, records):
    with db.transaction(conn):
        db.insert_results(conn, key, message_id, uid, records)
        db.insert_delivery(conn, key, message_id, f"digest-{message_id}", uid, 1)
        db.upsert_state(conn, key, 1, uid)


def test_snapshot_isolation_within_one_transaction(tmp_path):
    """同一读事务内两次读取一致；并发写入在提交前/后对旧快照均不可见。"""
    path = str(tmp_path / "t.db")
    key = "imap|10143|INBOX"
    writer = db.connect(path)
    db.init_db(writer)
    _insert_delivery(writer, key, "m1", 1, [(QC, ITEM, "0.020", UNIT)])

    reader = db.connect(path)
    reader.execute("BEGIN")
    first = qc.load_timeline(reader, key, ITEM)

    # 读事务持有期间，另一连接写入并提交新邮件
    _insert_delivery(writer, key, "m2", 2, [("S-1", ITEM, "0.031", UNIT)])

    second = qc.load_timeline(reader, key, ITEM)
    assert first == second  # 同一快照：看不到新邮件
    assert sum(len(m["records"]) for m in first) == 1
    reader.rollback()

    third = qc.load_timeline(reader, key, ITEM)  # 新事务 -> 新快照
    assert sum(len(m["records"]) for m in third) == 2
    reader.close()
    writer.close()


def test_read_snapshot_does_not_block_writes(tmp_path):
    """长查询的读事务不阻塞新邮件入库（WAL：读写并发）。"""
    path = str(tmp_path / "t.db")
    key = "imap|10143|INBOX"
    writer = db.connect(path)
    db.init_db(writer)
    _insert_delivery(writer, key, "m1", 1, [(QC, ITEM, "0.020", UNIT)])

    reader = db.connect(path)
    reader.execute("BEGIN")
    assert qc.load_timeline(reader, key, ITEM)  # 读事务已持有快照

    writer.execute("PRAGMA busy_timeout = 1000")  # 若被阻塞会迅速报错
    _insert_delivery(writer, key, "m2", 2, [("S-1", ITEM, "0.031", UNIT)])  # 不阻塞

    reader.rollback()
    reader.close()
    writer.close()


def test_open_read_snapshot_does_not_block_collect(qc_env):
    """持有查询快照期间，/collect 仍能完成新邮件入库。"""
    _append_mail(qc_env.root, "m1", [(QC, ITEM, "0.020", UNIT)])
    assert _collect(qc_env).stopped is None

    read_conn = db.connect(qc_env.db_path)  # 模拟进行中的长区间查询
    read_conn.execute("BEGIN")
    qc.load_timeline(read_conn, _mailbox_key(qc_env), ITEM)

    _append_mail(qc_env.root, "m2", [("S-1", ITEM, "0.031", UNIT)])
    report = _collect(qc_env)  # 读事务持有期间入库不被阻塞
    assert report.stopped is None
    assert report.total_new_records == 1

    read_conn.rollback()
    read_conn.close()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


def test_qc_intervals_api_end_to_end(qc_env):
    """完整演示场景：异常（超限/非数值/同封重复）、恢复、未闭合区间。"""
    seed_mod.seed_qc(qc_env.root, "INBOX")
    assert _collect(qc_env).stopped is None

    client = TestClient(app)
    resp = client.get(
        "/qc/intervals",
        params={
            "mailbox_key": _mailbox_key(qc_env),
            "qc_sample_no": QC,
            "test_item": ITEM,
            "unit": UNIT,
            "lower_limit": LOW,
            "upper_limit": HIGH,
        },
    )
    assert resp.status_code == 200
    body = resp.json()

    summary = body["summary"]
    assert summary["usable_count"] == 2  # S-2001, S-2002
    assert summary["pending_retest_count"] == 3  # S-2003, S-2004, S-2005
    assert summary["pending_review_count"] == 2  # S-2006, S-2007
    assert summary["qc_anomaly_count"] == 3
    assert summary["qc_pass_count"] == 4
    assert summary["interval_status_counts"] == {
        "closed_ok": 1, "invalidated": 2, "open": 1,
    }

    assert [a["type"] for a in body["anomalies"]] == [
        "out_of_limits", "non_numeric", "duplicate_in_mail",
    ]
    # 触发邮件与追溯结果 ID 一并返回
    first = body["anomalies"][0]
    assert first["message_id"] == "qc-demo-05@lab-instrument"
    s2003 = next(r for r in body["results"] if r["sample_no"] == "S-2003")
    assert first["affected_result_ids"] == [s2003["result_id"]]
    assert s2003["trigger"]["message_id"] == "qc-demo-05@lab-instrument"

    # 快照边界与查询参数回显
    assert body["snapshot"]["mail_count"] == summary["mail_count"]
    assert body["query"]["qc_sample_no"] == QC


def test_qc_intervals_api_validation(qc_env):
    client = TestClient(app)
    base = {
        "mailbox_key": _mailbox_key(qc_env),
        "qc_sample_no": QC,
        "test_item": ITEM,
        "unit": UNIT,
        "lower_limit": LOW,
        "upper_limit": HIGH,
    }
    # 下限大于上限 -> 422
    resp = client.get("/qc/intervals", params={**base, "lower_limit": 0.9})
    assert resp.status_code == 422
    # 非有限边界 -> 422
    resp = client.get("/qc/intervals", params={**base, "upper_limit": "inf"})
    assert resp.status_code == 422
    # 缺参数 -> 422
    resp = client.get("/qc/intervals", params={"mailbox_key": "x"})
    assert resp.status_code == 422
    # 未知邮箱 -> 200 空结果
    resp = client.get("/qc/intervals", params=base)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["sample_result_count"] == 0
    assert body["results"] == []
