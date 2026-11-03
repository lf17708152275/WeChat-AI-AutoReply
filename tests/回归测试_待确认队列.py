# -*- coding: utf-8 -*-
"""
P3 待确认队列 · 离线回归测试
=====================================================================
验证 pending_confirm.jsonl 的落盘 / 恢复 / 确认 / 拒绝 / 过期，
以及 HTTP 的 /pending、/confirm、/reject 三个接口。

跑法：  python 回归测试_待确认队列.py
=====================================================================
"""
import io
import json
import os
import socket
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

for _name in ("numpy", "pyperclip", "rapidocr_onnxruntime"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except Exception:
            _stub = types.ModuleType(_name)
            if _name == "numpy":
                _stub.array = lambda *a, **k: None
                _stub.sum = lambda *a, **k: 0
            if _name == "pyperclip":
                _stub.copy = lambda *a, **k: None
                _stub.paste = lambda *a, **k: ""
            if _name == "rapidocr_onnxruntime":
                _stub.RapidOCR = lambda *a, **k: (lambda img: ([], None))
            sys.modules[_name] = _stub

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import wechat_ai_bot_dev as bot

PASS = 0
FAIL = 0


def check(label, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  [PASS] {label}: {actual!r}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}: 实际={actual!r} 期望={expected!r}")


def check_true(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


class _FakeImg:
    def __init__(self, w=1100, h=750):
        self.size = (w, h)


_LOGS = []
bot.log = lambda msg: _LOGS.append(str(msg))

TMP = tempfile.mkdtemp()
bot.PENDING_CONFIRM_FILE = os.path.join(TMP, "pending_confirm.jsonl")

CFG = {
    "proactive_send": {
        "enabled": True, "host": "127.0.0.1", "port": 0,
        "whitelist_only": True, "require_confirm": True,
        "max_per_hour": 3, "max_per_day": 20, "allow_search": False,
        "confirm_expire_hours": 12,
    },
    "whitelist": ["张三", "李四"],
    "blacklist": ["文件传输助手"],
}

# 发送链路全打桩
bot.get_wechat_hwnd = lambda: (1, 4)
bot.find_contact = lambda hwnd, target, cfg=None: True
bot.grab_wechat_window = lambda hwnd: (_FakeImg(), (0, 0, 1100, 750), 1.0)
bot.compute_wechat_layout = lambda W, H, img=None: {
    "sidebar_w": 60, "chat_start_x": 320, "header_h": 85,
    "input_h": 150, "total_w_px": W, "total_h_px": H,
}
_SENT = []
bot.send_reply_instant = lambda hwnd, rect, layout, target, text: (_SENT.append((target, text)) or True)

print("=== 1. 入队 -> 落盘 ===")
bot.PENDING_CONFIRM.clear()
bot.PROACTIVE_SENT_LOG[:] = []
res = bot.proactive_send("张三", "在忙吗？", CFG)
check("返回 code", res.get("code"), "pending_confirm")
check("返回 queued", res.get("queued"), True)
check("内存队列长度", len(bot.PENDING_CONFIRM), 1)
check_true("文件已创建", os.path.exists(bot.PENDING_CONFIRM_FILE))
with open(bot.PENDING_CONFIRM_FILE, "r", encoding="utf-8") as f:
    lines = [json.loads(x) for x in f if x.strip()]
check("磁盘条数", len(lines), 1)
check("磁盘 status", lines[0]["status"], "pending")
check("磁盘 target", lines[0]["target"], "张三")
check("磁盘 text", lines[0]["text"], "在忙吗？")
check_true("磁盘有 id", bool(lines[0].get("id")))

PID1 = lines[0]["id"]

print("\n=== 2. list_pending 排序 ===")
bot.proactive_send("李四", "第二条", CFG)
items = bot.list_pending()
check("待确认条数", len(items), 2)
check("按时间正序（第一条是张三）", items[0]["target"], "张三")
check("第二条是李四", items[1]["target"], "李四")

print("\n=== 3. 模拟进程重启 -> 从磁盘恢复 ===")
bot.PENDING_CONFIRM.clear()
bot._load_pending_confirm()
check("恢复条数", len(bot.PENDING_CONFIRM), 2)
check_true("恢复后能找到第一条", PID1 in bot.PENDING_CONFIRM)

print("\n=== 4. confirm_pending 真发出去 ===")
_SENT.clear()
res = bot.confirm_pending(PID1, CFG)
check("confirm ok", res.get("ok"), True)
check("confirm sent", res.get("sent"), True)
check("确实发出去了", _SENT, [("张三", "在忙吗？")])
check("确认后从队列移除", PID1 in bot.PENDING_CONFIRM, False)
check("剩余队列", len(bot.PENDING_CONFIRM), 1)
with open(bot.PENDING_CONFIRM_FILE, "r", encoding="utf-8") as f:
    left = [json.loads(x) for x in f if x.strip()]
check("磁盘也只剩 1 条", len(left), 1)

print("\n=== 5. confirm 不存在的 id ===")
res = bot.confirm_pending("p-does-not-exist", CFG)
check("code", res.get("code"), "not_found")

print("\n=== 6. reject_pending ===")
left_id = left[0]["id"]
res = bot.reject_pending(left_id, "不想发")
check("reject ok", res.get("ok"), True)
check("队列清空", len(bot.PENDING_CONFIRM), 0)
with open(bot.PENDING_CONFIRM_FILE, "r", encoding="utf-8") as f:
    check("磁盘也清空", len([x for x in f if x.strip()]), 0)

print("\n=== 7. reject 不存在的 id ===")
res = bot.reject_pending("p-nope")
check("code", res.get("code"), "not_found")

print("\n=== 8. 过期清理 ===")
bot.PENDING_CONFIRM.clear()
bot.PENDING_CONFIRM["old"] = {
    "id": "old", "target": "张三", "text": "很久以前",
    "created_ts": time.time() - 13 * 3600, "status": "pending",
}
bot.PENDING_CONFIRM["fresh"] = {
    "id": "fresh", "target": "李四", "text": "刚发的",
    "created_ts": time.time() - 60, "status": "pending",
}
bot._save_pending_confirm()
bot._expire_pending()
check("过期那条被清", "old" in bot.PENDING_CONFIRM, False)
check("新的那条保留", "fresh" in bot.PENDING_CONFIRM, True)

print("\n=== 9. 确认时护栏仍然生效（白名单） ===")
bot.PENDING_CONFIRM.clear()
bot.PENDING_CONFIRM["x1"] = {
    "id": "x1", "target": "王五", "text": "不在白名单",
    "created_ts": time.time(), "status": "pending",
}
bot._save_pending_confirm()
_SENT.clear()
res = bot.confirm_pending("x1", CFG)
check("确认也拦非白名单", res.get("code"), "not_whitelisted")
check("没发出去", _SENT, [])
check("失败时保留在队列里", "x1" in bot.PENDING_CONFIRM, True)

print("\n=== 10. 确认时护栏仍然生效（频率） ===")
bot.PENDING_CONFIRM.clear()
bot.PENDING_CONFIRM["x2"] = {
    "id": "x2", "target": "张三", "text": "超频",
    "created_ts": time.time(), "status": "pending",
}
bot._save_pending_confirm()
bot.PROACTIVE_SENT_LOG[:] = [(time.time(), "张三") for _ in range(3)]
_SENT.clear()
res = bot.confirm_pending("x2", CFG)
check("确认也拦频率", res.get("code"), "rate_hour")
check("没发出去", _SENT, [])

print("\n=== 11. HTTP /pending /confirm /reject ===")
bot.PENDING_CONFIRM.clear()
bot.PROACTIVE_SENT_LOG[:] = []
bot._save_pending_confirm()

_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = _s.getsockname()[1]
_s.close()
CFG_HTTP = json.loads(json.dumps(CFG))
CFG_HTTP["proactive_send"]["port"] = PORT

_orig_load = bot.load_config
bot.load_config = lambda: CFG_HTTP

srv = bot.start_proactive_server(CFG_HTTP)
check_true("服务已启动", srv is not None)
time.sleep(0.3)
BASE = f"http://127.0.0.1:{PORT}"


def _req(method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


# 11.1 先塞两条待确认
_SENT.clear()
_req("POST", "/send", {"target": "张三", "text": "接口测试1"})
_req("POST", "/send", {"target": "李四", "text": "接口测试2"})

code, body = _req("GET", "/pending")
check("GET /pending 状态码", code, 200)
check("pending 条数", len(body.get("items") or []), 2)

code, body = _req("GET", "/status")
check("status.pending_confirm", body.get("pending_confirm"), 2)

ids = [x["id"] for x in body.get("items") or []] if body.get("items") else []
code, body = _req("GET", "/pending")
ids = [x["id"] for x in body["items"]]
first_id = ids[0]

code, body = _req("POST", "/confirm", {"id": first_id})
check("POST /confirm 状态码", code, 200)
check("confirm sent", body.get("sent"), True)
check("真的发出去了", len(_SENT), 1)

second_id = ids[1]
code, body = _req("POST", "/reject", {"id": second_id, "reason": "算了"})
check("POST /reject 状态码", code, 200)
check("reject ok", body.get("ok"), True)

code, body = _req("GET", "/pending")
check("清空后 pending 条数", len(body.get("items") or []), 0)

code, body = _req("POST", "/confirm", {"id": "p-ghost"})
check("confirm 不存在 403", code, 403)
code, body = _req("POST", "/reject", {"id": "p-ghost"})
check("reject 不存在 404", code, 404)

bot.stop_proactive_server()
bot.load_config = _orig_load

print("\n" + "=" * 60)
if FAIL == 0:
    print(f"✅ 全部用例 PASS（共 {PASS} 项），零回退")
    sys.exit(0)
else:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
    sys.exit(1)
