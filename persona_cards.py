"""人格卡（Character Card）支持 —— 让机器人说话像个真人，而不是像客服。

兼容社区通用格式（SillyTavern / chub.ai 生态），可直接吃：

  * Character Card **V1**（老式扁平 JSON：name/description/personality/scenario/first_mes/mes_example）
  * Character Card **V2**（`spec: "chara_card_v2"` + `data{...}`，含 character_book 世界书）
  * Character Card **V3**（`spec: "chara_card_v3"`，字段与 V2 基本一致）
  * **PNG 卡**（图片 tEXt 块里塞 `chara` / `ccv3` 的 base64 JSON，社区最常见）

对外只需要三个函数：

    card = load_card("persona_cards/builtin/日常本人大号.json")
    prompt = build_system_prompt(card, friend="老爸", recent=["等下吃什么？"], now="18:20")
    card = active_card(cfg)          # 按配置里 persona_card 字段取当前启用的卡（带缓存）

设计取舍：不引入 jinja / 不跑 SillyTavern 的完整 prompt 管线，
只取「角色设定 + 示例对话 + 世界书命中 + 微信说话工艺」四段，拼成一条 system prompt。
理由：微信回复是**单句短文本**，长上下文模板反而会让模型写成小作文。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import struct
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CARD_DIR = os.path.join(BASE_DIR, "persona_cards")
BUILTIN_DIR = os.path.join(CARD_DIR, "builtin")

# 世界书默认最多注入几条（多了会把短句回复挤成说明书）
DEFAULT_WORLD_BOOK_BUDGET = 3
# 世界书关键词匹配的最近消息条数
DEFAULT_SCAN_DEPTH = 6

# ---------------------------------------------------------------------------
# 微信聊天工艺：这一段是「不生硬」的关键，跟角色卡无关，所有卡共用
# ---------------------------------------------------------------------------
WECHAT_STYLE_RULES = """你在用手机微信跟人聊天，不是写文章、不是答疑、不是客服。

【必须做到】
- 一条只发一句话，通常 3~15 个字。超过 25 字就砍掉一半再说。
- 像打字一样说话：多用"嗯、啊、哈、行、成、就、呗、挺、得、吧"这类口头词。
- 同意就"行""可以""好嘞"，觉得好笑就"哈哈""笑死"，意外就"啊这""真的假的"。
- 偶尔可以打错字、可以用网络词，但别硬凹。
- 带点自己的态度：吐槽、调侃、关心都行，就是别平淡复述。

