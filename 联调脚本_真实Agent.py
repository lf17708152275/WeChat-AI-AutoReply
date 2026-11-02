# -*- coding: utf-8 -*-
"""联调脚本_真实Agent.py —— 用真实 DSH A2A 服务跑一遍 P0+P1 全链路

不碰微信：send_reply_instant 被打桩，只记录「本该发出去什么」。
运行:  python 联调脚本_真实Agent.py
"""

import io
import json
import os
import sys
import time
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def _stub(name, **attrs):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _A(object):
    shape = (0,)
    size = 0

    def __getitem__(self, i):
        return _A()

    def tobytes(self):
        return b""

    def astype(self, *a, **k):
        return self

    def max(self):
        return 0

    def __sub__(s, o):
        return s

    def __add__(s, o):
        return s

    def __gt__(s, o):
        return s

    def __ge__(s, o):
        return s


_np = _stub("numpy")
_np.array = lambda *a, **k: _A()
_np.sum = lambda *a, **k: 0
_np.uint8 = "uint8"
_stub("pyperclip", copy=lambda *a, **k: True, paste=lambda: "")


class _OCR(object):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return [], None


_stub("rapidocr_onnxruntime", RapidOCR=_OCR)

import wechat_ai_bot_dev as bot  # noqa: E402

LOG = []
bot.log = lambda m: (LOG.append(str(m)), print(str(m), flush=True))

SENT = []
bot.send_reply_instant = lambda hwnd, rect, layout, target, final_reply: (
    SENT.append((target, final_reply)) or True
)

REAL_CFG = {
    "agent": {
        "enabled": True,
        "url": "http://127.0.0.1:44708/a2a",
        "context_prefix": "wx-",
        "timeout_seconds": 180,
        "max_defer_seconds": 900,
        "retries": 0,
    },
    "reply_delay_seconds": 0.0,
    "include_prefix": False,
}
bot.load_config = lambda: REAL_CFG

CASES = [
    ("好兄弟A", "晚上一起吃饭不？我请", "约饭 -> 应该 reply"),
    ("老同学B", "【拼多多】砍一刀帮帮忙 https://x.cn/abc", "广告 -> 应该 ignore"),
    ("好兄弟A", "算了，我困了先睡了，晚安", "对方结束话题 -> 应该 ignore"),
]

print("=" * 70)
print("P0+P1 全链路联调（真实 DSH Agent，不碰微信）")
print("=" * 70)

cfg = bot.load_config()
client = bot.get_agent_client(cfg)
if client is None:
    print("❌ AgentClient 不可用")
    sys.exit(1)

if not client.is_available():
    print(f"❌ A2A 服务不可达: {client.card_url}")
    print("   开启: curl -s -X POST http://127.0.0.1:44708/a2a/api "
          "-H 'Content-Type: application/json' -d '{\"action\":\"server-enable\"}'")
    sys.exit(1)
print(f"✅ A2A 服务在线: {client.url}\n")

all_ok = True
for sender, msg, expect in CASES:
    with bot.AGENT_LOCK:
        bot.AGENT_RESULTS.clear()
        bot.AGENT_PENDING.clear()
        bot.AGENT_INFLIGHT.clear()
        bot.AGENT_LATEST.clear()
        bot.AGENT_OUTBOX.clear()
    SENT[:] = []

    print(f"--- 场景: {expect}")
    print(f"    好友【{sender}】发来: {msg}")
    t0 = time.time()
    sig = (sender, msg, 0, 0)
    job = bot.submit_agent_job(sender, msg, sig, cfg, source="e2e")
    if not job:
        print("    ❌ 提交失败")
        all_ok = False
        continue

    # 模拟主循环心跳：每 200ms 取一次结果
    sent_any = False
    deadline = time.time() + 60
    while time.time() < deadline:
        if bot.poll_agent_results(hwnd=1, rect=(0, 0, 1100, 750), layout={}):
            sent_any = True
            break
        with bot.AGENT_LOCK:
            done = not bot.AGENT_RESULTS and not bot.AGENT_INFLIGHT
        if done:
            # 再 poll 一次把 outbox / ignore 分支跑完
            bot.poll_agent_results(hwnd=1, rect=(0, 0, 1100, 750), layout={})
            break
        time.sleep(0.2)

    cost = time.time() - t0
    print(f"    耗时 {cost:.1f}s")

    if sent_any:
        for tgt, txt in SENT:
            print(f"    📤 发出 -> 【{tgt}】: {txt}")
    elif bot.AGENT_OUTBOX:
        e = bot.AGENT_OUTBOX[0]
        print(f"    ⏳ 延时排队 -> 【{e['target']}】 {e['text']}  (+{e['due_ts']-time.time():.0f}s)")
    else:
        print(f"    🙊 判定不回（已登记签名: {bot.LAST_PROCESSED_SIGNATURE.get(sender) is not None}）")

    # 断言
    if "应该 ignore" in expect:
        ok = (len(SENT) == 0 and not bot.AGENT_OUTBOX)
    else:
        ok = (len(SENT) == 1)
    print(f"    判定: {'✅ 符合预期' if ok else '❌ 不符合预期'}\n")
    all_ok = all_ok and ok
    time.sleep(0.5)

print("=" * 70)
if all_ok:
    print("✅ 全链路联调通过：提交 -> 后台决策 -> 取回 -> 发送/不发 全部正常")
else:
    print("⚠️ 部分场景不符合预期，见上方明细")
sys.exit(0 if all_ok else 1)
