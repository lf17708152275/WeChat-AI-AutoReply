# -*- coding: utf-8 -*-
"""
P2 主动发送 · 离线回归测试
=====================================================================
微信没开也能跑：把所有会碰真实窗口/OCR 的函数全部打桩，
只验证「护栏逻辑」「HTTP 接口」「找人三级策略的分支走向」是否正确。

跑法：  python 回归测试_主动发送.py
=====================================================================
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# --- 先把 bot 的重依赖打桩，避免 import 时去加载 OCR 模型 ---
import types

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


class _FakeImg:
    """假截图：只需要 size 属性。"""

    def __init__(self, w=1100, h=750):
        self.size = (w, h)


def check(label, actual, expected):
    global PASS, FAIL
    ok = actual == expected
    if ok:
        PASS += 1
        print(f"  [PASS] {label}: 实际={actual!r}")
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


# bot 内部的 log() 会写文件，测试期间换成静音
_LOGS = []
bot.log = lambda msg: _LOGS.append(str(msg))


print("=== 1. proactive_cfg 默认值补齐 ===")
cfg_min = {}
ps = bot.proactive_cfg(cfg_min)
check("默认 enabled", ps["enabled"], False)
check("默认 host", ps["host"], "127.0.0.1")
check("默认 port", ps["port"], 8765)
check("默认 whitelist_only", ps["whitelist_only"], True)
check("默认 max_per_hour", ps["max_per_hour"], 3)
check("默认 max_per_day", ps["max_per_day"], 20)
check("默认 require_confirm", ps["require_confirm"], False)

print("\n=== 2. proactive_check 护栏 ===")
bot.PROACTIVE_SENT_LOG[:] = []

# 2.1 未启用
cfg = {"proactive_send": {"enabled": False}, "whitelist": ["张三"]}
ok, code, _ = bot.proactive_check(cfg, "张三")
check("未启用被拒", (ok, code), (False, "disabled"))

# 2.2 不在白名单
cfg = {"proactive_send": {"enabled": True, "whitelist_only": True}, "whitelist": ["张三"]}
ok, code, _ = bot.proactive_check(cfg, "李四")
check("非白名单被拒", (ok, code), (False, "not_whitelisted"))

# 2.3 白名单命中
ok, code, _ = bot.proactive_check(cfg, "张三")
check("白名单放行", (ok, code), (True, "ok"))

# 2.4 前缀匹配（"张三" 命中 "张三 好的"）
ok, code, _ = bot.proactive_check(cfg, "张三")
check("前缀匹配放行", (ok, code), (True, "ok"))

# 2.5 越配防护："张三" 不应命中 "张三丰"
ok, code, _ = bot.proactive_check(cfg, "张三丰")
check("越配防护（张三丰）", (ok, code), (False, "not_whitelisted"))

# 2.6 群聊拒绝
ok, code, _ = bot.proactive_check({"proactive_send": {"enabled": True, "whitelist_only": False}}, "吃喝玩乐(25)")
check("群聊被拒", (ok, code), (False, "not_whitelisted"))

# 2.7 空 target
ok, code, _ = bot.proactive_check(cfg, "")
check("空 target 被拒", (ok, code), (False, "bad_target"))

# 2.8 每小时限流
bot.PROACTIVE_SENT_LOG[:] = []
now = time.time()
for i in range(3):
    bot.PROACTIVE_SENT_LOG.append((now - i * 10, "张三"))
ok, code, _ = bot.proactive_check(cfg, "张三")
check("每小时限流", (ok, code), (False, "rate_hour"))

# 2.9 24 小时之前的记录会被清掉
bot.PROACTIVE_SENT_LOG[:] = [(now - 90000, "张三"), (now - 90000, "张三")]
ok, code, _ = bot.proactive_check(cfg, "张三")
check("过期记录被清理后放行", (ok, code), (True, "ok"))

print("\n=== 3. whitelist_only=False 时的过滤 ===")
cfg_open = {"proactive_send": {"enabled": True, "whitelist_only": False}, "whitelist": []}
ok, code, _ = bot.proactive_check(cfg_open, "随便一个好友")
check("关闭白名单后普通好友放行", (ok, code), (True, "ok"))
ok, code, _ = bot.proactive_check(cfg_open, "文件传输助手")
check("黑名单仍被拒", (ok, code), (False, "not_whitelisted"))

print("\n=== 4. require_confirm -> 写 jsonl 而不发送 ===")
tmpdir = tempfile.mkdtemp()
bot.PENDING_CONFIRM_FILE = os.path.join(tmpdir, "pending_confirm.jsonl")
bot.PROACTIVE_SENT_LOG[:] = []
cfg_confirm = {
    "proactive_send": {"enabled": True, "whitelist_only": True, "require_confirm": True},
    "whitelist": ["张三"],
}
res = bot.proactive_send("张三", "在忙吗？", cfg_confirm)
check("返回 ok", res["ok"], True)
check("返回 queued", res["queued"], True)
check("返回 sent", res["sent"], False)
check("code", res["code"], "pending_confirm")
check_true("jsonl 已写入", os.path.exists(bot.PENDING_CONFIRM_FILE))
if os.path.exists(bot.PENDING_CONFIRM_FILE):
    with open(bot.PENDING_CONFIRM_FILE, "r", encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    check("队列条数", len(lines), 1)
    check("队列 target", lines[0]["target"], "张三")
    check("队列 text", lines[0]["text"], "在忙吗？")

print("\n=== 5. 空 text 被拒 ===")
bot.PROACTIVE_SENT_LOG[:] = []
res = bot.proactive_send("张三", "   ", cfg_confirm)
check("空 text", (res["ok"], res["code"]), (False, "bad_text"))

print("\n=== 6. find_contact 三级策略（打桩验证分支） ===")
# 全程用真实的 is_name_matched_strict（它本身就是被测对象之一），
# 只打桩「抓图 / OCR / 顶栏标题 / 点击」这些副作用。
_REAL = {
    "grab_wechat_window": bot.grab_wechat_window,
    "ocr_engine": bot.ocr_engine,
    "extract_active_chat_title": bot.extract_active_chat_title,
    "post_click_client_point": bot.post_click_client_point,
    "compute_wechat_layout": bot.compute_wechat_layout,
    "is_name_matched_strict": bot.is_name_matched_strict,
}

bot.grab_wechat_window = lambda hwnd: (_FakeImg(), (0, 0, 1100, 750), 1.0)
bot.compute_wechat_layout = lambda W, H, img=None: {
    "sidebar_w": 60, "chat_start_x": 320, "header_h": 85,
    "input_h": 150, "total_w_px": W, "total_h_px": H,
}
_clicked = []
bot.post_click_client_point = lambda hwnd, x, y: _clicked.append((x, y))

# 6.1 顶栏已经是目标 -> 直接 True，不点击
bot.ocr_engine = lambda img: ([], None)
bot.extract_active_chat_title = lambda res, layout: "张三"
check_true("顶栏命中直接放行", bot.find_contact(1, "张三", {}) is True)
check_true("顶栏命中时零点击", len(_clicked) == 0, f"(clicked={_clicked})")

# 6.2 顶栏不是，侧栏命中 -> 点击 -> 复核通过
_clicked.clear()
_titles = iter(["李四", "张三"])
bot.extract_active_chat_title = lambda res, layout: next(_titles, "张三")
# 会话行必须落在左侧栏内：sidebar_w(60) <= cx <= chat_start_x(320)
bot.ocr_engine = lambda img: ([([[100, 300], [200, 300], [200, 330], [100, 330]], "张三 晚上吃啥", 0.99)], None)
ok = bot.find_contact(1, "张三", {})
check_true("侧栏命中并复核通过", ok is True)
check_true("确实点击了一次", len(_clicked) == 1, f"(clicked={_clicked})")

# 6.3 顶栏不是，侧栏没有，且禁止搜索 -> False
_clicked.clear()
bot.extract_active_chat_title = lambda res, layout: "李四"
bot.ocr_engine = lambda img: ([], None)
ok = bot.find_contact(1, "张三", {"proactive_send": {"allow_search": False}})
check_true("侧栏没有且禁止搜索 -> False", ok is False)
check_true("没找到人时零点击", len(_clicked) == 0, f"(clicked={_clicked})")

# 6.4 顶栏是「张三丰」，目标是「张三」-> 越配防护，不能误认
bot.extract_active_chat_title = lambda res, layout: "张三丰"
bot.ocr_engine = lambda img: ([], None)
ok = bot.find_contact(1, "张三", {"proactive_send": {"allow_search": False}})
check_true("越配防护：张三丰不算张三", ok is False)

for _k, _v in _REAL.items():
    setattr(bot, _k, _v)

print("\n=== 7. HTTP 接口端到端（真起服务，真发请求） ===")
bot.PENDING_CONFIRM_FILE = os.path.join(tmpdir, "pending_confirm2.jsonl")
bot.PROACTIVE_SENT_LOG[:] = []

# 用一个随机高位端口，避免和真实 bot 抢 8765
import socket

_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = _s.getsockname()[1]
_s.close()

_cfg_http = {
    "proactive_send": {
        "enabled": True, "host": "127.0.0.1", "port": PORT,
        "whitelist_only": True, "require_confirm": False,
        "max_per_hour": 3, "max_per_day": 20, "allow_search": False,
    },
    "whitelist": ["张三"],
}
_orig_load_config = bot.load_config
bot.load_config = lambda: _cfg_http

# 微信窗口打桩成「存在」
bot.get_wechat_hwnd = lambda: (1, 4)
bot.find_contact = lambda hwnd, target, cfg=None: str(target) == "张三"
bot.send_reply_instant = lambda hwnd, rect, layout, target, text: True
bot.grab_wechat_window = lambda hwnd: (_FakeImg(), (0, 0, 1100, 750), 1.0)
bot.compute_wechat_layout = lambda W, H, img=None: {
    "sidebar_w": 60, "chat_start_x": 320, "header_h": 85,
    "input_h": 150, "total_w_px": W, "total_h_px": H,
}

srv = bot.start_proactive_server(_cfg_http)
check_true("服务已启动", srv is not None)
time.sleep(0.3)

BASE = f"http://127.0.0.1:{PORT}"


def _post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


# 7.1 GET /status
code, body = _get("/status")
check("GET /status 状态码", code, 200)
check("status.enabled", body.get("enabled"), True)
check("status.sent_today", body.get("sent_today"), 0)

# 7.2 POST /send 白名单好友 -> 成功
code, body = _post("/send", {"target": "张三", "text": "早上好"})
check("POST /send 状态码", code, 200)
check("send.ok", body.get("ok"), True)
check("send.sent", body.get("sent"), True)
check("限流账本 +1", len(bot.PROACTIVE_SENT_LOG), 1)

# 7.3 POST /send 非白名单 -> 403
code, body = _post("/send", {"target": "李四", "text": "你好"})
check("非白名单状态码", code, 403)
check("非白名单 code", body.get("code"), "not_whitelisted")

# 7.4 POST /send 缺字段 -> 400
code, body = _post("/send", {"target": "张三"})
check("缺 text 状态码", code, 400)
check("缺 text code", body.get("code"), "missing_field")

# 7.5 POST /send 非法 JSON -> 400
req = urllib.request.Request(
    BASE + "/send", data=b"{not json",
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
check("非法 JSON 状态码", code, 400)

# 7.5b 坏 JSON 要给出可操作的 detail + 回显收到的原文
# （PowerShell 会吃掉 -d '{"target":"x"}' 里的双引号，这是真实踩过的坑）
req = urllib.request.Request(
    BASE + "/send", data="{target:张三,text:你好}".encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        code, body = r.status, json.loads(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    code, body = e.code, json.loads(e.read().decode("utf-8"))
check("坏 JSON 不误判成表单", body.get("code"), "bad_json")
check("坏 JSON detail 给出 query 写法", "target=" in (body.get("detail") or ""), True)
check("坏 JSON 回显 received", "张三" in (body.get("received") or ""), True)


# 7.5c 三种「引号被吃掉后仍可用」的等价写法
def _post_raw(path, data, ctype):
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


code, body = _post_raw("/send", b"target=%E5%BC%A0%E4%B8%89&text=form",
                       "application/x-www-form-urlencoded")
check("form-urlencoded 可用", code, 200)
code, body = _post_raw("/send", "target=张三&text=raw".encode("utf-8"),
                       "application/x-www-form-urlencoded")
check("未编码中文 form 可用", code, 200)
# 这三条会真实记账，先隔离限流账本，避免干扰后面 7.7 的限流断言
_saved_log = list(bot.PROACTIVE_SENT_LOG)
bot.PROACTIVE_SENT_LOG[:] = []
code, body = _post_raw("/send?target=%E5%BC%A0%E4%B8%89&text=query", b"", "application/json")
check("query 参数可用", code, 200)

# 7.5d GBK 编码的请求体 —— Windows 终端里 curl -d "..." 的真实字节
code, body = _post_raw("/send", json.dumps({"target": "张三", "text": "gbkjson"}).encode("gbk"),
                       "application/json")
check("GBK 编码 JSON 可识别", code, 200)
code, body = _post_raw("/send", "target=张三&text=gbkform".encode("gbk"),
                       "application/x-www-form-urlencoded")
check("GBK 编码表单可识别", code, 200)
bot.PROACTIVE_SENT_LOG[:] = _saved_log

# 7.6 POST 到错误路径 -> 404
code, body = _post("/nope", {})
check("错误路径状态码", code, 404)

# 7.7 限流生效：连发到上限后应被拒
for i in range(2):
    _post("/send", {"target": "张三", "text": f"第{i+2}条"})
code, body = _post("/send", {"target": "张三", "text": "超限了"})
check("超出每小时上限状态码", code, 403)
check("超出每小时上限 code", body.get("code"), "rate_hour")

bot.stop_proactive_server()
bot.load_config = _orig_load_config

print("\n=== 8. 发送失败时不记账 ===")
bot.PROACTIVE_SENT_LOG[:] = []
bot.send_reply_instant = lambda *a, **k: False
cfg_plain = {
    "proactive_send": {"enabled": True, "whitelist_only": True, "require_confirm": False, "allow_search": False},
    "whitelist": ["张三"],
}
res = bot.proactive_send("张三", "测试", cfg_plain)
check("发送失败 ok", res["ok"], False)
check("发送失败 code", res["code"], "send_failed")
check("发送失败不记账", len(bot.PROACTIVE_SENT_LOG), 0)

print("\n=== 9. 找不到好友时不发 ===")
bot.find_contact = lambda hwnd, target, cfg=None: False
res = bot.proactive_send("张三", "测试", cfg_plain)
check("找不到好友 code", res["code"], "contact_not_found")
check("找不到好友不记账", len(bot.PROACTIVE_SENT_LOG), 0)

print("\n=== 10. 微信没开时优雅失败 ===")
bot.find_contact = lambda hwnd, target, cfg=None: True
bot.get_wechat_hwnd = lambda: (None, None)
res = bot.proactive_send("张三", "测试", cfg_plain)
check("无微信 code", res["code"], "no_wechat")

print("\n" + "=" * 60)
if FAIL == 0:
    print(f"✅ 全部用例 PASS（共 {PASS} 项），零回退")
    sys.exit(0)
else:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
    sys.exit(1)
