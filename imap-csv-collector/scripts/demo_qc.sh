#!/usr/bin/env bash
# 质控区间演示：docker compose up -d 后执行本脚本。
# 场景覆盖：
#   1. 初始采集（坏邮件停采 -> 清障 -> 完成）
#   2. 注入 11 封质控演示邮件并采集
#   3. 质控区间查询：可用 / 超限追溯待复测 / 非数值 / 同封重复质控 / 恢复 / 未闭合待核实
#   4. 长查询与新邮件入库并发互不阻塞；新合格质控闭合区间后，待核实 -> 可用
# 用法： bash scripts/demo_qc.sh
set -euo pipefail

API="${API:-http://localhost:8000}"
CTL=(docker compose run --rm mailctl)
MAILBOX_KEY="imap|10143|INBOX"

collect() {
  curl -sS -X POST "$API/collect" -H 'Content-Type: application/json' -d '{
    "host": "imap", "port": 10143, "folder": "INBOX",
    "username": "instrument@lab.local", "password": "labpass",
    "use_ssl": false
  }'
}

qc_query() {
  curl -sS -G "$API/qc/intervals" \
    --data-urlencode "mailbox_key=$MAILBOX_KEY" \
    --data-urlencode "qc_sample_no=QC-Pb" \
    --data-urlencode "test_item=铅Pb" \
    --data-urlencode "unit=mg/kg" \
    --data-urlencode "lower_limit=0.010" \
    --data-urlencode "upper_limit=0.050"
}

brief() { python3 -c "
import json, sys
d = json.load(sys.stdin)
print('status=', d['status'], ' ingested=', len(d['ingested']),
      ' total_new_records=', d['total_new_records'],
      ' stopped=', (d['stopped'] or {}).get('uid'))
"; }

verdicts() { python3 -c "
import json, sys
d = json.load(sys.stdin)
print('summary:', json.dumps(d['summary'], ensure_ascii=False))
for r in d['results']:
    print(f\"  {r['sample_no']:7s} {r['result']:6s} {r['unit']:6s} -> {r['verdict']:15s} ({r['reason']})\")
"; }

line() { printf '\n========== %s ==========\n' "$1"; }

line "1. 初始采集：停在坏邮件 UID3；移除坏邮件后完成采集"
collect | brief
docker compose exec -T imap rm -f /srv/mail/INBOX/3.eml || true
collect | brief

line "2. 注入 11 封质控演示邮件（幂等）并采集"
"${CTL[@]}" seed-qc
collect | brief

line "3. 质控区间查询：异常、追溯待复测、恢复、未闭合待核实"
qc_query > /tmp/qc_report_1.json
python3 - <<'PY'
import json
report = json.load(open("/tmp/qc_report_1.json"))
print("summary:", json.dumps(report["summary"], ensure_ascii=False, indent=2))
print("\n异常事件（触发邮件 + 追溯结果 ID）:")
for a in report["anomalies"]:
    print(f"  [{a['type']}] 邮件 {a['message_id']} (UID {a['uid']})"
          f" -> 追溯待复测结果 ID {a['affected_result_ids']}")
print("\n区间:")
for i in report["intervals"]:
    closed = (i["closed_by"] or {}).get("message_id")
    print(f"  区间{i['index']} [{i['status']:11s}] 开启={i['opened_by']['message_id']}"
          f" 闭合={closed} 结果ID={i['result_ids']}")
print("\n样品判定:")
for r in report["results"]:
    print(f"  {r['sample_no']:7s} {r['test_item']} = {r['result']:6s} {r['unit']:6s}"
          f" -> {r['verdict']:15s} ({r['reason']})")
PY

line "4. 闭合前查询：区间4 未闭合，S-2006/S-2007 待核实"
qc_query > /tmp/qc_before_close.json
python3 - <<'PY'
import json
d = json.load(open("/tmp/qc_before_close.json"))
for r in d["results"]:
    if r["sample_no"] in ("S-2006", "S-2007"):
        print(f"  {r['sample_no']} -> {r['verdict']} ({r['reason']})")
print("  快照 mail_count =", d["snapshot"]["mail_count"])
PY

line "5. 并发：长查询进行中，新邮件（闭合质控）入库不被阻塞"
cat > /tmp/qc_close.csv <<'CSV'
样品编号,检测项目,结果,单位
QC-Pb,铅Pb,0.024,mg/kg
S-2008,铅Pb,0.029,mg/kg
CSV
# 后台发起区间查询，同时注入闭合质控邮件并采集：两者并发，互不阻塞
qc_query > /tmp/qc_concurrent.json &
QPID=$!
docker compose run --rm -v /tmp/qc_close.csv:/tmp/qc_close.csv mailctl \
  append --message-id "qc-demo-12@lab-instrument" \
  --subject "仪器结果 质控批次 Q12（闭合区间4）" \
  --attachment qc_close.csv --csv-path /tmp/qc_close.csv
collect | brief
wait $QPID
python3 - <<'PY'
import json
d = json.load(open("/tmp/qc_concurrent.json"))
print("并发查询成功返回（自洽快照），快照 mail_count =", d["snapshot"]["mail_count"])
PY

line "6. 闭合后再次查询：S-2006/S-2007 可用，S-2008 进入新的未闭合区间"
qc_query > /tmp/qc_report_2.json
python3 - <<'PY'
import json
report = json.load(open("/tmp/qc_report_2.json"))
print("summary:", json.dumps(report["summary"], ensure_ascii=False))
for r in report["results"]:
    if r["sample_no"] in ("S-2006", "S-2007", "S-2008"):
        print(f"  {r['sample_no']} -> {r['verdict']} ({r['reason']})")
print("快照 mail_count =", report["snapshot"]["mail_count"])
PY

line "6. 到 http://localhost:8000/docs 打开 GET /qc/intervals，用以下参数亲自演示："
cat <<EOF
  mailbox_key   = $MAILBOX_KEY
  qc_sample_no  = QC-Pb
  test_item     = 铅Pb
  unit          = mg/kg
  lower_limit   = 0.010
  upper_limit   = 0.050
EOF

line "演示完成（可用 curl -X POST $API/admin/reset 重置结果库）"
