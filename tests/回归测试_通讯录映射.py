# -*- coding: utf-8 -*-
"""
通讯录映射（备注 + 微信号）· 离线回归测试
=====================================================================
验证这几件事：
  1. contacts 配置解析（字符串 / 字典 / 脏数据）
  2. 白名单 = contacts 备注 ∪ 旧 whitelist（去重保序）
  3. 备注 <-> 微信号 双向解析
  4. 搜索键 / 会话键的选择优先级
  5. 写回配置文件（不破坏其它字段）
  6. 白名单门禁：contacts 里的人能被回复，其他人不能
  7. 主动发送：传微信号也能过护栏
  8. /contacts HTTP 读写接口

微信不用开，纯离线。
跑法：  python 回归测试_通讯录映射.py
=====================================================================
"""
import io
import json
import os
import sys
import tempfile
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
FAILS = []


def ok(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        FAILS.append(label)
        print(f"  ❌ {label}  {extra}")


def eq(actual, expected, label):
    ok(actual == expected, label, f"(期望 {expected!r}，实际 {actual!r})")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 用临时配置文件隔离，绝不碰真实的 wechat_config_dev.json
# ---------------------------------------------------------------------------
_TMPDIR = tempfile.mkdtemp(prefix="wxcontacts_")
_CFG_PATH = os.path.join(_TMPDIR, "cfg.json")
bot.CONFIG_FILE = _CFG_PATH


def write_cfg(d):
    with open(_CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def base_cfg(**kw):
    d = {
        "whitelist_mode": True,
        "whitelist": [],
        "contacts": [],
        "blacklist": ["微信团队", "文件传输助手"],
        "proactive_send": {
            "enabled": True, "whitelist_only": True, "require_confirm": False,
            "max_per_hour": 3, "max_per_day": 20,
        },
        "agent": {"enabled": True},
    }
    d.update(kw)
    write_cfg(d)
    return d


print("=" * 68)
print("通讯录映射（备注 + 微信号）· 离线回归测试")
print("=" * 68)

# ---------------------------------------------------------------------------
section("1. load_contacts 配置解析")
# ---------------------------------------------------------------------------
base_cfg(contacts=[
    {"remark": "张三", "wxid": "zhangsan_88"},
    "李四",
    {"remark": "", "wxid": "wxid_only"},
    {"remark": "王五", "wxid": ""},
    {"remark": "", "wxid": ""},          # 空记录 → 丢弃
    "   ",                                # 空白字符串 → 丢弃
    123,                                  # 类型非法 → 丢弃
    {"note": "只有注释"}                   # 无 remark/wxid → 丢弃
])
cs = bot.load_contacts()
eq(len(cs), 4, "脏数据被清洗，保留 4 条")
eq(cs[0], {"remark": "张三", "wxid": "zhangsan_88"}, "字典形态解析正确")
eq(cs[1], {"remark": "李四", "wxid": ""}, "纯字符串形态解析正确")
eq(cs[2], {"remark": "wxid_only", "wxid": "wxid_only"}, "只填 wxid 时用 wxid 当显示名")
eq(cs[3], {"remark": "王五", "wxid": ""}, "只填 remark 时 wxid 为空串")

base_cfg(contacts="不是列表")
eq(bot.load_contacts(), [], "contacts 非列表 → 返回空")
base_cfg()
eq(bot.load_contacts(), [], "contacts 缺省 → 返回空")
base_cfg(contacts=[{"remark": "甲", "wechat_id": "jia_1"}])
eq(bot.load_contacts()[0]["wxid"], "jia_1", "wechat_id 别名可用")
base_cfg(contacts=[{"name": "乙", "alias": "yi_1"}])
eq(bot.load_contacts()[0], {"remark": "乙", "wxid": "yi_1"}, "name/alias 别名可用")

# ---------------------------------------------------------------------------
section("2. effective_whitelist = contacts ∪ 旧 whitelist")
# ---------------------------------------------------------------------------
base_cfg(contacts=[{"remark": "张三", "wxid": "zs"}, {"remark": "李四"}],
         whitelist=["李四", "旧好友", "  ", 123])
eq(bot.effective_whitelist(), ["张三", "李四", "旧好友"],
   "并集去重保序，过滤空白与非字符串")

base_cfg(contacts=[], whitelist=["老A", "老B"])
eq(bot.effective_whitelist(), ["老A", "老B"], "只有旧 whitelist 时行为不变")

base_cfg(contacts=[{"remark": "张三"}], whitelist=[])
eq(bot.effective_whitelist(), ["张三"], "只用 contacts、whitelist 为空也能生效")

# ---------------------------------------------------------------------------
section("3. resolve_contact 双向解析")
# ---------------------------------------------------------------------------
base_cfg(contacts=[
    {"remark": "张三", "wxid": "zhangsan_88"},
    {"remark": "李总", "wxid": "li_zong"},
])
eq(bot.resolve_contact("张三")["wxid"], "zhangsan_88", "按备注命中")
eq(bot.resolve_contact("zhangsan_88")["remark"], "张三", "按微信号命中")
eq(bot.resolve_contact("ZHANGSAN_88")["remark"], "张三", "微信号大小写不敏感")
ok(bot.resolve_contact("张三丰") is None, "越配防护：张三 ≠ 张三丰")
ok(bot.resolve_contact("王五") is None, "不在通讯录 → None")
ok(bot.resolve_contact("") is None, "空名字 → None")
ok(bot.resolve_contact("  ") is None, "全空白 → None")

# ---------------------------------------------------------------------------
section("4. canonical_target / 搜索键 / 会话键")
# ---------------------------------------------------------------------------
base_cfg(contacts=[{"remark": "张三", "wxid": "zhangsan_88"},
                   {"remark": "李四", "wxid": ""},
                   {"remark": "官方号", "wxid": "wxid_27jcr0lsu2hc22"}])
eq(bot.canonical_target("zhangsan_88"), "张三", "wxid → 备注")
eq(bot.canonical_target("张三"), "张三", "备注 → 备注（原样）")
eq(bot.canonical_target("陌生人"), "陌生人", "未知名字原样返回")
eq(bot.contact_search_key("张三"), "zhangsan_88", "自定义微信号可搜，搜索键用它")
eq(bot.contact_search_key("李四"), "李四", "没微信号时搜索键退回备注")
eq(bot.contact_search_key("官方号"), "官方号",
   "wxid_ 开头的官方微信号搜不到 → 搜索键退回备注")
ok(bot.is_custom_wxid("zhangsan_88") is True, "自定义微信号判定为可搜")
ok(bot.is_custom_wxid("wxid_27jcr0lsu2hc22") is False, "官方 wxid_ 判定为不可搜")
ok(bot.is_custom_wxid("WXID_ABC") is False, "官方 wxid_ 大小写不敏感")
ok(bot.is_custom_wxid("") is False, "空微信号不可搜")
ok(bot.is_custom_wxid(None) is False, "None 微信号不可搜")
eq(bot.contact_context_id("张三"), "zhangsan_88", "有微信号时会话键用微信号")
eq(bot.contact_context_id("李四"), "李四", "没微信号时会话键退回备注")
eq(bot.contact_wxid("陌生人"), "", "未知名字的 wxid 为空")

# 会话键必须是 ASCII 才能避开 DSH A2A 的中文 contextId 坑
cid = bot.contact_context_id("张三")
ok(cid.isascii(), f"微信号会话键是纯 ASCII（{cid}）")

# ---------------------------------------------------------------------------
section("5. save_contacts 写回不破坏其它字段")
# ---------------------------------------------------------------------------
base_cfg(contacts=[], system_prompt="我是自定义提示词", reply_prefix="[AI] ")
bot.save_contacts([{"remark": "新朋友", "wxid": "new_friend"}])
raw = json.load(open(_CFG_PATH, encoding="utf-8"))
eq(raw["system_prompt"], "我是自定义提示词", "其它字段保持不变")
eq(raw["reply_prefix"], "[AI] ", "其它字段保持不变（2）")
eq(raw["contacts"], [{"remark": "新朋友", "wxid": "new_friend"}], "contacts 已写入")
eq(bot.load_contacts()[0]["remark"], "新朋友", "写后可读回")

bot.save_contacts("不是列表")
eq(json.load(open(_CFG_PATH, encoding="utf-8"))["contacts"], [],
   "save_contacts 传非法值 → 落成空列表")

# ---------------------------------------------------------------------------
section("6. 白名单门禁：contacts 里的人才能被回复")
# ---------------------------------------------------------------------------
base_cfg(contacts=[{"remark": "张三", "wxid": "zhangsan_88"}], whitelist=[])
wl = bot.effective_whitelist()
ok(bot.is_name_matched_strict("张三", wl)[0], "通讯录里的张三 → 命中白名单")
ok(bot.is_name_matched_strict("张三 晚上吃啥", wl)[0], "带消息预览也能命中")
ok(not bot.is_name_matched_strict("张三丰", wl)[0], "张三丰 → 不命中")
ok(not bot.is_name_matched_strict("路人甲", wl)[0], "通讯录外的人 → 不命中")

# ---------------------------------------------------------------------------
section("7. 主动发送：传微信号也能过护栏")
# ---------------------------------------------------------------------------
base_cfg(contacts=[{"remark": "张三", "wxid": "zhangsan_88"}],
         whitelist=[], blacklist=["微信团队"])
cfg = bot.load_config()
ok(bot.proactive_check(cfg, "张三")[0], "按备注：放行")
okv, code, detail = bot.proactive_check(cfg, "zhangsan_88")
ok(okv, "按微信号：放行", f"code={code} detail={detail}")
okv, code, _ = bot.proactive_check(cfg, "陌生人")
ok(not okv and code == "not_whitelisted", "陌生人：拒绝 not_whitelisted", f"code={code}")
okv, code, _ = bot.proactive_check(cfg, "吃喝玩乐(25)")
ok(not okv, "群聊名：一律拒绝", f"code={code}")

# ---------------------------------------------------------------------------
section("8. /contacts HTTP 读写")
# ---------------------------------------------------------------------------
base_cfg(contacts=[{"remark": "张三", "wxid": "zhangsan_88"}],
         proactive_send={"enabled": True, "host": "127.0.0.1", "port": 0,
                         "whitelist_only": True, "max_per_hour": 3,
                         "max_per_day": 20, "require_confirm": False})
srv = bot.start_proactive_server()
if srv is None:
    ok(False, "HTTP 服务启动失败")
else:
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def call(method, path, payload=None):
        url = base + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            except Exception:
                return e.code, {}

    st, r = call("GET", "/contacts")
    eq(st, 200, "GET /contacts 返回 200")
    eq(len(r.get("contacts") or []), 1, "GET /contacts 列出 1 条")
    eq(r["contacts"][0]["wxid"], "zhangsan_88", "GET 内容正确")

    st, r = call("POST", "/contacts", {"remark": "李四", "wxid": "li_si"})
    eq(st, 200, "POST 单条新增 200")
    eq(len(bot.load_contacts()), 2, "新增后共 2 条")

    st, r = call("POST", "/contacts", {"remark": "李四", "wxid": "li_si_v2"})
    eq(st, 200, "POST 同备注覆盖 200")
    eq(len(bot.load_contacts()), 2, "覆盖不新增条目")
    eq(bot.contact_wxid("李四"), "li_si_v2", "覆盖后微信号已更新")

    st, r = call("POST", "/contacts", {"contacts": [{"remark": "甲"}, {"remark": "乙"}]})
    eq(st, 200, "POST 整体替换 200")
    eq([c["remark"] for c in bot.load_contacts()], ["甲", "乙"], "整体替换生效")

    st, r = call("POST", "/contacts", {"remove": "甲"})
    eq(st, 200, "POST 删除 200")
    eq([c["remark"] for c in bot.load_contacts()], ["乙"], "删除生效")

    st, r = call("POST", "/contacts", {"remove": "不存在"})
    eq(st, 400, "删除不存在 → 400")

    st, r = call("POST", "/contacts", {})
    eq(st, 400, "空 body → 400")

    st, r = call("POST", "/contacts", {"contacts": ["纯字符串丙"]})
    eq(st, 200, "字符串数组也能整体替换")
    eq(bot.load_contacts()[0]["remark"], "纯字符串丙", "字符串转成 remark")

    bot.stop_proactive_server()

# ---------------------------------------------------------------------------
section("9. find_contact 的搜索键接线")
# ---------------------------------------------------------------------------
base_cfg(contacts=[{"remark": "张三", "wxid": "zhangsan_88"},
                   {"remark": "李四", "wxid": ""},
                   {"remark": "文件传输助手", "wxid": ""}])

_orig_scan = bot._scan_window
_orig_row = bot._sidebar_row_y
_orig_search = bot._search_and_open
_captured = {}


class _FakeImg(object):
    size = (1925, 1313)


def _fake_scan(hwnd):
    return _FakeImg(), {"sidebar_w": 105, "chat_start_x": 548,
                        "header_h": 144, "input_h": 209,
                        "total_w_px": 1925, "total_h_px": 1313}, [], ""


def _fake_search(hwnd, target, search_key=None):
    _captured["hwnd"] = hwnd
    _captured["target"] = target
    _captured["search_key"] = search_key
    return True


bot._scan_window = _fake_scan
bot._sidebar_row_y = lambda res, layout, target, H: None
bot._search_and_open = _fake_search

_captured.clear()
ok(bot.find_contact(1, "张三") is True, "find_contact 走到搜索级并成功")
eq(_captured["target"], "张三", "target 仍是备注（复核用）")
eq(_captured["search_key"], "zhangsan_88", "有微信号时搜索键用微信号")

_captured.clear()
bot.find_contact(1, "李四")
eq(_captured["search_key"], "李四", "没微信号时搜索键退回备注")

_captured.clear()
bot.find_contact(1, "zhangsan_88")
eq(_captured["target"], "张三", "调用方传微信号 → 归一化成备注")
eq(_captured["search_key"], "zhangsan_88", "搜索键仍是微信号")

_captured.clear()
bot.find_contact(1, "陌生人")
eq(_captured["target"], "陌生人", "通讯录外的人原样透传")
eq(_captured["search_key"], "陌生人", "通讯录外的人搜索键就是名字")

# --- 搜索降级：微信号搜不到就改用备注重搜（用户真机结论，m01907）---
calls = []


def _fail_first(hwnd, target, search_key=None):
    calls.append(search_key)
    return len(calls) >= 2


bot._search_and_open = _fail_first
base_cfg(contacts=[{"remark": "张三", "wxid": "zhangsan_88"}], whitelist=[])
ok(bot.find_contact(1, "张三") is True, "自定义微信号搜不到 → 降级备注重搜，最终成功")
eq(calls, ["zhangsan_88", "张三"], "降级顺序：先自定义微信号，后备注")

calls.clear()


def _succeed(hwnd, target, search_key=None):
    calls.append(search_key)
    return True


bot._search_and_open = _succeed
base_cfg(contacts=[{"remark": "官方号", "wxid": "wxid_abc123"}], whitelist=[])
ok(bot.find_contact(1, "官方号") is True, "官方 wxid_ 场景能搜到")
eq(calls, ["官方号"], "官方 wxid_ 直接按备搜，只搜一次（不做无意义的重复）")

calls.clear()


def _always_fail(hwnd, target, search_key=None):
    calls.append(search_key)
    return False


bot._search_and_open = _always_fail
base_cfg(contacts=[{"remark": "张三", "wxid": "zhangsan_88"}], whitelist=[])
ok(bot.find_contact(1, "张三") is False, "两种关键词都搜不到 → 明确返回 False")
eq(calls, ["zhangsan_88", "张三"], "两次都试过，绝不盲发")

bot._scan_window = _orig_scan
bot._sidebar_row_y = _orig_row
bot._search_and_open = _orig_search

# ---------------------------------------------------------------------------
print("\n" + "=" * 68)
if FAIL == 0:
    print(f"✅ 全部用例 PASS（{PASS} 项断言），通讯录映射零回退")
else:
    print(f"❌ 失败 {FAIL} 项 / 共 {PASS + FAIL} 项：")
    for f in FAILS:
        print(f"   - {f}")
print("=" * 68)
sys.exit(0 if FAIL == 0 else 1)
