"""演示用初始邮件：两封正常邮件 + 一封多附件坏 CSV 邮件。"""
from __future__ import annotations

import email

from . import mailbox

HEADER = "样品编号,检测项目,结果,单位"


def csv_text(rows: list[tuple[str, str, str, str]]) -> bytes:
    lines = [HEADER]
    lines.extend(",".join(row) for row in rows)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


BAD_CSV = (
    "样品编号,检测项目,结果,单位\r\n"
    "S-1002,铅Pb,0.03,mg/kg\r\n"
    "S-1002,镉Cd,,mg/kg\r\n"  # 结果为空 -> 整封判定坏附件
).encode("utf-8")


def seed(root: str, folder: str = "INBOX") -> None:
    if mailbox.folder_exists(root, folder) and mailbox.list_uids(root, folder):
        return
    mailbox.ensure_folder(root, folder)

    mail1 = mailbox.build_message(
        message_id="inst-0001@lab-instrument",
        subject="仪器结果 批次 B1",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[
            (
                "batch_b1.csv",
                csv_text(
                    [
                        ("S-1001", "铅Pb", "0.021", "mg/kg"),
                        ("S-1001", "镉Cd", "0.008", "mg/kg"),
                        ("S-1002", "铅Pb", "0.030", "mg/kg"),
                    ]
                ),
            )
        ],
    )
    mailbox.append_message(root, folder, mail1)

    mail2 = mailbox.build_message(
        message_id="inst-0002@lab-instrument",
        subject="仪器结果 批次 B2",
        sender="instrument-b@lab.local",
        recipient="lab@lab.local",
        attachments=[
            (
                "batch_b2.csv",
                csv_text(
                    [
                        ("S-1003", "汞Hg", "0.005", "mg/kg"),
                        ("S-1004", "砷As", "0.012", "mg/kg"),
                    ]
                ),
            )
        ],
    )
    mailbox.append_message(root, folder, mail2)

    # 第三封：两个附件，其中一个 CSV 缺字段 -> 整封失败、停在此邮件
    mail3 = mailbox.build_message(
        message_id="inst-0003@lab-instrument",
        subject="仪器结果 批次 B3（含损坏附件）",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[
            (
                "batch_b3_good.csv",
                csv_text(
                    [
                        ("S-1005", "铬Cr", "0.044", "mg/kg"),
                    ]
                ),
            ),
            ("batch_b3_bad.csv", BAD_CSV),
        ],
    )
    mailbox.append_message(root, folder, mail3)


# ---------------------------------------------------------------------------
# 质控区间演示邮件
#
# 检测项目 铅Pb、单位 mg/kg、质控样 QC-Pb，演示查询区间 [0.010, 0.050]。
# 时间线（按入库顺序）：
#   qc-demo-01  QC 0.020 合格，开启区间1；S-2001 铅Pb 归属区间1
#   qc-demo-02  S-2002 归属区间1
#   qc-demo-03  QC 0.025 合格 -> 区间1闭合（S-2001/S-2002 可用），开启区间2
#   qc-demo-04  S-2003 归属区间2
#   qc-demo-05  QC 0.080 超限 -> 区间2作废，S-2003 追溯待复测
#   qc-demo-06  S-2004 异常之后、恢复之前 -> 待复测
#   qc-demo-07  QC "ND" 非数值 -> 异常，无开启区间可污染
#   qc-demo-08  QC 0.022 合格（恢复）-> 开启区间3；S-2005 归属区间3
#   qc-demo-09  同封两条 QC -> 重复质控异常，区间3作废，S-2005 待复测
#   qc-demo-10  QC 0.021 合格（恢复）-> 开启区间4；S-2006 归属区间4
#   qc-demo-11  S-2007 归属区间4；区间4 未闭合 -> S-2006/S-2007 待核实
# ---------------------------------------------------------------------------

QC_DEMO_MAILS: list[tuple[str, str, list[tuple[str, str, str, str]]]] = [
    (
        "qc-demo-01@lab-instrument",
        "仪器结果 质控批次 Q1（合格质控开启）",
        [
            ("QC-Pb", "铅Pb", "0.020", "mg/kg"),
            ("S-2001", "铅Pb", "0.031", "mg/kg"),
            ("S-2001", "镉Cd", "0.008", "mg/kg"),  # 其他项目：演示同项目过滤
        ],
    ),
    (
        "qc-demo-02@lab-instrument",
        "仪器结果 质控批次 Q2",
        [("S-2002", "铅Pb", "0.033", "mg/kg")],
    ),
    (
        "qc-demo-03@lab-instrument",
        "仪器结果 质控批次 Q3（合格质控闭合区间1）",
        [("QC-Pb", "铅Pb", "0.025", "mg/kg")],
    ),
    (
        "qc-demo-04@lab-instrument",
        "仪器结果 质控批次 Q4",
        [("S-2003", "铅Pb", "0.028", "mg/kg")],
    ),
    (
        "qc-demo-05@lab-instrument",
        "仪器结果 质控批次 Q5（质控超限）",
        [("QC-Pb", "铅Pb", "0.080", "mg/kg")],
    ),
    (
        "qc-demo-06@lab-instrument",
        "仪器结果 质控批次 Q6（异常后未恢复）",
        [("S-2004", "铅Pb", "0.029", "mg/kg")],
    ),
    (
        "qc-demo-07@lab-instrument",
        "仪器结果 质控批次 Q7（质控非数值）",
        [("QC-Pb", "铅Pb", "ND", "mg/kg")],
    ),
    (
        "qc-demo-08@lab-instrument",
        "仪器结果 质控批次 Q8（合格质控恢复）",
        [
            ("QC-Pb", "铅Pb", "0.022", "mg/kg"),
            ("S-2005", "铅Pb", "0.030", "mg/kg"),
        ],
    ),
    (
        "qc-demo-09@lab-instrument",
        "仪器结果 质控批次 Q9（同封重复质控）",
        [
            ("QC-Pb", "铅Pb", "0.023", "mg/kg"),
            ("QC-Pb", "铅Pb", "0.024", "mg/kg"),
        ],
    ),
    (
        "qc-demo-10@lab-instrument",
        "仪器结果 质控批次 Q10（合格质控再次恢复）",
        [
            ("QC-Pb", "铅Pb", "0.021", "mg/kg"),
            ("S-2006", "铅Pb", "0.027", "mg/kg"),
        ],
    ),
    (
        "qc-demo-11@lab-instrument",
        "仪器结果 质控批次 Q11（区间未闭合）",
        [("S-2007", "铅Pb", "0.026", "mg/kg")],
    ),
]


def seed_qc(root: str, folder: str = "INBOX") -> list[int]:
    """向邮箱追加质控演示邮件（幂等：Message-ID 已存在则跳过）。

    返回新追加邮件的 UID 列表。
    """
    mailbox.ensure_folder(root, folder)
    existing_ids = set()
    for uid in mailbox.list_uids(root, folder):
        raw = mailbox.read_message(root, folder, uid) or b""
        message_id = (email.message_from_bytes(raw).get("Message-ID") or "")
        existing_ids.add(message_id.strip().strip("<>"))

    appended: list[int] = []
    for message_id, subject, rows in QC_DEMO_MAILS:
        if message_id in existing_ids:
            continue
        mail = mailbox.build_message(
            message_id=message_id,
            subject=subject,
            sender="instrument-a@lab.local",
            recipient="lab@lab.local",
            attachments=[("qc_batch.csv", csv_text(rows))],
        )
        appended.append(mailbox.append_message(root, folder, mail))
    return appended
