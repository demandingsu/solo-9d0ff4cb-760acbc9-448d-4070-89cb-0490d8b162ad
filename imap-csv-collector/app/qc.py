"""质控区间分析：按邮件首次入库顺序，用质控样读数圈定可交给统计脚本的样品结果。

判定规则（与质控员手工流程一致）：
  * 邮件按「首次入库顺序」（mail_delivery.id）排列；UIDVALIDITY 重扫、
    重复投递不会产生新的投递记录，因此不会重复参与分析。
  * 同一封邮件内先判质控样，再归属普通样品结果。
  * 质控异常：超限 / 非数值 / 同封重复质控 / 单位与指定单位不一致。
  * 异常使当前区间作废：自最近一次合格质控后的结果追溯标为「待复测」，
    直到下一次合格质控出现；异常之后、恢复之前的结果同样待复测。
  * 被前后两次合格质控夹住且期间无异常的结果 -> 「可用」。
  * 区间尚未闭合且未见异常的结果 -> 「待核实」。
  * 从未被合格质控开启区间覆盖的结果 -> 「待复测」。

快照与并发：
  analyze_snapshot 要求调用方已开启事务（BEGIN）。SQLite WAL 模式下
  读事务持有稳定快照，且不阻塞 /collect 的新邮件入库写入。
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

# 质控判定
QC_PASS = "pass"
QC_OUT_OF_LIMITS = "out_of_limits"
QC_NON_NUMERIC = "non_numeric"
QC_DUPLICATE_IN_MAIL = "duplicate_in_mail"
QC_UNIT_MISMATCH = "unit_mismatch"
QC_ANOMALY_VERDICTS = {
    QC_OUT_OF_LIMITS,
    QC_NON_NUMERIC,
    QC_DUPLICATE_IN_MAIL,
    QC_UNIT_MISMATCH,
}

# 样品结果判定
RESULT_USABLE = "usable"
RESULT_PENDING_RETEST = "pending_retest"
RESULT_PENDING_REVIEW = "pending_review"

# 样品结果原因码
REASON_BRACKETED = "bracketed_by_passing_qc"
REASON_INVALIDATED = "interval_invalidated_by_anomaly"
REASON_AFTER_ANOMALY = "after_anomaly_before_recovery"
REASON_NO_PRECEDING_QC = "no_preceding_qc"
REASON_INTERVAL_OPEN = "interval_open_no_anomaly"
REASON_UNIT_MISMATCH = "unit_mismatch"

_REASON_DETAIL = {
    REASON_BRACKETED: "被前后两次合格质控夹住且期间无异常，可交给统计脚本",
    REASON_INVALIDATED: "所在区间被异常质控作废，自最近一次合格质控起追溯待复测",
    REASON_AFTER_ANOMALY: "异常质控之后、下一次合格质控之前，待复测",
    REASON_NO_PRECEDING_QC: "此前没有任何合格质控，仪器状态无佐证，待复测",
    REASON_INTERVAL_OPEN: "区间尚未闭合（缺下一次合格质控），期间无异常，待核实",
    REASON_UNIT_MISMATCH: "结果单位与指定单位不一致，需人工核实",
}


def parse_number(text: str) -> float | None:
    """把结果文本解析为有限数值；非数值（ND、<0.01、空等）返回 None。"""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class QcParams:
    mailbox_key: str
    qc_sample_no: str
    test_item: str
    unit: str
    lower_limit: float
    upper_limit: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "mailbox_key": self.mailbox_key,
            "qc_sample_no": self.qc_sample_no,
            "test_item": self.test_item,
            "unit": self.unit,
            "lower_limit": self.lower_limit,
            "upper_limit": self.upper_limit,
        }


def load_timeline(
    conn: sqlite3.Connection, mailbox_key: str, test_item: str
) -> list[dict[str, Any]]:
    """取出指定邮箱、指定项目的邮件时间线（须在调用方的事务内执行）。

    邮件按首次入库顺序（mail_delivery.id）排列，邮件内记录按 test_result.id
    排列。重复投递 / UIDVALIDITY 重扫不会产生新的 mail_delivery 或
    test_result 行，天然不重复计算。
    """
    rows = conn.execute(
        """
        SELECT d.id            AS delivery_id,
               d.message_id    AS message_id,
               d.first_uid     AS first_uid,
               d.ingested_at   AS mail_ingested_at,
               r.id            AS result_id,
               r.sample_no     AS sample_no,
               r.test_item     AS test_item,
               r.result        AS result,
               r.unit          AS unit
        FROM mail_delivery d
        JOIN test_result r
          ON r.mailbox_key = d.mailbox_key
         AND r.message_id  = d.message_id
        WHERE d.mailbox_key = ?
          AND r.test_item   = ?
        ORDER BY d.id, r.id
        """,
        (mailbox_key, test_item),
    ).fetchall()

    mails: list[dict[str, Any]] = []
    for row in rows:
        if not mails or mails[-1]["delivery_id"] != row["delivery_id"]:
            mails.append(
                {
                    "delivery_id": row["delivery_id"],
                    "message_id": row["message_id"],
                    "uid": row["first_uid"],
                    "ingested_at": row["mail_ingested_at"],
                    "records": [],
                }
            )
        mails[-1]["records"].append(
            {
                "result_id": row["result_id"],
                "sample_no": row["sample_no"],
                "test_item": row["test_item"],
                "result": row["result"],
                "unit": row["unit"],
            }
        )
    return mails


def snapshot_meta(conn: sqlite3.Connection, mailbox_key: str) -> dict[str, Any]:
    """快照边界信息（与 load_timeline 同一事务执行，证明数据来自同一快照）。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS mail_count, COALESCE(MAX(id), 0) AS max_delivery_id
        FROM mail_delivery WHERE mailbox_key = ?
        """,
        (mailbox_key,),
    ).fetchone()
    return {
        "mailbox_key": mailbox_key,
        "mail_count": row["mail_count"],
        "max_delivery_id": row["max_delivery_id"],
    }


def _qc_verdict(
    record: dict[str, Any], params: QcParams
) -> tuple[str, float | None, str]:
    """判定单条质控记录，返回 (verdict, value, detail)。"""
    if record["unit"] != params.unit:
        return (
            QC_UNIT_MISMATCH,
            None,
            f"质控记录单位 {record['unit']!r} 与指定单位 {params.unit!r} 不一致，无法判定",
        )
    value = parse_number(record["result"])
    if value is None:
        return (
            QC_NON_NUMERIC,
            None,
            f"质控读数 {record['result']!r} 不是数值，无法判定",
        )
    if value < params.lower_limit or value > params.upper_limit:
        return (
            QC_OUT_OF_LIMITS,
            value,
            f"质控读数 {value} 超出区间 "
            f"[{params.lower_limit}, {params.upper_limit}]",
        )
    return QC_PASS, value, "在质控区间内"


def analyze(mails: list[dict[str, Any]], params: QcParams) -> dict[str, Any]:
    """纯函数：对邮件时间线做质控区间分析，返回结构化判定。"""
    qc_events: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    intervals: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    current: dict[str, Any] | None = None  # 当前开启的区间
    last_anomaly: dict[str, Any] | None = None  # 最近一次异常（触发邮件）

    def _mail_ref(mail: dict[str, Any], seq: int) -> dict[str, Any]:
        return {
            "message_id": mail["message_id"],
            "uid": mail["uid"],
            "delivery_id": mail["delivery_id"],
            "mail_seq": seq,
        }

    def _close_usable(interval: dict[str, Any], closing_event: dict[str, Any]) -> None:
        interval["status"] = "closed_ok"
        interval["closed_by"] = _event_ref(closing_event)
        for entry in interval["samples"]:
            entry["verdict"] = RESULT_USABLE
            entry["reason"] = REASON_BRACKETED
        intervals.append(interval)

    def _invalidate(
        interval: dict[str, Any], anomaly: dict[str, Any]
    ) -> list[int]:
        interval["status"] = "invalidated"
        interval["invalidated_by_anomaly"] = anomaly["index"]
        affected = []
        for entry in interval["samples"]:
            entry["verdict"] = RESULT_PENDING_RETEST
            entry["reason"] = REASON_INVALIDATED
            entry["trigger"] = anomaly["trigger_mail"]
            affected.append(entry["result_id"])
        intervals.append(interval)
        return affected

    def _event_ref(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "qc_result_id": event["qc_result_id"],
            "message_id": event["message_id"],
            "uid": event["uid"],
            "mail_seq": event["mail_seq"],
            "value": event["value"],
        }

    for seq, mail in enumerate(mails, start=1):
        records = mail["records"]
        qc_records = [r for r in records if r["sample_no"] == params.qc_sample_no]
        sample_records = [r for r in records if r["sample_no"] != params.qc_sample_no]

        # ---- 第一步：判质控 ----
        mail_anomaly: dict[str, Any] | None = None
        passing_event: dict[str, Any] | None = None

        if len(qc_records) > 1:
            event_ids = []
            for record in qc_records:
                event = {
                    "qc_result_id": record["result_id"],
                    "verdict": QC_DUPLICATE_IN_MAIL,
                    "value": parse_number(record["result"]),
                    "detail": "同一封邮件出现多条同项目质控记录，无法采信",
                    **_mail_ref(mail, seq),
                }
                qc_events.append(event)
                event_ids.append(record["result_id"])
            mail_anomaly = {
                "type": QC_DUPLICATE_IN_MAIL,
                "detail": f"同封邮件出现 {len(qc_records)} 条质控记录",
                "qc_result_ids": event_ids,
            }
        elif len(qc_records) == 1:
            record = qc_records[0]
            verdict, value, detail = _qc_verdict(record, params)
            event = {
                "qc_result_id": record["result_id"],
                "verdict": verdict,
                "value": value,
                "detail": detail,
                **_mail_ref(mail, seq),
            }
            qc_events.append(event)
            if verdict == QC_PASS:
                passing_event = event
            else:
                mail_anomaly = {
                    "type": verdict,
                    "detail": detail,
                    "qc_result_ids": [record["result_id"]],
                }

        # ---- 第二步：结算区间状态 ----
        if mail_anomaly is not None:
            anomaly = {
                "index": len(anomalies),
                "trigger_mail": _mail_ref(mail, seq),
                **mail_anomaly,
            }
            affected: list[int] = []
            if current is not None:
                affected = _invalidate(current, anomaly)
                current = None
            anomaly["affected_result_ids"] = affected
            anomalies.append(anomaly)
            last_anomaly = anomaly
        elif passing_event is not None:
            if current is not None:
                _close_usable(current, passing_event)
            current = {
                "index": len(intervals),
                "status": "open",
                "opened_by": _event_ref(passing_event),
                "closed_by": None,
                "invalidated_by_anomaly": None,
                "samples": [],
            }

        # ---- 第三步：归属普通样品结果 ----
        for record in sample_records:
            entry: dict[str, Any] = {
                "result_id": record["result_id"],
                "sample_no": record["sample_no"],
                "test_item": record["test_item"],
                "result": record["result"],
                "unit": record["unit"],
                "unit_matched": record["unit"] == params.unit,
                "interval_index": None,
                "verdict": None,
                "reason": None,
                "trigger": None,
                **_mail_ref(mail, seq),
            }
            if current is not None:
                entry["interval_index"] = current["index"]
                current["samples"].append(entry)
            else:
                entry["verdict"] = RESULT_PENDING_RETEST
                if last_anomaly is not None:
                    entry["reason"] = REASON_AFTER_ANOMALY
                    entry["trigger"] = last_anomaly["trigger_mail"]
                else:
                    entry["reason"] = REASON_NO_PRECEDING_QC
            results.append(entry)

    # 查询结束时仍未闭合的区间：期间无异常 -> 待核实
    if current is not None:
        for entry in current["samples"]:
            entry["verdict"] = RESULT_PENDING_REVIEW
            entry["reason"] = REASON_INTERVAL_OPEN
        intervals.append(current)

    # 样品单位与指定单位不一致：即使落在可用区间也降格为待核实
    for entry in results:
        if not entry["unit_matched"] and entry["verdict"] == RESULT_USABLE:
            entry["verdict"] = RESULT_PENDING_REVIEW
            entry["reason"] = REASON_UNIT_MISMATCH

    # ---- 汇总输出 ----
    interval_views = []
    for interval in intervals:
        interval_views.append(
            {
                "index": interval["index"],
                "status": interval["status"],
                "opened_by": interval["opened_by"],
                "closed_by": interval["closed_by"],
                "invalidated_by_anomaly": interval["invalidated_by_anomaly"],
                "result_ids": [e["result_id"] for e in interval["samples"]],
            }
        )

    anomaly_views = []
    for anomaly in anomalies:
        anomaly_views.append(
            {
                "index": anomaly["index"],
                "type": anomaly["type"],
                "detail": anomaly["detail"],
                "message_id": anomaly["trigger_mail"]["message_id"],
                "uid": anomaly["trigger_mail"]["uid"],
                "delivery_id": anomaly["trigger_mail"]["delivery_id"],
                "mail_seq": anomaly["trigger_mail"]["mail_seq"],
                "qc_result_ids": anomaly["qc_result_ids"],
                "affected_result_ids": anomaly["affected_result_ids"],
            }
        )

    result_views = []
    for entry in results:
        result_views.append(
            {
                "result_id": entry["result_id"],
                "sample_no": entry["sample_no"],
                "test_item": entry["test_item"],
                "result": entry["result"],
                "unit": entry["unit"],
                "message_id": entry["message_id"],
                "uid": entry["uid"],
                "mail_seq": entry["mail_seq"],
                "interval_index": entry["interval_index"],
                "verdict": entry["verdict"],
                "reason": entry["reason"],
                "reason_detail": _REASON_DETAIL.get(entry["reason"], ""),
                "trigger": entry["trigger"],
            }
        )

    verdict_counts = {
        RESULT_USABLE: 0,
        RESULT_PENDING_RETEST: 0,
        RESULT_PENDING_REVIEW: 0,
    }
    for entry in results:
        verdict_counts[entry["verdict"]] += 1

    summary = {
        "mail_count": len(mails),
        "qc_event_count": len(qc_events),
        "qc_pass_count": sum(1 for e in qc_events if e["verdict"] == QC_PASS),
        "qc_anomaly_count": len(anomalies),
        "sample_result_count": len(results),
        "usable_count": verdict_counts[RESULT_USABLE],
        "pending_retest_count": verdict_counts[RESULT_PENDING_RETEST],
        "pending_review_count": verdict_counts[RESULT_PENDING_REVIEW],
        "interval_status_counts": {
            status: sum(1 for i in intervals if i["status"] == status)
            for status in ("closed_ok", "invalidated", "open")
        },
    }

    return {
        "query": params.as_dict(),
        "summary": summary,
        "qc_events": qc_events,
        "anomalies": anomaly_views,
        "intervals": interval_views,
        "results": result_views,
    }
