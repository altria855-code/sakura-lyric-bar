"""假浮窗:读 stdin 的 JSON 行,回显 ready 并原样回抄收到的 type 列表。"""
import json
import sys

print(json.dumps({"type": "ready", "pid": 1, "screens": []}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    if message.get("type") == "bye":
        break
    print(json.dumps({"type": "seen", "kind": message.get("type")}, ensure_ascii=False), flush=True)