【绝对不要】
- 不要列 1.2.3.、不要 markdown、不要星号书名号、不要括号写动作或心理。
- 不要用"首先/其次/此外/综上/总之/总的来说"这种书面连接词。
- 不要客套收尾：禁止"希望对你有所帮助""如有需要随时找我""祝你愉快"。
- 不要复述对方的话，不要总结对方的意思，不要一次问两个以上问题。
- 不要解释自己在干什么，不要提 AI、模型、助手、机器人。
- 结尾基本不用句号，说完就完事。"""


# ---------------------------------------------------------------------------
# 卡片解析
# ---------------------------------------------------------------------------
def _as_text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return "\n".join(_as_text(x) for x in v if _as_text(x))
    return str(v).strip()


def _normalize_entry(raw, idx=0):
    """把 V2/V3 世界书条目、或酒馆导出的 entries 统一成内部结构。"""
    if not isinstance(raw, dict):
        return None
    keys = raw.get("keys") or raw.get("key") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in re.split(r"[,，]", keys) if k.strip()]
    keys = [str(k).strip() for k in keys if str(k).strip()]
    content = _as_text(raw.get("content") or raw.get("value") or "")
    if not content:
        return None
    return {
        "keys": keys,
        "secondary_keys": [str(k).strip() for k in (raw.get("secondary_keys") or []) if str(k).strip()],
        "content": content,
        "enabled": bool(raw.get("enabled", True)),
        "constant": bool(raw.get("constant", False)),
        "selective": bool(raw.get("selective", False)),
        "case_sensitive": bool(raw.get("case_sensitive", False)),
        "order": int(raw.get("insertion_order", raw.get("order", idx)) or idx),
        "name": _as_text(raw.get("name") or raw.get("comment") or ""),
    }


def _normalize_card(data, source="", raw=None):
    """接受 V1 扁平 dict / V2/V3 包装 dict，输出统一结构。"""
    if not isinstance(data, dict):
        raise ValueError("卡片内容不是 JSON 对象")

    spec = str(data.get("spec") or "").strip()
    body = data.get("data") if isinstance(data.get("data"), dict) else data

    # 少数导出把 data 再包一层 character
    if isinstance(body.get("character"), dict):
        body = body["character"]

    card = {
        "name": _as_text(body.get("name") or data.get("name") or "未命名"),
        "description": _as_text(body.get("description") or ""),
        "personality": _as_text(body.get("personality") or ""),
        "scenario": _as_text(body.get("scenario") or ""),
        "first_mes": _as_text(body.get("first_mes") or ""),
        "mes_example": _as_text(body.get("mes_example") or ""),
        "system_prompt": _as_text(body.get("system_prompt") or ""),
        "post_history_instructions": _as_text(body.get("post_history_instructions") or ""),
        "creator_notes": _as_text(body.get("creator_notes") or ""),
        "tags": [str(t) for t in (body.get("tags") or []) if str(t).strip()],
        "spec": spec or "chara_card_v1",
        "source": source,
        "world_book": [],
    }

    book = body.get("character_book") or data.get("character_book") or {}
    entries = []
    if isinstance(book, dict):
        entries = book.get("entries") or []
    elif isinstance(book, list):
        entries = book
    for i, e in enumerate(entries or []):
        norm = _normalize_entry(e, i)
        if norm:
            card["world_book"].append(norm)

    # 有些卡把世界书塞在 extensions 里
    ext = body.get("extensions") or {}
    if not card["world_book"] and isinstance(ext, dict):
        for key in ("world", "world_info", "lorebook", "character_book"):
            maybe = ext.get(key)
            if isinstance(maybe, dict) and maybe.get("entries"):
                for i, e in enumerate(maybe["entries"]):
                    norm = _normalize_entry(e, i)
                    if norm:
                        card["world_book"].append(norm)
                break
    return card


def _read_png_card(path):
    """从 PNG 的 tEXt/iTXt 块里抠出 base64 的卡片 JSON（社区通用做法）。"""
    with open(path, "rb") as f:
        blob = f.read()
    if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    texts = {}
    while pos + 8 <= len(blob):
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        ctype = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"tEXt":
            k, _, v = data.partition(b"\x00")
            texts[k.decode("latin-1", "ignore").lower()] = v.decode("latin-1", "ignore")
        elif ctype == b"iTXt":
            k, _, rest = data.partition(b"\x00")
            # iTXt: keyword \0 compression_flag compression_method language_tag \0 translated \0 text
            try:
                parts = rest.split(b"\x00")
                body = parts[-1] if len(parts) >= 3 else rest
                texts[k.decode("latin-1", "ignore").lower()] = body.decode("utf-8", "ignore")
            except Exception:
                continue
        if ctype == b"IEND":
            break

    for key in ("ccv3", "chara"):
        val = texts.get(key)
        if not val:
            continue
        try:
            payload = base64.b64decode(val + "=" * (-len(val) % 4))
            return json.loads(payload.decode("utf-8", "ignore"))
        except Exception:
            continue
    return None


def load_card(path):
    """从 JSON 或 PNG 读一张卡。"""
    if isinstance(path, dict):
        return _normalize_card(path, source="<dict>", raw=path)

    if not os.path.isabs(path):
        candidates = []
        for root in (CARD_DIR, BUILTIN_DIR, BASE_DIR):
            candidates.append(os.path.join(root, path))
            if not path.lower().endswith((".json", ".png")):
                candidates.append(os.path.join(root, path + ".json"))
                candidates.append(os.path.join(root, path + ".png"))
        # 也允许直接写卡名（不带扩展名）
        for c in candidates:
            if os.path.exists(c):
                path = c
                break

    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到人格卡：{path}")

    if path.lower().endswith(".png"):
        data = _read_png_card(path)
        if not data:
            raise ValueError(f"PNG 里没有找到 chara/ccv3 数据：{path}")
        return _normalize_card(data, source=path)

    with io.open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    return _normalize_card(data, source=path)


def list_cards():
    """列出所有可用卡片：内置 + 用户自建。返回 [{name, file, origin, tags}]"""
    out = []
    for origin, root in (("内置", BUILTIN_DIR), ("自建", CARD_DIR)):
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            if not fn.lower().endswith((".json", ".png")):
                continue
            full = os.path.join(root, fn)
            try:
                c = load_card(full)
            except Exception:
                continue
            if origin == "自建" and os.path.abspath(full).startswith(os.path.abspath(BUILTIN_DIR)):
                continue
            out.append({"name": c["name"], "file": full, "origin": origin,
                        "tags": c["tags"], "spec": c["spec"]})
    return out


# ---------------------------------------------------------------------------
# 世界书命中
# ---------------------------------------------------------------------------
def select_world_book(card, recent_text, budget=DEFAULT_WORLD_BOOK_BUDGET):
    """按关键词从世界书里挑要注入的条目。

    recent_text: 最近几条消息拼成的字符串（对方说的 + 我说的）
    """
    entries = card.get("world_book") or []
    if not entries:
        return []
    haystack = (recent_text or "").lower()
    hits, const_hits = [], []
    for e in entries:
        if not e.get("enabled", True):
            continue
        if e.get("constant"):
            const_hits.append(e)
            continue
        keys = e.get("keys") or []
        if not keys:
            continue
        probe = haystack if not e.get("case_sensitive") else (recent_text or "")
        hit = any((k if e.get("case_sensitive") else k.lower()) in probe for k in keys)
        if hit and e.get("selective") and e.get("secondary_keys"):
            hit = any((k if e.get("case_sensitive") else k.lower()) in probe
                      for k in e["secondary_keys"])
        if hit:
            hits.append(e)
    hits.sort(key=lambda e: e.get("order", 0))
    const_hits.sort(key=lambda e: e.get("order", 0))
    picked = (const_hits + hits)[:budget]
    return picked


# ---------------------------------------------------------------------------
# 示例对话
# ---------------------------------------------------------------------------
_MACRO_RE = re.compile(r"\{\{\s*(char|user|persona)\s*\}\}", re.I)


def apply_macros(text, char_name="我", user_name="对方"):
    if not text:
        return ""
    return _MACRO_RE.sub(lambda m: char_name if m.group(1).lower() == "char" else user_name, text)


def parse_example_dialog(mes_example, char_name="我", user_name="对方", max_rounds=6):
    """把 mes_example 解析成 [(说话人, 内容)]，支持 <START> 分隔和 {{char}}/{{user}} 宏。"""
    if not mes_example:
        return []
    text = apply_macros(mes_example, char_name, user_name)
    turns = []
    for block in re.split(r"<START>", text, flags=re.I):
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(我|你|{{char}}|{{user}}|" + re.escape(char_name) + r"|"
                         + re.escape(user_name) + r")\s*[:：]\s*(.*)$", line)
            if m:
                who = m.group(1)
                said = m.group(2).strip()
                if said:
                    speaker = "char" if who in ("我", "{{char}}", char_name) else "user"
                    turns.append((speaker, said))
            elif turns:
                # 续行：并到上一条
                turns[-1] = (turns[-1][0], turns[-1][1] + line)
    return turns[:max_rounds * 2]


# ---------------------------------------------------------------------------
# 组装 system prompt
# ---------------------------------------------------------------------------
def build_system_prompt(card, friend="", recent=None, now="", persona_desc="",
                        extra_rules="", world_book_budget=DEFAULT_WORLD_BOOK_BUDGET):
    """把角色卡 + 微信工艺 + 世界书拼成一条 system prompt。"""
    recent = recent or []
    recent_text = "\n".join(str(x) for x in recent)
    char_name = "我"

    parts = []
    parts.append(f"你是「{card['name']}」，现在正用这部手机上的微信跟人聊天。")

    if card.get("description"):
        parts.append("【你是谁】\n" + apply_macros(card["description"], char_name))
    if card.get("personality"):
        parts.append("【你的性格与说话习惯】\n" + apply_macros(card["personality"], char_name))
    if card.get("scenario"):
        parts.append("【眼下的场景】\n" + apply_macros(card["scenario"], char_name))

    parts.append(WECHAT_STYLE_RULES)

    examples = parse_example_dialog(card.get("mes_example", ""))
    if examples:
        lines = ["【说话示范】（照着这个语感，不要照抄内容）"]
        for speaker, said in examples:
            lines.append(("对方：" if speaker == "user" else "你：") + said)
        parts.append("\n".join(lines))

    hits = select_world_book(card, recent_text, world_book_budget)
    if hits:
        lines = ["【你记得的事】"]
        for e in hits:
            lines.append("- " + apply_macros(e["content"], char_name))
        parts.append("\n".join(lines))

    if persona_desc:
        parts.append("【对这个人的额外态度】\n" + persona_desc)

    ctx = []
    if friend:
        ctx.append(f"这是跟「{friend}」的对话。")
    if now:
        ctx.append(f"现在是{now}。")
    if ctx:
        parts.append("【当前情况】\n" + " ".join(ctx))

    if card.get("post_history_instructions"):
        parts.append("【最后叮嘱】\n" + apply_macros(card["post_history_instructions"], char_name))
    if extra_rules:
        parts.append(extra_rules)

    parts.append("现在，直接输出你要发出去的那一句微信。")
    return "\n\n".join(p for p in parts if p.strip())


def persona_brief(card, max_examples=3):
    """给 agent 契约用的人设。

    跟早期版本的区别：**带上了示例对话**。示例（few-shot）才是治生硬的关键，
    只给性格描述时模型仍会写书面语。为控制长度只取前 max_examples 轮。
    """
    bits = []
    if card.get("description"):
        bits.append(apply_macros(card["description"]))
    if card.get("personality"):
        bits.append(apply_macros(card["personality"]))

    turns = parse_example_dialog(card.get("mes_example", ""), max_rounds=max_examples)
    # 截到完整的一问一答，别让示例停在"对方：……"上
    while turns and turns[-1][0] == "user":
        turns.pop()
    if turns:
        lines = ["像这样说话："]
        for speaker, said in turns:
            lines.append(("对方：" if speaker == "user" else "你：") + said)
        bits.append("\n".join(lines))

    bits.append("一条只发一句，3~15字，口语，不列点、不写书面连接词、不客套收尾、不加括号动作。")
    return "\n".join(b for b in bits if b)


# ---------------------------------------------------------------------------
# 角色关系（通讯录里的 relation 字段）→ 人格卡
# ---------------------------------------------------------------------------
# 下拉默认值，用户可以自己在通讯录里写别的关系名
RELATIONS = ["家人", "朋友", "同事", "客户", "恋人", "同学", "领导", "陌生"]


_UNREAD_SUFFIX_RE = re.compile(r"\s*[（(]\s*\d+\s*[)）]\s*$")


def normalize_title(title):
    """剥掉微信顶栏标题尾部的未读数后缀。

    只剥「行尾孤立的 (3) / （12）」这种：
        "ai聊天群(3)"   -> "ai聊天群"
        "老爸(2)"       -> "老爸"
    但**不动**以数字开头或中间带数字的备注：
        "22-撩人精(3)"  -> "22-撩人精"     （保留 22，名字的一部分）
        "微信 2"        -> "微信 2"
    """
    t = str(title or "").strip()
    if not t:
        return ""
    return _UNREAD_SUFFIX_RE.sub("", t).strip()


def _norm_relation(rel):
    rel = str(rel or "").strip()
    return rel


def contact_of(cfg, target):
    """在通讯录里按 备注 / 微信号 找这个人，返回那条 contact dict（可能含 relation）。"""
    target = str(target or "").strip()
    if not cfg or not target:
        return None
    for c in cfg.get("contacts") or []:
        if isinstance(c, str):
            c = {"remark": c}
        if not isinstance(c, dict):
            continue
        remark = str(c.get("remark") or c.get("name") or "").strip()
        wxid = str(c.get("wxid") or c.get("wechat_id") or c.get("alias") or "").strip()
        if target and (target == remark or (wxid and target == wxid)):
            return c
    return None


def relation_of(cfg, target):
    """这个人的关系分组，没填返回空串。"""
    c = contact_of(cfg, target)
    if not c:
        return ""
    return _norm_relation(c.get("relation"))


def _card_path_ref(value):
    """把配置里写的卡片引用（路径 / 文件名 / 卡名）解析成可读路径，失败返回原值。"""
    value = str(value or "").strip()
    if not value:
        return ""
    return value


def resolve_card(cfg, target=""):
    """按「好友指定卡 > 关系卡 > 全局卡」的优先级，返回 (card, 来源说明)。

    优先级理由：越具体的设定越该赢。给某个好友单独指定一张卡，
    不该被一条关系分组覆盖；反过来，只设了关系时，所有同关系的人共用一张卡。
    """
    if not cfg:
        return None, ""

    name = str(target or "").strip()
    # 1) 好友单独指定
    if name:
        per = (cfg.get("friend_cards") or {})
        if isinstance(per, dict) and per.get(name):
            card = _load_ref(per.get(name))
            if card:
                return card, f"好友:{name}"

    # 2) 关系分组
    rel = relation_of(cfg, name) if name else ""
    if rel:
        rel_map = cfg.get("relation_cards") or {}
        if isinstance(rel_map, dict) and rel_map.get(rel):
            card = _load_ref(rel_map.get(rel))
            if card:
                return card, f"关系:{rel}"

    # 3) 全局
    card = active_card(cfg)
    if card:
        return card, "全局"
    return None, ""


def _load_ref(ref):
    """ref 可以是绝对路径、相对 persona_cards 的路径、或纯卡名。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    try:
        return load_card(ref)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 配置接入（带 mtime 缓存，主循环每轮都会问一次）
