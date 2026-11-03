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
{"action":"reply|ignore|defer","segments":["第一条","第二条"],"urgency":"now|soon|later","reason":"一句话理由"}
规则：
- action=reply：正常回复。segments 是要**连发的 1-3 条消息**，按顺序一条条发出去，像真人连发
  · 每条 5-30 个汉字，一条只说一件事；想说的多就分条，别把一大段挤进一条
  · 只有一条时可以只写 "text":"..."；多段必须用 "segments"（不要用 ||| 之类的分隔符）
- action=ignore：不需要回复。**以下情况一律 ignore**：
  · 广告、砍价链接、群发拉人头、系统消息
  · 对方说「晚安 / 我先睡了 / 先这样 / 不聊了 / 改天再说 / 拜拜」这类收尾话
  · 对方只是点赞、发个表情、发个「嗯/哦/好」，没有实际内容
  · 纯通知类（「我到楼下了」自己知道就行，除非明显在等你回应）
- action=defer：该回但现在不合适（对方在忙、话题需要停顿）；segments 照常写，到点再发
- urgency=now 立即发；soon 30-90 秒后发；later 3-15 分钟后发
- segments/text 只写要发出去的话：不要加引号、不要加说明、不要带 [AI] 之类前缀、不要带序号
- 不要用书面连接词（首先/其次/此外/总之）、不要列点、不要括号写动作、不要句末客套
- 想斗图/发表情时：可以调用表情工具（sticker_search / sticker_send），把工具返回的那一行
  markdown 图片原样放在 JSON 的 "sticker" 字段里（形如 "![摸鱼](http://127.0.0.1:25675/x-nekowhale-stickers/s/xxxx.gif)"）
  · 一条回复最多 1 张表情，别每句都配图；图片链接不要混进 segments 正文
  · 表情是锦上添花：对方在难过/说正事时不要发表情
  · 注意：即使要发表情，也**只输出那一行 JSON**，绝对不要直接输出聊天文字或裸 markdown
