# -*- coding: utf-8 -*-
"""
agent_client.py —— 微信自动回复的「Agent 大脑」（A2A 协议客户端）

把原来的「直连大模型 /chat/completions」换成「调用本机 DSH Agent」：

    POST http://127.0.0.1:44708/a2a      (JSON-RPC 2.0, method = SendMessage)

每个联系人分配一个独立的 contextId，DSH 侧会为这个 contextId 自动建一个
持久会话，所以「同一个人的聊天记忆」由 agent 自己维护，我们只发最新那一句。

对外只有一个方法：

    decision = AgentClient(cfg).ask(context_id, prompt)
    # -> {"action": "reply|ignore|defer",
    #     "text":    "要发出去的那句话",
    #     "urgency": "now|soon|later",
    #     "reason":  "一句话理由"}

设计红线（用户明确要求）：
  * agent 不可用 / 返回不可解析 —— 记 ERROR 日志 + 回一句固定的可爱话术兜底，
    绝不静默丢弃，也绝不回退到老的大模型直连。
  * 只输出一行 JSON，不允许多余解释。

自检：  python agent_client.py
"""

import hashlib
import json
import os
import random
import re
import sys
import time
import uuid

try:
    import requests
except Exception:  # requests 是项目硬依赖，这里只是保险，避免 import 期直接崩
    requests = None


# =============================================================================
# 常量
# =============================================================================
DEFAULT_URL = "http://127.0.0.1:44708/a2a"
DEFAULT_CARD_URL = "http://127.0.0.1:44708/.well-known/agent-card.json"

DEFAULT_FALLBACK_REPLIES = [
    "嘿嘿，我这会儿有点懵，等下再聊~",
    "啊呀，脑子突然卡壳了，晚点回你哈",
    "我这边有点小状况，一会儿再来找你~",
    "等等我，手头有点事，马上回来！",
]

CONTRACT = """你是微信自动回复代理，代替账号主人回复好友消息。
只输出一行 JSON，不要 markdown 代码块，不要解释，不要任何多余文字：
{"action":"reply|ignore|defer","text":"回复内容","urgency":"now|soon|later","reason":"一句话理由"}
规则：
- action=reply：正常回复，text 写要发出去的那句话
- action=ignore：不需要回复。**以下情况一律 ignore**：
  · 广告、砍价链接、群发拉人头、系统消息
  · 对方说「晚安 / 我先睡了 / 先这样 / 不聊了 / 改天再说 / 拜拜」这类收尾话
  · 对方只是点赞、发个表情、发个「嗯/哦/好」，没有实际内容
  · 纯通知类（「我到楼下了」自己知道就行，除非明显在等你回应）
- action=defer：该回但现在不合适（对方在忙、话题需要停顿）
- urgency=now 立即发；soon 30-90 秒后发；later 3-15 分钟后发
- text 只写要发出去的话：不要加引号、不要加说明、不要带 [AI] 之类前缀
- 严禁透露自己是 AI 或机器人"""

ACTION_SET = {"reply", "ignore", "defer"}
URGENCY_SET = {"now", "soon", "later"}


# =============================================================================
# 工具函数
# =============================================================================
def build_prompt(sender, message, persona="", now_time=None):
    """拼出这一轮发给 agent 的完整文本（含契约 + 上下文）。"""
    now_time = now_time or time.strftime("%H:%M")
    persona = (persona or "").strip() or "幽默风趣、随和自然的真实好友"
    return (
        f"{CONTRACT}\n\n"
        f"【当前时间】{now_time}\n"
        f"【联系人】{sender}\n"
        f"【你的人设】{persona}\n"
        f"【对方刚发来】{message}\n"
    )


