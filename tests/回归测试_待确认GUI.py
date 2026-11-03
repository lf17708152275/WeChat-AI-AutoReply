# -*- coding: utf-8 -*-
"""
P3 GUI 侧 · 待确认队列逻辑回归测试
=====================================================================
不弹窗、不连微信，只验证 GUI 里那几段「读队列 / 重写队列 / 删一条」
的纯逻辑，以及 _call_bot_http 的失败兜底。

跑法：  python 回归测试_待确认GUI.py
=====================================================================
"""
import io
import json
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# GUI 模块 import 时会 ctypes.windll，Windows 上没问题
import wechat_bot_gui_dev as gui

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


class _FakeGUI:
    """借 GUI 的方法但不建窗口，避免弹窗和 tkinter 主循环。"""

    _load_pending_from_disk = gui.WeChatBotGUI._load_pending_from_disk
    _rewrite_pending_file = gui.WeChatBotGUI._rewrite_pending_file
    _drop_pending = gui.WeChatBotGUI._drop_pending
    _call_bot_http = gui.WeChatBotGUI._call_bot_http

    def __init__(self, cfg):
        self._cfg = cfg

    def load_config(self):
        return self._cfg

    def _fake_error(self, *a, **k):
        pass


TMP = tempfile.mkdtemp()
PENDING = os.path.join(TMP, "pending_confirm.jsonl")
gui.PENDING_FILE = PENDING
gui.messagebox.showerror = lambda *a, **k: None
gui.messagebox.showwarning = lambda *a, **k: None

g = _FakeGUI({"proactive_send": {"host": "127.0.0.1", "port": 1}})

print("=== 1. 文件不存在时返回空列表 ===")
check("空列表", g._load_pending_from_disk(), [])

print("\n=== 2. 正常读取 ===")
recs = [
    {"id": "p2", "target": "李四", "text": "第二条", "created_ts": 200, "status": "pending"},
    {"id": "p1", "target": "张三", "text": "第一条", "created_ts": 100, "status": "pending"},
]
with open(PENDING, "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
items = g._load_pending_from_disk()
check("条数", len(items), 2)
check("按时间正序，第一条是 p1", items[0]["id"], "p1")
check("第二条是 p2", items[1]["id"], "p2")

print("\n=== 3. 非 pending 状态的记录被忽略 ===")
with open(PENDING, "w", encoding="utf-8") as f:
    f.write(json.dumps({"id": "a", "target": "A", "text": "x", "created_ts": 1, "status": "pending"}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"id": "b", "target": "B", "text": "y", "created_ts": 2, "status": "sent"}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"id": "c", "target": "C", "text": "z", "created_ts": 3, "status": "rejected"}, ensure_ascii=False) + "\n")
items = g._load_pending_from_disk()
check("只剩 pending 那条", len(items), 1)
check("是 a", items[0]["id"], "a")

print("\n=== 4. 脏数据不炸 ===")
with open(PENDING, "w", encoding="utf-8") as f:
    f.write("这不是 json\n")
    f.write(json.dumps({"id": "ok", "target": "A", "text": "x", "created_ts": 1, "status": "pending"}, ensure_ascii=False) + "\n")
    f.write("\n")
    f.write(json.dumps({"target": "没有id"}, ensure_ascii=False) + "\n")
items = g._load_pending_from_disk()
check("跳过脏行，只留有效那条", len(items), 1)
check("id 正确", items[0]["id"], "ok")

print("\n=== 5. 重写文件 ===")
g._rewrite_pending_file([{"id": "x", "target": "王五", "text": "hi", "created_ts": 9, "status": "pending"}])
items = g._load_pending_from_disk()
check("重写后条数", len(items), 1)
check("重写后 target", items[0]["target"], "王五")
check_true("没有残留 tmp 文件", not os.path.exists(PENDING + ".gui.tmp"))

print("\n=== 6. 删掉一条 ===")
g._rewrite_pending_file([
    {"id": "k1", "target": "A", "text": "1", "created_ts": 1, "status": "pending"},
    {"id": "k2", "target": "B", "text": "2", "created_ts": 2, "status": "pending"},
])
ok = g._drop_pending("k1")
check("drop 返回 True", ok, True)
items = g._load_pending_from_disk()
check("剩 1 条", len(items), 1)
check("剩的是 k2", items[0]["id"], "k2")

print("\n=== 7. 删不存在的 id 不报错 ===")
ok = g._drop_pending("nope")
check("仍然 True", ok, True)
check("条数不变", len(g._load_pending_from_disk()), 1)

print("\n=== 8. _call_bot_http 连不上时优雅返回 ===")
code, body = g._call_bot_http("/confirm", {"id": "x"})
check("状态码 None", code, None)
check("ok False", body.get("ok"), False)
check("code http_error", body.get("code"), "http_error")

print("\n=== 9. GUI 里 confirm/reject 的路径和 bot 一致 ===")
# bot 端路由必须存在，否则 GUI 点了没反应
import types  # noqa: E402

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

import wechat_ai_bot_dev as bot  # noqa: E402

check_true("bot 有 confirm_pending", callable(getattr(bot, "confirm_pending", None)))
check_true("bot 有 reject_pending", callable(getattr(bot, "reject_pending", None)))
check_true("bot 有 list_pending", callable(getattr(bot, "list_pending", None)))
check("GUI 和 bot 用同一个队列文件名",
      os.path.basename(gui.PENDING_FILE), os.path.basename(bot.PENDING_CONFIRM_FILE))

print("\n" + "=" * 60)
if FAIL == 0:
    print(f"✅ 全部用例 PASS（共 {PASS} 项），零回退")
    sys.exit(0)
else:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
    sys.exit(1)