# ---------------------------------------------------------------------------
_CARD_CACHE = {"path": None, "mtime": None, "card": None}


def active_card(cfg):
    """按 cfg["persona_card"] 返回当前启用的卡；没配/读失败返回 None。"""
    if not cfg:
        return None
    ref = str(cfg.get("persona_card") or "").strip()
    if not ref:
        return None

    path = ref
    if not os.path.isabs(path) and not os.path.exists(path):
        for root in (CARD_DIR, BASE_DIR, BUILTIN_DIR):
            cand = os.path.join(root, ref)
            if os.path.exists(cand):
                path = cand
                break

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    if _CARD_CACHE["path"] == path and _CARD_CACHE["mtime"] == mtime:
        return _CARD_CACHE["card"]

    try:
        card = load_card(path)
    except Exception:
        return None
    _CARD_CACHE.update({"path": path, "mtime": mtime, "card": card})
    return card


# ---------------------------------------------------------------------------
# CLI：方便肉眼检查拼出来的 prompt
# ---------------------------------------------------------------------------
def _main(argv):  # pragma: no cover - 手工调试用
    import argparse
    ap = argparse.ArgumentParser(description="人格卡工具")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出可用人格卡")
    p_show = sub.add_parser("show", help="看一张卡的字段")
    p_show.add_argument("card")
    p_prompt = sub.add_parser("prompt", help="直接看拼出来的 system prompt")
    p_prompt.add_argument("card")
    p_prompt.add_argument("--friend", default="老爸")
    p_prompt.add_argument("--recent", default="", help="最近消息，用 | 分隔")
    p_imp = sub.add_parser("import", help="导入一张卡（JSON/PNG）到 persona_cards/")
    p_imp.add_argument("path")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        rows = list_cards()
        if not rows:
            print("（还没有人格卡）")
            return 0
        for r in rows:
            print(f"[{r['origin']}] {r['name']}  ({r['spec']})  {os.path.basename(r['file'])}")
        return 0

    if args.cmd == "show":
        c = load_card(args.card)
        print(json.dumps({k: v for k, v in c.items() if k != "world_book"},
                         ensure_ascii=False, indent=2))
        print(f"世界书条目：{len(c['world_book'])}")
        for e in c["world_book"]:
            print("  -", e["keys"], "→", e["content"][:40])
        return 0

    if args.cmd == "prompt":
        c = load_card(args.card)
        recent = [x for x in args.recent.split("|") if x.strip()]
        print(build_system_prompt(c, friend=args.friend, recent=recent,
                                  now=time.strftime("%H:%M")))
        return 0

    if args.cmd == "import":
        os.makedirs(CARD_DIR, exist_ok=True)
        c = load_card(args.path)
        dst = os.path.join(CARD_DIR, os.path.basename(args.path))
        if os.path.abspath(args.path) != os.path.abspath(dst):
            with open(args.path, "rb") as src, open(dst, "wb") as out:
                out.write(src.read())
        print(f"已导入：{c['name']} → {dst}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(__import__("sys").argv[1:]))
