# 仪器邮件 CSV 采集服务

检测中心资料员无需逐封下载邮件：服务通过 IMAP 自动采集仪器邮件中的 CSV 附件，
解析后写入 SQLite 样品结果库，并提供按样品编号查询的 HTTP API。

质控员可用质控区间查询 API 圈定能交给统计脚本的结果：指定邮箱、质控样品编号、
检测项目、单位和数值上下限后，服务按邮件首次入库顺序分析同项目的普通样品，
只有被前后两次合格质控夹住且期间无异常的结果才标记为「可用」。

## 核心保证

| 需求 | 实现方式 |
| --- | --- |
| 整封邮件原子入库 | 一封邮件的**全部** CSV 解析成功后，检测记录、投递记录、UID 进度才在同一个 SQLite 事务提交；任一附件损坏则整封放弃，无部分结果 |
| 失败停在该邮件并返回标识 | 坏附件 / 断线 / 冲突都通过响应的 `stopped` 字段返回 UID、Message-ID、主题和原因，进度不推进 |
| 重启可续采 | 进度持久化在 `ingest_state`，下次从 `last_uid + 1` 开始 |
| UIDVALIDITY 改变后重扫 | 检测到变化即清零进度重新扫描，历史投递记录保留用于去重 |
| 重投识别 | Message-ID + 全部附件的 SHA-256 摘要：同 ID 同摘要跳过；同 ID 不同摘要停采待核实，原结果原样保留 |
| 采集参数 | 每次采集请求显式传入 IMAP 主机、端口、文件夹及账号密码 |
| 质控区间判定 | 按邮件首次入库顺序（`mail_delivery.id`）；同封邮件先判质控再归属样品；超限 / 非数值 / 同封重复质控 / 单位不符均为异常 |
| 异常追溯 | 异常使当前区间作废：自最近一次合格质控后的结果追溯标为「待复测」，直到下一次合格质控；响应带触发邮件与结果 ID |
| 快照一致 | 每次区间查询在同一个 SQLite 读事务（WAL 快照）内完成，返回的质控、样品结果与判定来自同一快照 |
| 查询不阻塞入库 | WAL 模式下读事务与 `/collect` 的写事务并发，长区间查询不阻塞新邮件入库 |
| 重扫不重复计算 | 重投 / UIDVALIDITY 重扫不产生新的投递与结果行，质控分析天然不重复 |

## 目录结构

```
app/                 FastAPI + 采集核心 + SQLite 持久层
  main.py            HTTP 路由（采集、样品查询、质控区间查询）
  collector.py       IMAP 采集、重投识别、事务边界
  csv_parse.py       CSV 校验解析（表头：样品编号,检测项目,结果,单位）
  qc.py              质控区间分析（判定逻辑 + 快照读取）
  db.py              SQLite 建表与查询
imap_server/         本地 IMAP 样例服务（纯标准库实现 IMAP4rev1 子集）
  server.py          IMAP 协议服务，支持 FLAKY_FETCH_AFTER 断线注入
  mailbox.py         文件邮箱、附件替换、邮箱重建
  seed.py            演示种子：两封正常邮件 + 一封多附件坏 CSV 邮件 + 11 封质控演示邮件
  cli.py             邮箱控制 CLI（重投/重建/修复/追加/质控种子）
tests/               28 个 pytest 用例，覆盖全部承诺语义
scripts/demo.sh      采集端到端演示脚本
scripts/demo_qc.sh   质控区间演示脚本
docker-compose.yml   api + imap 两个服务 + mailctl 控制容器
```

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

- API 文档： http://localhost:8000/docs
- IMAP 样例服务： localhost:10143（明文 IMAP，账号 `instrument@lab.local` / `labpass`）
- SQLite 数据与邮箱分别持久化在 `db_data`、`mail_data` 卷

运行完整演示（多附件失败、重投、邮箱重建、冲突停采）：

```bash
bash scripts/demo.sh
```

运行质控区间演示（异常、恢复、未闭合区间、并发不阻塞）：

```bash
bash scripts/demo_qc.sh
```

### 断线场景演示

给 imap 服务注入故障：第 N 次 FETCH 响应发送一半即断开 TCP。

```bash
FLAKY_FETCH_AFTER=2 docker compose up -d --force-recreate imap
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{
  "host":"imap","port":10143,"folder":"INBOX",
  "username":"instrument@lab.local","password":"labpass","use_ssl":false}'
# stopped.uid 指向失败邮件；去掉环境变量重启 imap 后再次调用即从断点续采
```

## API

### POST /collect — 触发一轮采集

```bash
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{
  "host": "imap",
  "port": 10143,
  "folder": "INBOX",
  "username": "instrument@lab.local",
  "password": "labpass",
  "use_ssl": false
}'
```

响应（正常完成 `status=completed`；停采时 `status=stopped` 并带 `stopped` 邮件标识）：

```json
{
  "status": "stopped",
  "mailbox_key": "imap|10143|INBOX",
  "uidvalidity": 1794581572,
  "resume_from_uid": 3,
  "scanned_uids": [1, 2, 3],
  "ingested": [ {"uid": 1, "message_id": "inst-0001@lab-instrument", "record_count": 3, "...": "..."} ],
  "skipped_duplicates": [],
  "stopped": {
    "reason": "坏 CSV 附件，整封邮件不入库: 附件 batch_b3_bad.csv: 第 3 行结果为空",
    "uid": 3,
    "message_id": "inst-0003@lab-instrument",
    "subject": "仪器结果 批次 B3（含损坏附件）"
  },
  "total_new_records": 5
}
```

