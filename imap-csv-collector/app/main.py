"""FastAPI 应用：采集触发、样品结果查询与质控区间分析。"""
from __future__ import annotations

import math
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, qc
from .collector import collect


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="仪器邮件 CSV 采集服务",
    description=(
        "从 IMAP 邮箱采集仪器 CSV 附件并入库，支持按样品编号查询检测结果，"
        "并提供质控区间分析：只有被前后两次合格质控夹住且期间无异常的结果"
        "才可交给统计脚本。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


class CollectRequest(BaseModel):
    host: str = Field(..., description="IMAP 服务器地址")
    port: int = Field(..., ge=1, le=65535, description="IMAP 端口")
    folder: str = Field(..., description="邮箱文件夹，如 INBOX")
    username: str
    password: str
    use_ssl: bool = Field(False, description="是否使用 IMAP over SSL")


class CollectResponse(BaseModel):
    status: str
    mailbox_key: str
    folder: str
    uidvalidity: int
    resume_from_uid: int
    scanned_uids: list[int]
    ingested: list[dict[str, Any]]
    skipped_duplicates: list[dict[str, Any]]
    stopped: dict[str, Any] | None
    total_new_records: int


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/collect", response_model=CollectResponse)
def trigger_collect(req: CollectRequest) -> dict[str, Any]:
    report = collect(
        host=req.host,
        port=req.port,
        folder=req.folder,
        username=req.username,
        password=req.password,
        use_ssl=req.use_ssl,
    )
    return report.to_dict()


@app.get("/results/{sample_no}")
def get_results(sample_no: str) -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.query_results_by_sample(conn, sample_no)
    if not rows:
        raise HTTPException(status_code=404, detail=f"未找到样品 {sample_no} 的结果")
    return {
        "sample_no": sample_no,
        "count": len(rows),
        "results": [dict(row) for row in rows],
    }


class QcIntervalResponse(BaseModel):
    query: dict[str, Any]
    snapshot: dict[str, Any]
    summary: dict[str, Any]
    qc_events: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]
    intervals: list[dict[str, Any]]
    results: list[dict[str, Any]]


@app.get("/qc/intervals", response_model=QcIntervalResponse)
def get_qc_intervals(
    mailbox_key: str = Query(
        ...,
        description="邮箱标识 host|port|folder，与 /collect 的入参对应",
        examples=["imap|10143|INBOX"],
    ),
    qc_sample_no: str = Query(
        ..., description="质控样品编号", examples=["QC-Pb"]
    ),
    test_item: str = Query(..., description="检测项目", examples=["铅Pb"]),
    unit: str = Query(..., description="单位", examples=["mg/kg"]),
    lower_limit: float = Query(..., description="质控下限（含）", examples=[0.010]),
    upper_limit: float = Query(..., description="质控上限（含）", examples=[0.050]),
) -> dict[str, Any]:
    """质控区间分析：按邮件首次入库顺序，用质控样读数判定同项目普通样品结果。

    只有被前后两次合格质控夹住且期间无异常的结果为 usable；
    异常（超限/非数值/同封重复质控/单位不符）会追溯标待复测，直到下一次合格质控；
    尚未闭合且未见异常的结果标待核实。

    整个查询在同一个 SQLite 只读事务（WAL 快照）内完成：
    返回的质控、样品结果与判定来自同一快照，且不阻塞新邮件入库。
    """
    if not (math.isfinite(lower_limit) and math.isfinite(upper_limit)):
        raise HTTPException(status_code=422, detail="上下限必须是有限数值")
    if lower_limit > upper_limit:
        raise HTTPException(status_code=422, detail="lower_limit 不能大于 upper_limit")

    params = qc.QcParams(
        mailbox_key=mailbox_key,
        qc_sample_no=qc_sample_no,
        test_item=test_item,
        unit=unit,
        lower_limit=lower_limit,
        upper_limit=upper_limit,
    )
    conn = db.connect()
    try:
        db.init_db(conn)
        # 显式开启读事务：本次响应的所有数据来自同一数据库快照；
        # WAL 模式下该读事务不阻塞 /collect 的并发写入。
        conn.execute("BEGIN")
        mails = qc.load_timeline(conn, mailbox_key, test_item)
        snapshot = qc.snapshot_meta(conn, mailbox_key)
        conn.rollback()
    finally:
        conn.close()

    report = qc.analyze(mails, params)
    report["snapshot"] = snapshot
    return report


@app.get("/samples")
def get_samples() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_samples(conn)
    return {"count": len(rows), "samples": [dict(row) for row in rows]}


@app.get("/ingest/state")
def get_ingest_state() -> dict[str, Any]:
    with db.connect() as conn:
        states = [
            dict(row)
            for row in conn.execute("SELECT * FROM ingest_state ORDER BY mailbox_key")
        ]
    return {"count": len(states), "states": states}


@app.get("/ingest/deliveries")
def get_deliveries() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_deliveries(conn)
    return {"count": len(rows), "deliveries": [dict(row) for row in rows]}


@app.post("/admin/reset")
def reset_database() -> dict[str, str]:
    """清空全部业务数据（演示/测试用，生产环境请经网关限制访问）。"""
    db.reset_all(db.get_db_path())
    return {"status": "reset"}