def _first_json_object(text):
    """从一段可能夹着解释/markdown 的文字里，抠出第一个完整的 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def _clean_text(t):
    """清掉模型偶尔带出来的引号、代码围栏、'回复：' 前缀。"""
    if not t:
        return ""
    t = str(t).strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    t = re.sub(r"^(回复|答复|回复内容|text)\s*[:：]\s*", "", t)
    if len(t) >= 2 and t[0] in "\"'“”‘’" and t[-1] in "\"'“”‘’":
        t = t[1:-1].strip()
    return t


# =============================================================================
# AgentClient
# =============================================================================
class AgentClient(object):
    def __init__(self, cfg=None):
        cfg = cfg or {}
        agent_cfg = cfg.get("agent") or {}

        self.enabled = bool(agent_cfg.get("enabled", True))
        self.url = str(agent_cfg.get("url") or DEFAULT_URL).strip()
        self.card_url = str(agent_cfg.get("card_url") or "").strip() or self._derive_card_url(self.url)
        self.context_prefix = str(agent_cfg.get("context_prefix") or "wx-")
        self.timeout = float(agent_cfg.get("timeout_seconds") or 180)
        self.connect_timeout = float(agent_cfg.get("connect_timeout_seconds") or 5)
        self.retries = max(0, int(agent_cfg.get("retries", 1)))
        self.max_defer_seconds = float(agent_cfg.get("max_defer_seconds") or 900)

        replies = agent_cfg.get("fallback_replies")
        if isinstance(replies, list):
            replies = [str(r).strip() for r in replies if str(r).strip()]
        else:
            replies = []
        self.fallback_replies = replies or list(DEFAULT_FALLBACK_REPLIES)

        self.logger = None          # 由主程序注入 log()，默认 print
        self.last_error = None
        self._mapping_path = agent_cfg.get("context_map_file")

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def _derive_card_url(url):
        u = (url or "").rstrip("/")
        if u.endswith("/a2a"):
            u = u[:-4]
        return f"{u}/.well-known/agent-card.json" if u else DEFAULT_CARD_URL

    def context_id_for(self, sender):
        """
        生成 A2A contextId。

        ⚠️ 血泪坑：DSH 的 A2A 执行器用 contextId 直接拼会话目录名
        （`a2a-<contextId>`），中文会被转义成 `a2a-wx-~597D~5144~5F1FA`
        这种形式，导致会话建不起来、每轮都返回 `(no reply)`。
        所以这里强制把联系人名压成纯 ASCII：可读前缀 + 名字的 sha1 前 8 位。
        同一个人永远得到同一个 id，agent 侧的会话记忆照样连续。
        """
        raw = str(sender or "").strip()
        if not raw:
            return f"{self.context_prefix}anonymous"
        # 纯 ASCII 名字（英文/数字/下划线）直接用，可读性好
        if re.fullmatch(r"[A-Za-z0-9_.\-]{1,48}", raw):
            return f"{self.context_prefix}{raw}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        ctx = f"{self.context_prefix}u{digest}"
        self._remember_mapping(ctx, raw)
        return ctx

    def _sanitize_context_id(self, context_id):
        """把任意 contextId 压成纯 ASCII（DSH 的 A2A 会话目录名不能含中文）。"""
        cid = str(context_id or "").strip()
        if not cid:
            return f"{self.context_prefix}anonymous"
        try:
            cid.encode("ascii")
            return cid
        except UnicodeEncodeError:
            digest = hashlib.sha1(cid.encode("utf-8")).hexdigest()[:12]
            safe = f"{self.context_prefix}u{digest}"
            self._log(f"⚠️ [contextId 含非 ASCII 字符] 已自动改写 {cid!r} -> {safe}")
            return safe

    def _remember_mapping(self, ctx, sender):
        """把 hash 后的 contextId 反查表落盘，方便排查「这个会话是谁」。"""
        if self._mapping_path is None:
            return
        try:
            data = {}
            if os.path.exists(self._mapping_path):
                with open(self._mapping_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            if data.get(ctx) == sender:
                return
            data[ctx] = sender
            with open(self._mapping_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _log(self, msg):
        fn = self.logger
        if fn is None:
            try:
                print(msg, flush=True)
            except Exception:
                pass
            return
        try:
            fn(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------ 健康
    def is_available(self, timeout=2.0):
        """轻量探活：GET agent card。不抛异常，只返回 True/False。"""
        if requests is None or not self.enabled:
            return False
        try:
            resp = requests.get(self.card_url, timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------ 主入口
    def ask(self, context_id, prompt, timeout=None):
        """问 agent 要一个决策。永远返回合法 dict，绝不抛异常。"""
        if not self.enabled:
            return self._fallback("agent_disabled")
        if requests is None:
            return self._fallback("requests_missing")

        # 防御：调用方直接传了中文 contextId（DSH 建会话会失败，永远回 (no reply)）
        context_id = self._sanitize_context_id(context_id)

        timeout = timeout or self.timeout
        raw = None
        last_err = None

        for attempt in range(self.retries + 1):
            try:
                raw = self._send(context_id, prompt, timeout)
                break
            except Exception as exc:
                last_err = exc
                if attempt < self.retries:
                    time.sleep(0.8)

        if raw is None:
            return self._fallback(f"transport_error: {last_err}")

        decision = self._parse(raw)
        if decision is None:
            return self._fallback(f"unparseable_reply: {str(raw)[:160]!r}")

        self.last_error = None
        return decision

    # ------------------------------------------------------------------ 传输
    def _send(self, context_id, prompt, timeout):
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "kind": "message",
                    # 关键：contextId 必须放在 message 内部，放 params 层会被忽略并新开会话
                    "contextId": context_id,
                    "parts": [{"kind": "text", "text": prompt}],
                }
            },
        }
        resp = requests.post(
            self.url,
            json=payload,
            timeout=(self.connect_timeout, timeout),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
        except Exception:
            raise RuntimeError(f"非 JSON 响应: {resp.text[:200]}")

        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            raise RuntimeError(f"A2A error: {json.dumps(err, ensure_ascii=False)[:200]}")

        text = self._extract_text(body)
        if not text:
            raise RuntimeError("A2A 返回空文本")
        return text

    @staticmethod
    def _extract_text(body):
        result = (body or {}).get("result") or {}
        task = result.get("task") or {}

        state = str((task.get("status") or {}).get("state") or "")
        if state in ("TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"):
            raise RuntimeError(f"任务未成功: {state}")

        # 1) artifacts 里是最终产物，取最后一个非空 part
        for art in reversed(task.get("artifacts") or []):
            for part in reversed(art.get("parts") or []):
                t = str(part.get("text") or "").strip()
                if t:
                    return t

        # 2) 兜底：status.message
        for part in reversed((task.get("status") or {}).get("message", {}).get("parts") or []):
            t = str(part.get("text") or "").strip()
            if t:
                return t
        return ""

    # ------------------------------------------------------------------ 解析
    def _parse(self, raw):
        if not raw:
            return None

        text = str(raw).strip()
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

        obj = _first_json_object(text)
        if obj is None:
            return None

        action = str(obj.get("action") or "").strip().lower()
        out_text = _clean_text(obj.get("text") or "")

        if action not in ACTION_SET:
            # 模型忘了给 action：有正文就当 reply，没正文就当 ignore
            action = "reply" if out_text else "ignore"

        if action == "reply" and not out_text:
            # 说要回却没内容 —— 视为坏输出，走降级兜底
            return None
        urgency = str(obj.get("urgency") or "").strip().lower()
        if urgency not in URGENCY_SET:
            urgency = "now"

        reason = str(obj.get("reason") or "").strip()[:120]
        return {
            "action": action,
            "text": out_text,
            "urgency": urgency,
            "reason": reason,
        }

    # ------------------------------------------------------------------ 降级
    def _fallback(self, reason):
        self.last_error = reason
        self._log(f"🚨 [Agent 降级] {reason} → 改用固定可爱回复兜底")
        text = random.choice(self.fallback_replies) if self.fallback_replies else DEFAULT_FALLBACK_REPLIES[0]
        return {
            "action": "reply",
            "text": text,
            "urgency": "now",
            "reason": "agent_unavailable",
        }

    # ------------------------------------------------------------------ 延迟
    def urgency_to_seconds(self, urgency):
        """把 now/soon/later 折算成秒（带抖动，别像机器人一样卡点）。"""
        u = str(urgency or "now").strip().lower()
        if u == "soon":
            delay = random.uniform(30, 90)
        elif u == "later":
            delay = random.uniform(180, 900)
        else:
            delay = 0.0
        return min(delay, self.max_defer_seconds)


# =============================================================================
# 自检：python agent_client.py
# =============================================================================
def _selftest():
    try:
        if sys.stdout:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 60)
    print("AgentClient 自检")
    print("=" * 60)

    cfg = {}
    cfg_path = None
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("wechat_config_dev.json", "wechat_config.json"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            cfg_path = p
            break
    if cfg_path:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            print(f"[1/3] 已读取配置: {cfg_path}")
        except Exception as exc:
            print(f"[1/3] 读取配置失败({exc})，改用默认配置")
    else:
        print("[1/3] 未找到配置文件，使用默认配置")

    client = AgentClient(cfg)
    client.logger = lambda m: print("   " + str(m))

    ok = client.is_available()
    print(f"[2/3] 探活 {client.card_url} -> {'OK' if ok else 'FAIL'}")

    if not ok:
        print("\n⚠️  DSH A2A 服务没开。开启方式（二选一）：")
        print("   1) DSH 界面里打开 A2A 插件的服务开关")
        print("   2) curl -s -X POST http://127.0.0.1:44708/a2a/api "
              "-H 'Content-Type: application/json' -d '{\"action\":\"server-enable\"}'")
        print("\n下面只演示降级路径：")
        print("   ", json.dumps(client.ask("wx-selftest", "你好"), ensure_ascii=False))
        return 1

    prompt = build_prompt("自检好友", "在吗？晚上一起吃饭不", "死党互损模式：幽默随和、接地气")
    print("[3/3] 发一条真实消息给 agent ...")
    t0 = time.time()
    decision = client.ask(client.context_id_for("selftest"), prompt)
    cost = time.time() - t0
    print(f"   耗时 {cost:.1f}s")
    print("   " + json.dumps(decision, ensure_ascii=False))
    print()
    if decision.get("reason") == "agent_unavailable":
        print("❌ 自检失败：走了降级路径")
        return 1
    print("✅ 自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