### GET /results/{样品编号} — 按样品查询

```bash
curl localhost:8000/results/S-1001
```

### GET /qc/intervals — 质控区间查询

按邮件首次入库顺序，用质控样读数判定同项目普通样品结果是否可交给统计脚本。

```bash
curl -G localhost:8000/qc/intervals \
  --data-urlencode 'mailbox_key=imap|10143|INBOX' \
  --data-urlencode 'qc_sample_no=QC-Pb' \
  --data-urlencode 'test_item=铅Pb' \
  --data-urlencode 'unit=mg/kg' \
  --data-urlencode 'lower_limit=0.010' \
  --data-urlencode 'upper_limit=0.050'
```

参数：

| 参数 | 说明 |
| --- | --- |
| `mailbox_key` | 邮箱标识 `host|port|folder`，与 `/collect` 的入参对应 |
| `qc_sample_no` | 质控样品编号（该编号 + 检测项目的记录视为质控读数） |
| `test_item` | 检测项目（只分析该项目的记录） |
| `unit` | 单位（质控与样品记录单位须一致，否则判异常/降格） |
| `lower_limit` / `upper_limit` | 质控数值上下限（含边界） |

判定规则：

- 同一封邮件内**先判质控，再归属**该邮件的普通样品结果。
- 质控异常：`out_of_limits`（超限）、`non_numeric`（非数值）、
  `duplicate_in_mail`（同封重复质控）、`unit_mismatch`（单位不符）。
- 异常使当前区间作废：自最近一次合格质控后的结果**追溯标为待复测**，
  直到下一次合格质控；异常之后、恢复之前的结果同样待复测。
- 样品结果三态：
  - `usable` 可用 —— 被前后两次合格质控夹住且期间无异常；
  - `pending_retest` 待复测 —— 区间被异常作废、异常后未恢复、或此前无合格质控；
  - `pending_review` 待核实 —— 区间尚未闭合（缺下一次合格质控）且期间无异常。
- 每条待复测结果带 `trigger`（触发异常的邮件），每个异常带
  `affected_result_ids`（追溯的结果 ID）。

响应节选（演示数据）：

```json
{
  "summary": {"usable_count": 2, "pending_retest_count": 3,
              "pending_review_count": 2, "qc_anomaly_count": 3},
  "anomalies": [
    {"type": "out_of_limits", "message_id": "qc-demo-05@lab-instrument",
     "uid": 8, "affected_result_ids": [11]}
  ],
  "intervals": [
    {"index": 0, "status": "closed_ok",
     "opened_by": {"message_id": "qc-demo-01@lab-instrument"},
     "closed_by": {"message_id": "qc-demo-03@lab-instrument"},
     "result_ids": [7, 9]}
  ],
  "results": [
    {"result_id": 11, "sample_no": "S-2003", "verdict": "pending_retest",
     "reason": "interval_invalidated_by_anomaly",
     "trigger": {"message_id": "qc-demo-05@lab-instrument", "uid": 8}}
  ],
  "snapshot": {"mail_count": 13, "max_delivery_id": 13}
}
```

一致性保证：整个查询在同一个 SQLite 读事务（WAL 快照）内完成——返回的
质控事件、样品结果与判定来自同一数据库快照；WAL 模式下该读事务不阻塞
`/collect` 的新邮件入库，采集与长区间查询可同时进行。

### 其他端点

| 端点 | 说明 |
| --- | --- |
| `GET /samples` | 已入库样品汇总 |
| `GET /ingest/state` | 各邮箱 UIDVALIDITY 与 last_uid 进度 |
| `GET /ingest/deliveries` | 已处理投递（Message-ID、附件摘要） |
| `GET /health` | 健康检查 |
| `POST /admin/reset` | 清空结果库（演示用） |

## CSV 约定

UTF-8（可带 BOM），首行表头固定为 `样品编号,检测项目,结果,单位`，
四个字段均不允许为空，否则该附件所在整封邮件判定失败。

## 本地无 Docker 运行 / 测试

```bash
python -m venv .venv && source .venv/bin/activate   # 或使用已有解释器
pip install -r requirements.txt pytest httpx
PYTHONPATH=. pytest -q

# 手动起两个进程
python -m imap_server.cli serve --host 127.0.0.1 --port 10143
COLLECTOR_DB=./data/collector.db uvicorn app.main:app --port 8000
```

## 设计说明与边界

- 采集按邮箱文件夹维度维护进度，`mailbox_key = host|port|folder`。
- 附件摘要对「文件名 + 文件内容 SHA-256」排序后再做一次 SHA-256，
  附件顺序变化不影响判定，内容或文件名变化即视为不同投递。
- 质控分析按 `mail_delivery.id`（首次入库顺序）排列邮件，邮件内按
  `test_result.id` 排列记录；重投与 UIDVALIDITY 重扫不产生新行，不重复计算。
- 质控读数用 `float()` 解析且要求有限（`ND`、`<0.01`、`inf` 等判非数值）。
- 区间查询是只读事务：WAL 快照保证一致性，且不阻塞 `/collect` 写入；
  `/collect` 在单进程内加锁串行化，生产多副本部署时应改用分布式锁或单 worker。
- 样例 IMAP 服务仅实现采集所需命令（LOGIN/SELECT/STATUS/UID SEARCH/UID FETCH 等），
  面向演示与测试，不可作为生产邮箱使用。
