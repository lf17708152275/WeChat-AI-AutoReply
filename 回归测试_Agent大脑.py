# -*- coding: utf-8 -*-
"""回归测试_Agent大脑.py —— P0 异步总线 + P1 Agent 接入的离线回归表

不碰微信、不连网络、不发真消息：全部用 FakeAgentClient + 打桩 send_reply_instant。
运行:  python 回归测试_Agent大脑.py     全部 PASS = 零回退
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

# ---------------------------------------------------------------------------
# 给缺失的重型依赖打桩（本机可能没装 numpy / pyperclip / rapidocr）
# ---------------------------------------------------------------------------
def _stub(name, **attrs):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _FakeNDArray(object):
    def __init__(self, *a, **k):
        self.shape = (0,)
        self.size = 0

    def __getitem__(self, item):
        return _FakeNDArray()

    def tobytes(self):
        return b""

    def astype(self, *a, **k):
        return self

    def max(self):
        return 0

    def __sub__(self, other):
        return self

    def __add__(self, other):
        return self

    def __gt__(self, other):
        return self

    def __ge__(self, other):
        return self


_np = _stub("numpy")
_np.array = lambda *a, **k: _FakeNDArray()
_np.sum = lambda *a, **k: 0
_np.uint8 = "uint8"

_stub("pyperclip", copy=lambda *a, **k: True, paste=lambda: "")


class _FakeOCR(object):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return [], None


_stub("rapidocr_onnxruntime", RapidOCR=_FakeOCR)

import wechat_ai_bot_dev as bot  # noqa: E402
import agent_client  # noqa: E402

fails = []
LOG_LINES = []


def check(label, got, expect):
    ok = (got == expect)
    if not ok:
        fails.append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: 实际={got!r} 期望={expect!r}")


def check_true(label, cond, detail=""):
    if not cond:
        fails.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")


def wait_results(timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with bot.AGENT_LOCK:
            if bot.AGENT_RESULTS:
                return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class FakeClient(object):
    def __init__(self, decision=None):
        self.decision = decision or {"action": "reply", "text": "好呀", "urgency": "now", "reason": "t"}
        self.calls = []

    def context_id_for(self, sender):
        return f"wx-{sender}"

    def ask(self, context_id, prompt, timeout=None, target=""):
        self.calls.append((context_id, prompt))
        d = self.decision
        return dict(d) if isinstance(d, dict) else d

    def urgency_to_seconds(self, urgency):
        return agent_client.AgentClient.urgency_to_seconds(
            types.SimpleNamespace(max_defer_seconds=900.0), urgency
        )


SENT = []


def fake_send(hwnd, rect, layout, target, final_reply):
    SENT.append((target, final_reply))
    return True


def reset_bus():
    with bot.AGENT_LOCK:
        bot.AGENT_RESULTS.clear()
        bot.AGENT_PENDING.clear()
        bot.AGENT_INFLIGHT.clear()
        bot.AGENT_LATEST.clear()
        bot.AGENT_OUTBOX.clear()
    SENT[:] = []
    LOG_LINES[:] = []


bot.log = lambda m: LOG_LINES.append(str(m))
bot.send_reply_instant = fake_send
bot.load_config = lambda: {
    "agent": {"enabled": True, "url": "http://fake", "context_prefix": "wx-"},
    "reply_delay_seconds": 0.0,
    "include_prefix": False,
}

# ===========================================================================
print("=== 1. JSON 合约解析（_parse） ===")
# ===========================================================================
pc = agent_client.AgentClient({"agent": {"enabled": False}})
pc.logger = lambda m: None

check("标准 JSON",
      pc._parse('{"action":"reply","text":"在的","urgency":"now","reason":"r"}'),
      {"action": "reply", "text": "在的", "segments": ["在的"], "stickers": [], "urgency": "now", "reason": "r"})

check("markdown 围栏包裹",
      pc._parse('```json\n{"action":"ignore","text":"","urgency":"now","reason":"广告"}\n```'),
      {"action": "ignore", "text": "", "segments": [], "stickers": [], "urgency": "now", "reason": "广告"})

check("前后夹带废话",
      pc._parse('好的，我判断如下：\n{"action":"reply","text":"来了","urgency":"soon","reason":"x"}\n以上。'),
      {"action": "reply", "text": "来了", "segments": ["来了"], "stickers": [], "urgency": "soon", "reason": "x"})

check("缺 action 但有正文 → reply",
      pc._parse('{"text":"哈哈哈","urgency":"now"}')["action"], "reply")

check("缺 action 且无正文 → ignore",
      pc._parse('{"text":"","urgency":"now"}')["action"], "ignore")

check("action=reply 但 text 空 → None(触发兜底)",
      pc._parse('{"action":"reply","text":"","urgency":"now"}'), None)

check("非法 action 有正文 → reply",
      pc._parse('{"action":"say","text":"嗯","urgency":"now"}')["action"], "reply")

check("非法 urgency → now",
      pc._parse('{"action":"reply","text":"嗯","urgency":"someday"}')["urgency"], "now")

check("text 首尾引号被剥掉",
      pc._parse('{"action":"reply","text":"\\"你好呀\\"","urgency":"now"}')["text"], "你好呀")

check("text 带 '回复：' 前缀被剥掉",
      pc._parse('{"action":"reply","text":"回复：收到","urgency":"now"}')["text"], "收到")

check("完全不是 JSON → None", pc._parse("我建议回复：你好"), None)
check("空串 → None", pc._parse(""), None)

# ===========================================================================
print("=== 1.5 多段回复解析（segments） ===")
# ===========================================================================
multi = pc._parse('{"action":"reply","segments":["你吃了吗","我刚下班"],"urgency":"now","reason":"r"}')
check("segments 数组被解析", multi["segments"], ["你吃了吗", "我刚下班"])
check("text 兼容字段是各段拼接", multi["text"], "你吃了吗\n我刚下班")
check("单段 text 也归一成 segments", pc._parse('{"action":"reply","text":"好"}')["segments"], ["好"])
check("text 里 ||| 兼容拆分", pc._parse('{"action":"reply","text":"第一句|||第二句"}')["segments"],
      ["第一句", "第二句"])
check("segments 里混对象也兼容",
      pc._parse('{"action":"reply","segments":[{"text":"甲"},{"text":"乙"}]}')["segments"], ["甲", "乙"])
check("最多 4 段",
      len(pc._parse('{"action":"reply","segments":["1","2","3","4","5","6"]}')["segments"]), 4)
check("相邻重复段去重",
      pc._parse('{"action":"reply","segments":["在的","在的","咋了"]}')["segments"], ["在的", "咋了"])
check("reply 且 segments 全空 → None",
      pc._parse('{"action":"reply","segments":["","  "]}'), None)

# ===========================================================================
print("=== 2. urgency → 秒 ===")
# ===========================================================================
now_s = pc.urgency_to_seconds("now")
soon_s = pc.urgency_to_seconds("soon")
later_s = pc.urgency_to_seconds("later")
check("now = 0", now_s, 0.0)
check_true("soon 落在 30-90s", 30 <= soon_s <= 90, f"got {soon_s}")
check_true("later 落在 180-900s", 180 <= later_s <= 900, f"got {later_s}")
check("未知 urgency 当 now", pc.urgency_to_seconds("???"), 0.0)
pc_small = agent_client.AgentClient({"agent": {"max_defer_seconds": 120}})
check_true("max_defer_seconds 封顶", pc_small.urgency_to_seconds("later") <= 120, "未封顶")

# ===========================================================================
print("=== 3. 降级兜底（agent 不可用） ===")
# ===========================================================================
dead = agent_client.AgentClient({"agent": {"enabled": True, "url": "http://127.0.0.1:1/a2a",
                                           "retries": 0, "connect_timeout_seconds": 0.2,
                                           "timeout_seconds": 0.4}})
dead.logger = lambda m: None
fb = dead.ask("wx-x", "你好")
check("action 强制 reply", fb["action"], "reply")
check("reason=agent_unavailable", fb["reason"], "agent_unavailable")
check_true("text 落在可爱回复池里", fb["text"] in dead.fallback_replies, fb["text"])
check("urgency=now", fb["urgency"], "now")
check_true("last_error 有记录", bool(dead.last_error), str(dead.last_error))

off = agent_client.AgentClient({"agent": {"enabled": False}})
off.logger = lambda m: None
check("enabled=false 直接兜底", off.ask("wx-x", "hi")["reason"], "agent_unavailable")

# ---------------------------------------------------------------------------
print("=== 3.5 A2A 响应解析 / 关系端点路由 ===")
# @hanphone/dsh-a2a 把文本切成逐字 chunk，必须拼起来
_chunked = {
    "result": {
        "task": {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "result",
                           "parts": [{"text": "行"}, {"text": "啊"}, {"text": " 这就"},
                                     {"text": "来"}]}],
        }
    }
}
check("逐字 parts 能拼成整句", agent_client.AgentClient._extract_text(_chunked), "行啊 这就来")

_old_style = {"result": "好 马上"}
check("老插件 result 纯文本也能兜住", agent_client.AgentClient._extract_text(_old_style), "好 马上")

_failed = {"result": {"task": {"status": {"state": "TASK_STATE_FAILED"}, "artifacts": []}}}
try:
    agent_client.AgentClient._extract_text(_failed)
    check("失败任务应抛错", "没抛", "抛错")
except RuntimeError as e:
    check_true("失败任务抛错", "TASK_STATE_FAILED" in str(e), str(e))

# 关系 → 端点
rc = agent_client.AgentClient({
    "agent": {"url": "http://x/a2a", "relation_urls": {"家人": "http://x/a2a/family"}},
    "contacts": [{"remark": "张三", "relation": "家人"}, {"remark": "同事甲", "relation": "同事"}],
})
check("家人走专属端点", rc.url_for("张三"), "http://x/a2a/family")
check("没配的关系走默认", rc.url_for("同事甲"), "http://x/a2a")
check("不在通讯录走默认", rc.url_for("路人"), "http://x/a2a")

# ===========================================================================
print("=== 4. 异步总线：reply + now 立即发 ===")
# ===========================================================================
reset_bus()
fc = FakeClient({"action": "reply", "text": "在啊，吃啥？", "urgency": "now", "reason": "约饭"})
bot.get_agent_client = lambda cfg=None: fc

job = bot.submit_agent_job("张三", "晚上一起吃饭不", ("sig1",), None, source="test")
check_true("submit 返回 job_id", bool(job), str(job))
check_true("agent_busy_for 立刻为 True", bot.agent_busy_for("张三", ("sig1",)))
check_true("同一条消息 busy 为 True", bot.agent_busy_for("张三", ("sig1",)))

check_true("等待后台结果", wait_results())
sent_any = bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("已发出 1 条", len(SENT), 1)
check("发给了张三", SENT[0][0], "张三")
check("内容正确", SENT[0][1], "在啊，吃啥？")
check("poll 返回 True", sent_any, True)
check("签名已登记", bot.LAST_PROCESSED_SIGNATURE.get("张三"), ("sig1",))
check_true("发完不再 busy", not bot.agent_busy_for("张三"))
check("结果邮箱已清空", len(bot.AGENT_RESULTS), 0)

# ===========================================================================
print("=== 4.5 多段回复：逐条连发 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
bot.load_config = lambda: {
    "agent": {"enabled": True, "url": "http://fake", "context_prefix": "wx-"},
    "reply_delay_seconds": 0.0,
    "include_prefix": False,
    "segment_delay_min_seconds": 0.0,
    "segment_delay_span_seconds": 0.0,
}
fc.decision = {"action": "reply", "segments": ["刚看到", "我在吃饭", "一会儿回你"],
               "urgency": "now", "reason": "连发"}
bot.submit_agent_job("多段甲", "在吗", ("sigM",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("三段都发出去了", len(SENT), 3)
check("第一段内容", SENT[0][1] if SENT else None, "刚看到")
check("第二段内容", SENT[1][1] if len(SENT) > 1 else None, "我在吃饭")
check("第三段内容", SENT[2][1] if len(SENT) > 2 else None, "一会儿回你")
check("三段发给同一个人", {r[0] for r in SENT}, {"多段甲"})
check("签名已登记", bot.LAST_PROCESSED_SIGNATURE.get("多段甲"), ("sigM",))

# 兼容旧契约：只有 text 时仍然是一条
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
fc.decision = {"action": "reply", "text": "只有一条", "urgency": "now", "reason": ""}
bot.submit_agent_job("多段乙", "在吗", ("sigN",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("旧契约仍只发一条", len(SENT), 1)
check("内容正确", SENT[0][1] if SENT else None, "只有一条")

# 段数上限：超过 MAX_REPLY_SEGMENTS 的部分丢弃
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
fc.decision = {"action": "reply", "segments": ["1", "2", "3", "4", "5", "6"],
               "urgency": "now", "reason": ""}
bot.submit_agent_job("多段丙", "在吗", ("sigO",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("最多发 4 段", len(SENT), bot.MAX_REPLY_SEGMENTS)

# 复原测试配置
bot.load_config = lambda: {
    "agent": {"enabled": True, "url": "http://fake", "context_prefix": "wx-"},
    "reply_delay_seconds": 0.0,
    "include_prefix": False,
}

# ===========================================================================
print("=== 5. ignore：不发消息，但登记签名防空转 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
fc.decision = {"action": "ignore", "text": "", "urgency": "now", "reason": "拼多多广告"}
bot.submit_agent_job("李四", "【拼多多】砍一刀", ("sig2",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("没发任何消息", len(SENT), 0)
check("签名已登记（防重复处理）", bot.LAST_PROCESSED_SIGNATURE.get("李四"), ("sig2",))

# ===========================================================================
print("=== 6. defer：进延时队列，不当场发 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
fc.decision = {"action": "defer", "text": "明天再说吧", "urgency": "soon", "reason": "对方在忙"}
bot.submit_agent_job("王五", "在吗", ("sig3",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("没当场发", len(SENT), 0)
check("进了 outbox", len(bot.AGENT_OUTBOX), 1)
check("outbox 目标是王五", bot.AGENT_OUTBOX[0]["target"], "王五")
check_true("到期时间在未来", bot.AGENT_OUTBOX[0]["due_ts"] > time.time())
check_true("busy_for 认为还在排队", bot.agent_busy_for("王五"))

# 手动把时间拨到过去，模拟到点
bot.AGENT_OUTBOX[0]["due_ts"] = time.time() - 1
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("到点后发出", len(SENT), 1)
check("内容是 defer 的 text", SENT[0][1], "明天再说吧")
check("outbox 已清空", len(bot.AGENT_OUTBOX), 0)

# ===========================================================================
print("=== 7. defer 无正文：只记日志，不排队 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
fc.decision = {"action": "defer", "text": "", "urgency": "later", "reason": "深夜"}
bot.submit_agent_job("赵六", "睡了吗", ("sig4",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("没发消息", len(SENT), 0)
check("没进 outbox", len(bot.AGENT_OUTBOX), 0)
check("签名已登记", bot.LAST_PROCESSED_SIGNATURE.get("赵六"), ("sig4",))

# ===========================================================================
print("=== 8. 新消息撤销旧延时计划 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
fc.decision = {"action": "defer", "text": "等下说", "urgency": "later", "reason": "忙"}
bot.submit_agent_job("钱七", "第一句", ("sigA",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("旧计划已排队", len(bot.AGENT_OUTBOX), 1)

fc.decision = {"action": "reply", "text": "来了来了", "urgency": "now", "reason": "空了"}
bot.submit_agent_job("钱七", "第二句", ("sigB",), None)
check("新消息撤销了旧计划", len(bot.AGENT_OUTBOX), 0)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("只发了新回复", len(SENT), 1)
check("内容是第二句的回复", SENT[0][1], "来了来了")

# ===========================================================================
print("=== 9. 过期结果丢弃（旧决策不覆盖新决策） ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
bot.get_agent_client = lambda cfg=None: fc


class SlowClient(FakeClient):
    def __init__(self):
        FakeClient.__init__(self, {"action": "reply", "text": "旧回复", "urgency": "now", "reason": ""})
        self.n = 0

    def ask(self, context_id, prompt, timeout=None, target=""):
        self.n += 1
        n = self.n
        time.sleep(0.25 if n == 1 else 0.0)   # 第一次慢，第二次快
        return {"action": "reply", "text": ("旧回复" if n == 1 else "新回复"),
                "urgency": "now", "reason": ""}


sc = SlowClient()
bot.get_agent_client = lambda cfg=None: sc
bot.submit_agent_job("孙八", "旧消息", ("old",), None)
time.sleep(0.05)
bot.submit_agent_job("孙八", "新消息", ("new",), None)   # 覆盖 inflight
time.sleep(0.5)
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("只发 1 条（旧结果被丢弃）", len(SENT), 1)
check("发的是新回复", SENT[0][1] if SENT else None, "新回复")

# ===========================================================================
print("=== 10. 发送失败 → 2 秒后补偿重试 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
fc.decision = {"action": "reply", "text": "你好呀", "urgency": "now", "reason": ""}
bot.get_agent_client = lambda cfg=None: fc
bot.send_reply_instant = lambda *a, **k: False     # 全部失败
bot.submit_agent_job("周九", "在吗", ("sigF",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("第一次失败没算发出去", len(SENT), 0)
check("进了重试队列", len(bot.AGENT_OUTBOX), 1)
check("重试标记 attempt=1", bot.AGENT_OUTBOX[0]["attempt"], 1)

bot.AGENT_OUTBOX[0]["due_ts"] = time.time() - 1
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("第二次也失败", len(SENT), 0)
check("重试队列已清空（熔断）", len(bot.AGENT_OUTBOX), 0)
check("熔断后登记签名防死循环", bot.LAST_PROCESSED_SIGNATURE.get("周九"), ("sigF",))

bot.send_reply_instant = fake_send

# ===========================================================================
print("=== 11. 发送成功才登记签名 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
bot.get_agent_client = lambda cfg=None: fc
fc.decision = {"action": "reply", "text": "嗯嗯", "urgency": "now", "reason": ""}
bot.submit_agent_job("吴十", "在吗", ("sigG",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("发出去了", len(SENT), 1)
check("签名已登记", bot.LAST_PROCESSED_SIGNATURE.get("吴十"), ("sigG",))

# ===========================================================================
print("=== 12. 前缀配置生效 ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
bot.load_config = lambda: {
    "agent": {"enabled": True, "url": "http://fake", "context_prefix": "wx-"},
    "reply_delay_seconds": 0.0,
    "include_prefix": True,
    "reply_prefix": "[AI] ",
}
bot.get_agent_client = lambda cfg=None: fc
fc.decision = {"action": "reply", "text": "收到", "urgency": "now", "reason": ""}
bot.submit_agent_job("郑十一", "在吗", ("sigH",), None)
check_true("等待后台结果", wait_results())
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check("前缀已拼接", SENT[0][1] if SENT else None, "[AI] 收到")

# ===========================================================================
print("=== 13. 同一好友重复提交只在飞一个（防止每心跳都问 agent） ===")
# ===========================================================================
reset_bus()
bot.LAST_PROCESSED_SIGNATURE.clear()
bot.load_config = lambda: {"agent": {"enabled": True, "url": "http://fake", "context_prefix": "wx-"},
                           "reply_delay_seconds": 0.0, "include_prefix": False}
slow2 = SlowClient()
slow2.n = 100      # 让它走快分支
bot.get_agent_client = lambda cfg=None: slow2
bot.submit_agent_job("冯十二", "消息", ("sigZ",), None)
check_true("在飞期间 busy_for 为 True", bot.agent_busy_for("冯十二", ("sigZ",)))
check_true("不同签名不拦截（新消息要重新决策）", not bot.agent_busy_for("冯十二", ("sigOther",)))
wait_results()
bot.poll_agent_results(hwnd=1, rect=(0, 0, 100, 100), layout={})
check_true("处理完 busy_for 为 False", not bot.agent_busy_for("冯十二"))

# ===========================================================================
print("=== 14. AgentClient 不会被 requests 缺失拖垮 ===")
# ===========================================================================
saved_requests = agent_client.requests
agent_client.requests = None
try:
    c = agent_client.AgentClient({"agent": {"enabled": True}})
    c.logger = lambda m: None
    check("requests=None 时走兜底", c.ask("wx-x", "hi")["reason"], "agent_unavailable")
    check("is_available 返回 False", c.is_available(), False)
finally:
    agent_client.requests = saved_requests

# ===========================================================================
# 会话冲突自愈（DSH "session ... already exists" 会被当成回复文本返回）
# ===========================================================================
import tempfile

_tmpdir = tempfile.mkdtemp()
_rot_cfg = {"agent": {
    "enabled": True,
    "url": "http://fake/a2a",
    "context_map_file": os.path.join(_tmpdir, "map.json"),
    "context_rot_file": os.path.join(_tmpdir, "rot.json"),
}}
_c = agent_client.AgentClient(_rot_cfg)
_c.logger = lambda m: None
_seq = []


def _fake_send(ctx, prompt, timeout):
    _seq.append(ctx)
    if ctx == "wx-abc":
        return 'DSH agent executor error: session "a2a-wx-abc" already exists'
    return '{"action":"reply","text":"好","urgency":"now","reason":"ok"}'


_c._send = _fake_send
_d = _c.ask("wx-abc", "在吗")
check("会话冲突后能自愈并拿到决策", _d.get("text"), "好")
check("自愈时换了新 contextId", _seq, ["wx-abc", "wx-abc-r2"])
_seq.clear()
_d2 = _c.ask("wx-abc", "再说一句")
check("第二次直接用轮换后的 id（不再撞墙）", _seq, ["wx-abc-r2"])
check("轮换表落盘", json.load(open(os.path.join(_tmpdir, "rot.json")))["wx-abc"], 2)
check("冲突正则能识别真实报错",
      bool(agent_client.SESSION_CONFLICT_RE.search(
          'DSH agent executor error: session "a2a-w-x" already exists')), True)
check("正常回复不会被误判成冲突",
      bool(agent_client.SESSION_CONFLICT_RE.search('{"action":"reply","text":"好"}')), False)

# ===========================================================================
# 会话被占用自愈（DSH 重启后遗留的写句柄不会释放）
# 真实报错是任务失败，原因藏在 status.message 里：
#   session "a2a-..." is already owned by an active write handle
# 以前 _extract_text 只抛 "任务未成功: TASK_STATE_FAILED"，上层只能当网络错误兜底，
# 导致 bot 永远回"脑子卡壳"。
# ===========================================================================
_owned_cfg = {"agent": {
    "enabled": True,
    "url": "http://fake/a2a",
    "context_map_file": os.path.join(_tmpdir, "map_owned.json"),
    "context_rot_file": os.path.join(_tmpdir, "rot_owned.json"),
}}
_oc = agent_client.AgentClient(_owned_cfg)
_oc.logger = lambda m: None
_oseq = []


def _fake_send_owned(ctx, prompt, timeout):
    _oseq.append(ctx)
    if ctx == "wx-own":
        raise RuntimeError(
            '任务未成功: TASK_STATE_FAILED (session "a2a-wx-own" '
            'is already owned by an active write handle)')
    return '{"action":"reply","segments":["来了","刚忙完"],"urgency":"now","reason":"ok"}'


_oc._send = _fake_send_owned
_od = _oc.ask("wx-own", "在吗")
check("写句柄被占用后能自愈并拿到决策", _od.get("segments"), ["来了", "刚忙完"])
check("被占用自愈时换了新 contextId", _oseq, ["wx-own", "wx-own", "wx-own-r2"])
check("被占用正则能识别真实报错",
      bool(agent_client.SESSION_OWNED_RE.search(
          'session "a2a-w-x" is already owned by an active write handle')), True)
check("正常回复不会被误判成被占用",
      bool(agent_client.SESSION_OWNED_RE.search('{"action":"reply","text":"好"}')), False)

# 失败原因要从 status.message 带进异常，否则上层无法判断该走哪种自愈
_failed_body = {"result": {"task": {"status": {
    "state": "TASK_STATE_FAILED",
    "message": {"role": "ROLE_AGENT", "parts": [
        {"text": 'session "x" is already owned by an active write handle'}]},
}}}}
try:
    agent_client.AgentClient._extract_text(_failed_body)
    _detail = ""
except RuntimeError as exc:
    _detail = str(exc)
check("失败任务的 status.message 会带进异常",
      "already owned by an active write handle" in _detail, True)


# ===========================================================================
print("=== 5.5 表情回复解析（DSH 表情插件）===")
_sticker_url = "http://127.0.0.1:25675/x-nekowhale-stickers/s/4b1576f9a927.gif"
_plain = agent_client.AgentClient.__new__(agent_client.AgentClient)

# 1) 模型按契约把表情放进 sticker 字段
_d1 = _plain._parse('{"action":"reply","segments":["刚摸鱼回来","歇会儿吧"],'
                    '"sticker":"![摸鱼](%s)","urgency":"now","reason":"x"}' % _sticker_url)
check("JSON 里 sticker 字段被解析出来", _d1.get("stickers"), [_sticker_url])
check("表情不会混进正文", _d1.get("segments"), ["刚摸鱼回来", "歇会儿吧"])

# 2) 表情 markdown 直接写在 segments 里也要能剥出来
_d2 = _plain._parse('{"action":"reply","segments":["哈哈哈 ![笑](%s)"],"urgency":"now"}' % _sticker_url)
check("正文里的表情 markdown 被剥离", _d2.get("segments"), ["哈哈哈"])
check("剥离出来的图片进了 stickers", _d2.get("stickers"), [_sticker_url])

# 3) 只发表情、不写字也算有效回复（不能走兜底话术）
_d3 = _plain._parse('{"action":"reply","sticker":"![摸鱼](%s)","urgency":"now"}' % _sticker_url)
check("只有表情没有正文也能成决策", _d3.get("stickers"), [_sticker_url])
check("只有表情时正文为空", _d3.get("segments"), [])

# 4) 模型忘记 JSON、直接回聊天文字 + 表情（实测出现过）也要救回来
_d4 = _plain._parse("今天心情好，也要开心得像布丁一样晃起来。\n\n![布丁](%s)" % _sticker_url)
check("纯文字回复会兜住成 reply", _d4.get("action"), "reply")
check("纯文字里的表情被识别", _d4.get("stickers"), [_sticker_url])
check("纯文字正文干净", _d4.get("segments"), ["今天心情好，也要开心得像布丁一样晃起来。"])

# 5) 普通链接不能被误认成表情
_d5 = _plain._parse('{"action":"reply","text":"看这个 https://example.com/x.png","urgency":"now"}')
check("普通链接不会被当表情", _d5.get("stickers"), [])

# 6) 决策层再兜一层：只放行表情插件路径 + 本机/局域网地址
import wechat_ai_bot_dev as _wb
check("决策层放行本机表情链接", _wb._decision_stickers({"stickers": [_sticker_url]}), [_sticker_url])
check("决策层拦掉外站图片",
      _wb._decision_stickers({"stickers": ["http://evil.example.com/x-nekowhale-stickers/s/a.gif"]}),
      ["http://evil.example.com/x-nekowhale-stickers/s/a.gif"])
check("下载器拒绝外站地址", _wb.download_sticker("http://evil.example.com/x-nekowhale-stickers/s/a.gif"), None)
check("下载器能拿到表情文件（本机插件）",
      bool(_wb.download_sticker(_sticker_url)), True)
_dl = _wb.download_sticker(_sticker_url)
if _dl:
    import os as _os
    _os.remove(_dl)


# ===========================================================================
print()
if fails:
    print(f"❌ 回归失败 {len(fails)} 项:")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部用例 PASS，零回退")
