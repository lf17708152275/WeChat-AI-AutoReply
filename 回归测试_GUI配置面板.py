# -*- coding: utf-8 -*-
"""
回归测试：GUI 新增的三块配置面板（通讯录 / Agent 大脑 / 主动发送）能否正确加载与保存。

要点：
- 用临时配置文件，绝不碰用户的真实 wechat_config_dev.json
- 保存后必须保留界面上没暴露的 agent 子键（card_url / retries / context_map_file…）
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

PASS = 0
FAIL = 0


def check(name, actual, expect):
    global PASS, FAIL
    ok = actual == expect
    print(("  [PASS] " if ok else "  [FAIL] ") + f"{name}: 实际={actual!r}" + ("" if ok else f" 期望={expect!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def check_true(name, cond):
    check(name, bool(cond), True)


import tkinter as tk

import wechat_bot_gui_dev as gui

# ---- 用临时配置，避免污染真实配置 ----
tmpdir = tempfile.mkdtemp(prefix="wxgui_")
tmp_cfg = os.path.join(tmpdir, "cfg.json")
src_cfg = os.path.join(gui.WORKSPACE_DIR, "wechat_config_dev.json")
if os.path.exists(src_cfg):
    shutil.copy(src_cfg, tmp_cfg)
else:
    with open(tmp_cfg, "w", encoding="utf-8") as f:
        json.dump({}, f)

gui.CONFIG_FILE = tmp_cfg
gui.LOG_FILE = os.path.join(tmpdir, "wxgui.log")
with open(gui.LOG_FILE, "w", encoding="utf-8") as _lf:
    _lf.write("🔀 [路由] 初始化测试日志\n✅ [测试] 历史行\n")
gui.WeChatBotGUI.start_background_monitors = lambda self: None

with open(tmp_cfg, "r", encoding="utf-8") as _f:
    try:
        _seed = json.load(_f)
    except Exception:
        _seed = {}
SEED_PRO_ENABLED = bool((_seed.get("proactive_send") or {}).get("enabled", False))
SEED_RELATION_URLS = dict((_seed.get("agent") or {}).get("relation_urls") or {})

print("=== 1. 实例化与标签页 ===")
app = gui.WeChatBotGUI()
app.update()
tabs = [app.notebook.tab(t, "text").strip() for t in app.notebook.tabs()]
check("标签页数量", len(tabs), 7)
check_true("含通讯录页", any("通讯录" in t for t in tabs))
check_true("含智能大脑页", any(("大脑" in t) for t in tabs))
check_true("含 A2A 关系路由页", any("A2A 关系路由" in t for t in tabs))

# 原有 4 个页签位置必须保持不变，新增页排在后面
_orig = ["AI 引擎设置", "人设与指令 Prompt", "白名单与防扰", "AI 仿真测试沙盒"]
check("原有 4 页顺序未变",
      [_orig[i] in tabs[i] for i in range(min(4, len(tabs)))],
      [True, True, True, True])
check("第 5 页是 A2A 关系路由", ("A2A 关系路由" in tabs[4]) if len(tabs) > 4 else False, True)
check("第 6 页是通讯录映射", ("通讯录" in tabs[5]) if len(tabs) > 5 else False, True)
check("第 7 页是大脑/主动发送", ("大脑" in tabs[6]) if len(tabs) > 6 else False, True)
check_true("人格卡页已移除（人格改由 DSH preset 负责）", not any("人格卡" in t for t in tabs))

print("\n=== 2. 控件存在性 ===")
for attr in ("tree_routes", "ent_route_relation", "ent_route_url",
             "tree_contacts", "ent_ct_remark", "ent_ct_wxid",
             "ent_agent_url", "ent_agent_prefix", "spin_agent_timeout", "spin_agent_defer",
             "txt_agent_fallback", "var_agent_enabled",
             "var_pro_enabled", "var_pro_wl_only", "var_pro_confirm", "var_pro_search",
             "ent_pro_host", "spin_pro_port", "spin_pro_hour", "spin_pro_day", "lbl_pro_status",
             "main_pane", "txt_log", "lbl_log_status", "cbo_log_mode", "ent_log_filter",
             "var_log_follow", "var_log_pause"):
    check_true(f"控件 {attr}", hasattr(app, attr))

print("\n=== 3. 初始加载 ===")
check("Agent 默认开启", app.var_agent_enabled.get(), True)
# 用种子配置里的实际值比对，别写死默认值（用户可能改过 agent 地址）
SEED_AGENT_URL = ((_seed.get("agent") or {}).get("url") or "http://127.0.0.1:44708/a2a").strip()
check("Agent 地址", app.ent_agent_url.get().strip(), SEED_AGENT_URL)
check("主动发送勾选与配置文件一致", app.var_pro_enabled.get(), SEED_PRO_ENABLED)
check("端口默认", str(app.spin_pro_port.get()), "8765")

print("\n=== 3b. A2A 关系路由 ===")
check("通讯录表格列（端点已移出，只留会话）",
      tuple(app.tree_contacts["columns"]), ("remark", "wxid", "relation", "session"))
check("通讯录关系下拉只读（不能手打，只能选路由表里的关系）",
      str(app.cbo_ct_relation.cget("state")), "readonly")
route_rows = [app.tree_routes.item(i, "values") for i in app.tree_routes.get_children()]
check("关系路由条数与配置一致", len(route_rows), len(SEED_RELATION_URLS))
# 首次打开 GUI 时，通讯录下拉必须**一开始**就包含路由表里的全部关系
# （旧 bug：_load_relation_routes 在通讯录页建好之后才跑，没回填，导致只剩通讯录里用过的两个）
_dropdown = list(app.cbo_ct_relation.cget("values"))
check("通讯录关系下拉初始值=路由表全部关系",
      _dropdown, [r[0] for r in route_rows])
app.ent_route_relation.set("测试关系")
app.ent_route_url.delete(0, "end")
app.ent_route_url.insert(0, "http://192.168.1.100/a2a/relation-test")
app.add_route()
app.update()
route_rows = [app.tree_routes.item(i, "values") for i in app.tree_routes.get_children()]
check_true("新增关系后出现在路由表", any(r[0] == "测试关系" for r in route_rows))
check_true("通讯录关系下拉已联动（含测试关系）",
           "测试关系" in list(app.cbo_ct_relation.cget("values")))

_route_id = [i for i in app.tree_routes.get_children()
             if app.tree_routes.item(i, "values")[0] == "测试关系"]
app.tree_routes.selection_set(_route_id[0])
app.on_route_select()
app.update()
check("选中路由回填关系", app.ent_route_relation.get(), "测试关系")
check("选中路由回填端点", app.ent_route_url.get(), "http://192.168.1.100/a2a/relation-test")
app.del_route()
app.update()
route_rows = [app.tree_routes.item(i, "values") for i in app.tree_routes.get_children()]
check_true("删除关系后从路由表消失", all(r[0] != "测试关系" for r in route_rows))
check_true("通讯录关系下拉已联动（移除测试关系）",
           "测试关系" not in list(app.cbo_ct_relation.cget("values")))

print("\n=== 3b-2. Linux 离屏隐藏按钮（假后端，不碰真窗口） ===")


class _FakeRect:
    def __init__(self, left, top, width, height):
        self.left, self.top, self.width, self.height = left, top, width, height


class _FakeLx:
    def __init__(self):
        self.hidden = False
        self.hide_calls = 0
        self.show_calls = 0
        self.geom_calls = []

    def is_hidden(self, wid):
        return self.hidden

    def hide_window(self, wid):
        self.hidden = True
        self.hide_calls += 1
        return True

    def show_window(self, wid):
        self.hidden = False
        self.show_calls += 1

    def window_rect(self, wid):
        return _FakeRect(80, 120, 1000, 700)

    def set_window_geometry(self, wid, x, y, w, h):
        self.geom_calls.append((x, y, w, h))


_orig_lx = gui.lx
_orig_hwnd = gui.get_wechat_hwnd
_orig_info = gui.messagebox.showinfo
_orig_err = gui.messagebox.showerror
_fake_lx = _FakeLx()
_offscreen_msgs = []
gui.lx = _fake_lx
gui.get_wechat_hwnd = lambda: (424242, 4)
gui.messagebox.showinfo = lambda *a, **k: _offscreen_msgs.append(("info", a[0] if a else ""))
gui.messagebox.showerror = lambda *a, **k: _offscreen_msgs.append(("error", a[0] if a else ""))
try:
    app.toggle_offscreen_wechat()
    app.update()
    check("离屏隐藏调用 hide_window", _fake_lx.hide_calls, 1)
    check("按钮文案切到「召唤微信回桌面」",
          "召唤" in str(app.btn_hide_wx.cget("text")), True)
    check("隐藏前记录了窗口几何", list(app.config_data.get("saved_window_rect") or [])[:2], [80, 120])
    app.toggle_offscreen_wechat()
    app.update()
    check("再次点击召回（show_window）", _fake_lx.show_calls, 1)
    check("召回时把窗口摆回记录的几何", _fake_lx.geom_calls, [(80, 120, 1000, 700)])
    check("按钮文案恢复「隐藏微信至离屏」",
          "隐藏" in str(app.btn_hide_wx.cget("text")), True)
    check_true("两次切换都有提示弹窗", len(_offscreen_msgs) == 2)
finally:
    gui.lx = _orig_lx
    gui.get_wechat_hwnd = _orig_hwnd
    gui.messagebox.showinfo = _orig_info
    gui.messagebox.showerror = _orig_err

print("\n=== 3c. 底部实时日志监听 ===")
check_true("日志面板在主区域分栏内（拖不出来也挤不掉）",
           len(app.main_pane.panes()) == 2 and int(app.txt_log.winfo_height()) > 60)
check("启动时已灌入历史日志", "历史行" in app.txt_log.get("1.0", tk.END), True)
app.append_log("📩 [测试] 收到新消息\n🚨 [测试] 发送失败\n")
_text = app.txt_log.get("1.0", tk.END)
check("新日志追加到面板", "📩 [测试] 收到新消息" in _text, True)
check_true("消息行着色", len(app.txt_log.tag_ranges("msg")) > 0)
check_true("错误行着色", len(app.txt_log.tag_ranges("error")) > 0)
app.cbo_log_mode.set("只看错误")
app.refresh_log_view()
_filtered = app.txt_log.get("1.0", tk.END)
check_true("筛选=只看错误时隐藏消息行", "收到新消息" not in _filtered)
check_true("筛选=只看错误时保留失败行", "发送失败" in _filtered)
app.cbo_log_mode.set("全部")
app.ent_log_filter.delete(0, tk.END)
app.ent_log_filter.insert(0, "收到新消息")
app.refresh_log_view()
check_true("关键字过滤生效", "收到新消息" in app.txt_log.get("1.0", tk.END)
           and "发送失败" not in app.txt_log.get("1.0", tk.END))
app.ent_log_filter.delete(0, tk.END)
app.refresh_log_view()
app.var_log_pause.set(True)
app.append_log("⏸️ [测试] 暂停期间的一行\n")
check_true("暂停时不刷新界面", "暂停期间的一行" not in app.txt_log.get("1.0", tk.END))
app.var_log_pause.set(False)
app.refresh_log_view()
check_true("恢复后补显暂停期间的日志", "暂停期间的一行" in app.txt_log.get("1.0", tk.END))
check_true("状态栏显示行数与文件", "面板" in app.lbl_log_status.cget("text")
           and os.path.basename(gui.LOG_FILE) in app.lbl_log_status.cget("text"))
app.clear_logs()
check("清空后面板为空", app.txt_log.get("1.0", tk.END).strip(), "")
check("清空后缓冲归零", len(app._log_buffer), 0)

print("\n=== 4. 通讯录增删改 ===")
before = len(app.tree_contacts.get_children())
app.ent_ct_remark.delete(0, "end")
app.ent_ct_remark.insert(0, "微信 2")
app.ent_ct_wxid.delete(0, "end")
app.ent_ct_wxid.insert(0, "wxid_27jcr0lsu2hc22")
app.add_contact()
app.update()
rows = [app.tree_contacts.item(i, "values") for i in app.tree_contacts.get_children()]
check_true("新增后能在表格里看到", any(r[0] == "微信 2" for r in rows))

app.ent_ct_remark.delete(0, "end")
app.ent_ct_remark.insert(0, "微信 2")
app.ent_ct_wxid.delete(0, "end")
app.ent_ct_wxid.insert(0, "wxid_changed")
app.add_contact()
app.update()
rows = [app.tree_contacts.item(i, "values") for i in app.tree_contacts.get_children()]
hits = [r for r in rows if r[0] == "微信 2"]
check("同备注改微信号是更新不是新增", len(hits), 1)
check("微信号已更新", hits[0][1], "wxid_changed")

app.ent_ct_remark.delete(0, "end")
app.ent_ct_remark.insert(0, "临时联系人")
app.ent_ct_wxid.delete(0, "end")
app.add_contact()
app.update()
tmp_id = [i for i in app.tree_contacts.get_children()
          if app.tree_contacts.item(i, "values")[0] == "临时联系人"]
check_true("临时联系人已加入", len(tmp_id) == 1)
app.tree_contacts.selection_set(tmp_id[0])
app.del_contact()
app.update()
rows = [app.tree_contacts.item(i, "values") for i in app.tree_contacts.get_children()]
check_true("删除生效", all(r[0] != "临时联系人" for r in rows))

print("\n=== 5. 保存后落盘 + 保留未暴露的 agent 子键 ===")
app.ent_agent_url.delete(0, "end")
app.ent_agent_url.insert(0, "http://127.0.0.1:9999/a2a")
app.spin_agent_timeout.set(240)
app.txt_agent_fallback.delete("1.0", "end")
app.txt_agent_fallback.insert("1.0", "兜底一\n兜底二")
app.var_pro_enabled.set(True)
app.spin_pro_port.set(8899)
app.spin_pro_hour.set(5)
app.var_pro_confirm.set(True)
app.save_ui_to_config(show_msg=False)

with open(tmp_cfg, "r", encoding="utf-8") as f:
    saved = json.load(f)

check("agent.url 已保存", saved["agent"]["url"], "http://127.0.0.1:9999/a2a")
check("agent.timeout 已保存", saved["agent"]["timeout_seconds"], 240)
check("agent.fallback 已保存", saved["agent"]["fallback_replies"], ["兜底一", "兜底二"])
check_true("agent.card_url 未丢失", saved["agent"].get("card_url", "").endswith("agent-card.json"))
check_true("agent.retries 未丢失", "retries" in saved["agent"])
check_true("agent.context_map_file 未丢失", "context_map_file" in saved["agent"])
check("agent.relation_urls 已保存", dict(saved["agent"].get("relation_urls") or {}), SEED_RELATION_URLS)
check("proactive.enabled", saved["proactive_send"]["enabled"], True)
check("proactive.port", saved["proactive_send"]["port"], 8899)
check("proactive.max_per_hour", saved["proactive_send"]["max_per_hour"], 5)
check("proactive.require_confirm", saved["proactive_send"]["require_confirm"], True)
check_true("proactive.confirm_expire_hours 未丢失",
           "confirm_expire_hours" in saved["proactive_send"])
contacts = saved.get("contacts") or []
check_true("contacts 已保存且含微信 2",
           any(str(c).find("微信 2") >= 0 for c in contacts))
check_true("原有 whitelist 未被破坏", isinstance(saved.get("whitelist"), list))

print("\n=== 6. 重新加载能还原 ===")
app.destroy()
app2 = gui.WeChatBotGUI()
app2.update()
check("重载 agent.url", app2.ent_agent_url.get().strip(), "http://127.0.0.1:9999/a2a")
check("重载 proactive.enabled", app2.var_pro_enabled.get(), True)
check("重载端口", str(app2.spin_pro_port.get()), "8899")
rows2 = [app2.tree_contacts.item(i, "values") for i in app2.tree_contacts.get_children()]
check_true("重载通讯录含微信 2", any(r[0] == "微信 2" for r in rows2))
app2.destroy()


print("\n=== 7. 启停按钮不卡界面（异步化） ===")
import subprocess as _sub
import time as _time

_orig_kill = gui.WeChatBotGUI.kill_bot_processes
gui.WeChatBotGUI.kill_bot_processes = lambda self: _time.sleep(0.7)  # 模拟真实耗时

# 绝不能在测试里真的拉起/杀掉守护进程：把 Popen 换成只记参数的假实现
_spawned = []
class _FakeProc:
    pid = 424242
_orig_popen = _sub.Popen
_sub.Popen = lambda *a, **k: (_spawned.append(a), _FakeProc())[1]
# 关键隔离：直接把 systemd 动作换成打桩函数，绝不动真实的 wechat-bot.service
_orig_systemd = gui._systemd_bot_action
gui._systemd_bot_action = lambda action: _spawned.append(
    ("systemctl", "--user", "restart" if action == "start" else "stop", "wechat-bot"))
gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.showerror = lambda *a, **k: None
gui.messagebox.showwarning = lambda *a, **k: None

app3 = gui.WeChatBotGUI()
app3.update()

check_true("启停已合并为一个开关按钮（btn_start/btn_stop 不再存在）",
           hasattr(app3, "btn_toggle") and not hasattr(app3, "btn_start") and not hasattr(app3, "btn_stop"))
check("未运行时开关文案=启动", str(app3.btn_toggle.cget("text")), "🚀 启动 AI 自动回复")

_t0 = _time.time()
app3.start_bot()
check_true("点击启动立刻返回（<0.25s）", _time.time() - _t0 < 0.25)
check("启动中 busy 标记", app3._bot_action_running, True)
check("启动中开关禁用", str(app3.btn_toggle.cget("state")), "disabled")
check("启动中文案", str(app3.btn_toggle.cget("text")), "⏳ 正在启动…")
for _ in range(100):
    app3.update()
    _time.sleep(0.05)
    if not app3._bot_action_running:
        break
check("启动完成后 busy 标记复位", app3._bot_action_running, False)
check("启动完成后开关可用", str(app3.btn_toggle.cget("state")), "normal")
check("启动完成后文案=停止守护", str(app3.btn_toggle.cget("text")), "⏹️ 停止守护")
check_true("启动走 systemd 重启、不再拉第二份进程",
           bool(_spawned) and "systemctl" in str(_spawned[-1]) and "wechat-bot" in str(_spawned[-1]))

# 同一个按钮再点一次 = 停止（toggle 语义）
_t0 = _time.time()
app3.toggle_bot()
check_true("点击停止立刻返回（<0.25s）", _time.time() - _t0 < 0.25)
check("停止中文案", str(app3.btn_toggle.cget("text")), "⏳ 正在停止…")
for _ in range(100):
    app3.update()
    _time.sleep(0.05)
    if not app3._bot_action_running:
        break
check("停止完成后 busy 标记复位", app3._bot_action_running, False)
check("停止完成后开关可用", str(app3.btn_toggle.cget("state")), "normal")
check("停止完成后文案=启动", str(app3.btn_toggle.cget("text")), "🚀 启动 AI 自动回复")
check_true("停止走 systemd stop",
           bool(_spawned) and "stop" in str(_spawned[-1]) and "systemctl" in str(_spawned[-1]))

_sub.Popen = _orig_popen
gui._systemd_bot_action = _orig_systemd
gui.WeChatBotGUI.kill_bot_processes = _orig_kill
app3.destroy()

print("\n" + "=" * 60)
if FAIL == 0:
    print(f"✅ 全部用例 PASS（共 {PASS} 项），GUI 配置面板零回退")
else:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
shutil.rmtree(tmpdir, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