- 严禁透露自己是 AI 或机器人"""

# 一次回复最多拆几条：太多会像刷屏，真人连发 1-3 条最自然，留 4 的余量
MAX_SEGMENTS = 4
SEGMENT_SPLIT_RE = re.compile(r"\s*\|\|\|\s*")

# 表情包：DSH 表情插件吐出来的是 markdown 图片，bot 会下载后当图片消息发出去
MAX_STICKERS = 2
STICKER_MD_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
STICKER_BARE_RE = re.compile(r"(https?://[^\s)\]\[]+/x-nekowhale-stickers/[^\s)\]\[]+)")


def split_stickers(text):
    """把一段文字里的表情 markdown / 裸贴纸链接剥出来。

    返回 (清干净的文字, [贴纸 url...])。只认 http(s) 且带表情插件路径的链接，
    避免把普通图片/链接当表情。
    """
    urls = []

    def take(m):
        url = m.group(1).strip()
        if url not in urls:
            urls.append(url)
        return ""

    cleaned = STICKER_MD_RE.sub(take, str(text or ""))
    cleaned = STICKER_BARE_RE.sub(take, cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, urls


def decision_segments(obj):
    """
    把模型输出归一成「要逐条连发的消息列表」。

    优先读 `segments` 数组（新契约）；兼容三种旧/坏输出：
      · 只有 `text` 字符串
      · `text` 里用 ||| 分隔多段（模型不听话时）
      · segments 里混了 {"text": ...} 对象
    """
    if not isinstance(obj, dict):
        return []
    segs = []
    raw = obj.get("segments")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                item = item.get("text") or item.get("content") or ""
            seg = _clean_text(item)
            if seg:
                segs.append(seg)
    if not segs:
        text = _clean_text(obj.get("text") or "")
        if text:
            parts = [p for p in SEGMENT_SPLIT_RE.split(text) if p.strip()]
            segs = [_clean_text(p) for p in parts]
            segs = [s for s in segs if s]
    # 去重相邻重复（模型偶尔把同一句写两遍）
    deduped = []
    for seg in segs:
        if deduped and seg == deduped[-1]:
            continue
        deduped.append(seg)
    return deduped[:MAX_SEGMENTS]

# DSH A2A 执行器的"会话已存在"错误：本应 resume，但上游正则漏了 "already exists"，
# 于是把错误原文当成回复文本返回。这里做客户端自愈：换一个新的 contextId 重试。
# 目前见过的变体：
#   session "a2a-wx-x" already exists
#   session "a2a-wx2-x" is already owned by an active write handle
SESSION_CONFLICT_RE = re.compile(r"already\s+(?:exists|has|owns|owned)|会话已存在", re.I)
ROT_SUFFIX_RE = re.compile(r"-r\d+$")

# DSH 的 ArchivedSessionGate 会拒绝"已归档会话"的每一步（turn/end reason=blocked），
# 于是 agent 不产出任何文本，插件只回 "The agent completed without visible output."。
# 同一个 contextId 再重试永远是空，只能轮换到新会话（自愈）。
EMPTY_OUTPUT_RE = re.compile(r"without\s+visible\s+output|no[-\s]visible[-\s]output", re.I)
# DSH 重启后会"恢复"上次没干净收尾的会话（给它开一个写句柄做修复），
# 但这个句柄不会释放 —— 之后任何再打开同一会话的请求都会得到
# "session ... is already owned by an active write handle"。
# 这种会话救不回来，只能换新会话（-rN）继续聊。
SESSION_OWNED_RE = re.compile(r"already\s+owned\s+by\s+an\s+active\s+write\s+handle", re.I)

def _join_parts(parts):
    """A2A 的 parts 可能是逐字 chunk，拼成一段完整文本。"""
    if not parts:
        return ""
    buf = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("kind") not in (None, "text"):
            continue
        buf.append(str(part.get("text") or ""))
    return "".join(buf).strip()


ACTION_SET = {"reply", "ignore", "defer"}
URGENCY_SET = {"now", "soon", "later"}


# =============================================================================
# 工具函数
# =============================================================================
def build_prompt(sender, message, persona="", now_time=None, contract=None):
    """拼出这一轮发给 agent 的完整文本（含契约 + 上下文）。

    contract 由 bot GUI 的「人设与指令 Prompt」页编辑后写进
    ``config["agent"]["contract"]``；留空则用内置的 CONTRACT。
    """
    now_time = now_time or time.strftime("%H:%M")
    persona = (persona or "").strip() or "幽默风趣、随和自然的真实好友"
    rules = (contract or "").strip() or CONTRACT
    return (
        f"{rules}\n\n"
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
        # 关系 → 独立 A2A 端点（@hanphone/dsh-a2a 每个端点绑一个 preset）
        self.relation_urls = agent_cfg.get("relation_urls") or {}
        if not isinstance(self.relation_urls, dict):
            self.relation_urls = {}
        self._cfg = cfg
        self._active_url = ""
        self.timeout = float(agent_cfg.get("timeout_seconds") or 180)
        self.connect_timeout = float(agent_cfg.get("connect_timeout_seconds") or 5)
        self.retries = max(0, int(agent_cfg.get("retries", 1)))
        self.max_defer_seconds = float(agent_cfg.get("max_defer_seconds") or 900)
        # 可编辑的决策契约（bot GUI → agent.contract；空串 = 用内置 CONTRACT）
        self.contract = str(agent_cfg.get("contract") or "").strip()
        # 可编辑的【你的人设】兜底句（空 = 默认；正式人设仍由 DSH 人格卡提供）
        self.persona_hint = str(agent_cfg.get("persona_hint") or "").strip()

        replies = agent_cfg.get("fallback_replies")
        if isinstance(replies, list):
            replies = [str(r).strip() for r in replies if str(r).strip()]
        else:
            replies = []
        self.fallback_replies = replies or list(DEFAULT_FALLBACK_REPLIES)

        self.logger = None          # 由主程序注入 log()，默认 print
        self.last_error = None
        self._mapping_path = agent_cfg.get("context_map_file")
        # contextId 轮换表（会话冲突自愈用），默认跟映射表放同一个目录
        rot_file = str(agent_cfg.get("context_rot_file") or "").strip()
        if not rot_file:
            if self._mapping_path:
                rot_file = os.path.join(os.path.dirname(os.path.abspath(self._mapping_path)),
                                        "wechat_agent_context_rot.json")
            else:
                rot_file = "wechat_agent_context_rot.json"
        self._rot_path = rot_file
        self._rotations = None      # 懒加载 {base_ctx: 当前轮换号}

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

    # ---------------------------------------------------- contextId 冲突自愈
    def _load_rotations(self):
        if self._rotations is not None:
            return self._rotations
        data = {}
        try:
            if self._rot_path and os.path.exists(self._rot_path):
                with open(self._rot_path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    data = {str(k): int(v) for k, v in obj.items() if str(v).isdigit()}
        except Exception:
            data = {}
        self._rotations = data
        return data

    def _save_rotations(self):
        if not self._rot_path:
            return
        try:
            d = os.path.dirname(os.path.abspath(self._rot_path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(self._rot_path, "w", encoding="utf-8") as f:
                json.dump(self._load_rotations(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_rotation(self, context_id):
        """如果这个 contextId 之前自愈过，直接用最新的那个。"""
        ctx = str(context_id or "")
        n = self._load_rotations().get(ctx, 0)
        return f"{ctx}-r{n}" if n > 0 else ctx

    def effective_context_id(self, sender):
        """这个人下次发消息实际会用的 contextId（已带上自愈轮换号）。"""
        return self._apply_rotation(self.context_id_for(sender))

    def rotate_context_id(self, context_id):
        """把 contextId 轮换到下一个可用版本（-r2、-r3…），并落盘记住。"""
        ctx = ROT_SUFFIX_RE.sub("", str(context_id or ""))
        rot = self._load_rotations()
        rot[ctx] = rot.get(ctx, 1) + 1
        self._save_rotations()
        return f"{ctx}-r{rot[ctx]}"

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

    # ------------------------------------------------------------------ 端点选择
    def relation_of(self, target=""):
        """这个联系人在配置里的关系分组（家人/朋友/同事…），没填返回空串。

        只读 contacts 的 relation 字段——人格卡已经搬到 DSH preset 侧，
        这里只决定"消息该发到哪个 A2A 关系端点"。
        """
        if not target:
            return ""
        try:
            import persona_cards as _pc
            return _pc.relation_of(self._cfg, target) or ""
        except Exception:
            return ""

    def url_for(self, target=""):
        """按联系人关系挑 A2A 端点；没配映射就用默认 url。"""
        if not self.relation_urls:
            return self.url
        rel = self.relation_of(target)
        picked = str(self.relation_urls.get(rel) or "").strip()
        if not picked and rel:
            warned = getattr(self, "_unmapped_relations", None)
            if warned is None:
                warned = self._unmapped_relations = set()
            if rel not in warned:
                warned.add(rel)
                print(f"⚠️ [A2A 路由] 关系「{rel}」没有配置专属端点，回落到默认 {self.url}", flush=True)
        return picked or self.url

    # ------------------------------------------------------------------ 主入口
    def prompt_for(self, sender, message, now_time=None):
        """用实例上配置的契约/人设拼 prompt（GUI 可编辑，空=内置）。"""
        return build_prompt(sender, message, now_time=now_time,
                            contract=self.contract,
                            persona=self.persona_hint)

    def ask(self, context_id, prompt, timeout=None, target=""):
        """问 agent 要一个决策。永远返回合法 dict，绝不抛异常。"""
        if not self.enabled:
            return self._fallback("agent_disabled")
        if requests is None:
            return self._fallback("requests_missing")

        # 防御：调用方直接传了中文 contextId（DSH 建会话会失败，永远回 (no reply)）
        context_id = self._sanitize_context_id(context_id)
        # 之前自愈过的会话：直接用轮换后的 id，避免每次都先撞一次墙
        context_id = self._apply_rotation(context_id)

        timeout = timeout or self.timeout
        url = self.url_for(target)
        raw = None
        last_err = None

        for attempt in range(self.retries + 1):
            try:
                self._active_url = url
                raw = self._send(context_id, prompt, timeout)
                break
            except Exception as exc:
                last_err = exc
                if attempt < self.retries:
                    time.sleep(0.8)

        if raw is None:
            err_text = str(last_err or "")
            # 会话被占用（DSH 重启后遗留的写句柄）走的是"任务失败"而不是正常文本，
            # 所以必须在 transport 兜底之前先自愈，否则永远只能回可爱兜底。
            if SESSION_OWNED_RE.search(err_text):
                new_ctx = self.rotate_context_id(context_id)
                self._log(f"♻️ [会话被占用自愈] {context_id} 写句柄未释放，改用新会话 {new_ctx} 重试")
                try:
                    self._active_url = url
                    raw2 = self._send(new_ctx, prompt, timeout)
                except Exception as exc:
                    raw2 = None
                    last_err = exc
                if raw2:
                    context_id, raw = new_ctx, raw2
            if raw is None:
                return self._fallback(f"transport_error: {last_err}")

        # ---- 会话冲突自愈：DSH 把 "session ... already exists" 当成回复文本返回 ----
        if SESSION_CONFLICT_RE.search(str(raw)) and self.retries >= 0:
            new_ctx = self.rotate_context_id(context_id)
            self._log(f"♻️ [会话冲突自愈] {context_id} 已存在，改用新会话 {new_ctx} 重试")
            try:
                self._active_url = url
                raw2 = self._send(new_ctx, prompt, timeout)
            except Exception as exc:
                raw2 = None
                last_err = exc
            if raw2 and not SESSION_CONFLICT_RE.search(str(raw2)):
                context_id, raw = new_ctx, raw2
            else:
                return self._fallback(f"session_conflict: {str(raw)[:120]!r}")

        # ---- 空输出自愈：会话被归档/恢复失败时 turn 被静默 block，只有换新会话 ----
        if EMPTY_OUTPUT_RE.search(str(raw)):
            new_ctx = self.rotate_context_id(context_id)
            self._log(f"♻️ [空输出自愈] {context_id} 未产出内容，改用新会话 {new_ctx} 重试")
            try:
                self._active_url = url
                raw2 = self._send(new_ctx, prompt, timeout)
            except Exception as exc:
                raw2 = None
                last_err = exc
            if raw2 and not EMPTY_OUTPUT_RE.search(str(raw2)):
                context_id, raw = new_ctx, raw2
            else:
                return self._fallback(f"empty_output: {str(raw)[:120]!r}")

        # ---- 会话被占用自愈：DSH 重启后的恢复句柄不会释放，只能换新会话 ----
        if SESSION_OWNED_RE.search(str(raw)):
            new_ctx = self.rotate_context_id(context_id)
            self._log(f"♻️ [会话被占用自愈] {context_id} 写句柄未释放，改用新会话 {new_ctx} 重试")
            try:
                self._active_url = url
                raw2 = self._send(new_ctx, prompt, timeout)
            except Exception as exc:
                raw2 = None
                last_err = exc
            if raw2 and not SESSION_OWNED_RE.search(str(raw2)):
                context_id, raw = new_ctx, raw2
            else:
                return self._fallback(f"session_owned: {str(raw)[:120]!r}")

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
            getattr(self, "_active_url", "") or self.url,
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
        # 有的实现直接返回字符串
        if isinstance(result, str):
            return result.strip()
        if not isinstance(result, dict):
            return ""
        task = result.get("task") or {}
        if not isinstance(task, dict):
            task = {}

        status = task.get("status") or {}
        state = str(status.get("state") or "")
        if state in ("TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"):
            # 失败原因常常只在 status.message 里（例如 DSH 的
            # "session ... is already owned by an active write handle"），
            # 不带上去上层就无法自愈，只能一律当网络错误。
            detail = _join_parts((status.get("message") or {}).get("parts"))
            suffix = f" ({detail[:200]})" if detail else ""
            raise RuntimeError(f"任务未成功: {state}{suffix}")

        # 1) artifacts 里是最终产物。
        #    注意：@hanphone/dsh-a2a 会把文本切成逐字 chunk（"我","这边","没有"…），
        #    所以必须把同一个 artifact 的所有 part 拼起来，不能只取最后一个。
        for art in reversed(task.get("artifacts") or []):
            joined = _join_parts(art.get("parts"))
            if joined:
                return joined

        # 2) 兜底：status.message
        joined = _join_parts((task.get("status") or {}).get("message", {}).get("parts"))
        if joined:
            return joined

        # 3) 老插件（@ryubyte）可能直接把文本放在 result 上
        for key in ("text", "message", "content", "output"):
            t = str(result.get(key) or "").strip()
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
            # 模型偶尔忘记 JSON：直接回了聊天文字（有时还顺手把表情 markdown 写在后面）。
            # 这种输出比走固定兜底话术好得多，能救就救。
            cleaned, found = split_stickers(text)
            if not found:
                return None
            lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
            segments = [" ".join(lines)] if len(lines) > 1 else lines
            segments = [seg.strip(" \"\u201c\u201d") for seg in segments if seg.strip(" \"\u201c\u201d")]
            if segments or found:
                return {
                    "action": "reply",
                    "text": "\n".join(segments),
                    "segments": segments[:MAX_SEGMENTS],
                    "stickers": found[:MAX_STICKERS],
                    "urgency": "now",
                    "reason": "plain_text_reply",
                }
            return None

        action = str(obj.get("action") or "").strip().lower()
        raw_segments = decision_segments(obj)

        # 表情：模型可能把 markdown 塞进 segments/text，也可能走独立字段
        stickers = []
        explicit = obj.get("sticker")
        if explicit is None:
            explicit = obj.get("stickers")
        if isinstance(explicit, str):
            explicit = [explicit]
        if isinstance(explicit, list):
            for item in explicit:
                if isinstance(item, dict):
                    item = item.get("url") or item.get("text") or ""
                _clean, found = split_stickers(item)
                for url in found:
                    if url not in stickers:
                        stickers.append(url)

        segments = []
        for seg in raw_segments:
            cleaned, found = split_stickers(seg)
            for url in found:
                if url not in stickers:
                    stickers.append(url)
            if cleaned:
                segments.append(cleaned)
        stickers = stickers[:MAX_STICKERS]
        out_text = "\n".join(segments)

        if action not in ACTION_SET:
            # 模型忘了给 action：有正文就当 reply，没正文就当 ignore
            action = "reply" if segments else "ignore"

        if action == "reply" and not segments and not stickers:
            # 说要回却既没正文也没表情 —— 视为坏输出，走降级兜底
            return None
        urgency = str(obj.get("urgency") or "").strip().lower()
        if urgency not in URGENCY_SET:
            urgency = "now"

        reason = str(obj.get("reason") or "").strip()[:120]
        return {
            "action": action,
            "text": out_text,
            "segments": segments,
            "stickers": stickers,
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
            "segments": [text],
            "stickers": [],
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
