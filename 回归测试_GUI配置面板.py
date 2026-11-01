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
gui.WeChatBotGUI.start_background_monitors = lambda self: None

with open(tmp_cfg, "r", encoding="utf-8") as _f:
    try:
        _seed = json.load(_f)
    except Exception:
        _seed = {}
SEED_PRO_ENABLED = bool((_seed.get("proactive_send") or {}).get("enabled", False))

print("=== 1. 实例化与标签页 ===")
app = gui.WeChatBotGUI()
app.update()
tabs = [app.notebook.tab(t, "text").strip() for t in app.notebook.tabs()]
check("标签页数量", len(tabs), 6)
check_true("含通讯录页", any("通讯录" in t for t in tabs))
check_true("含智能大脑页", any(("大脑" in t) for t in tabs))

# 原有 4 个页签位置必须保持不变，新增两页排在最后
_orig = ["AI 引擎设置", "人设与指令 Prompt", "白名单与防扰", "AI 仿真测试沙盒"]
check("原有 4 页顺序未变",
      [_orig[i] in tabs[i] for i in range(min(4, len(tabs)))],
      [True, True, True, True])
check("第 5 页是通讯录映射", ("通讯录" in tabs[4]) if len(tabs) > 4 else False, True)
check("第 6 页是大脑/主动发送", ("大脑" in tabs[5]) if len(tabs) > 5 else False, True)

print("\n=== 2. 控件存在性 ===")
for attr in ("tree_contacts", "ent_ct_remark", "ent_ct_wxid",
             "ent_agent_url", "ent_agent_prefix", "spin_agent_timeout", "spin_agent_defer",
             "txt_agent_fallback", "var_agent_enabled",
             "var_pro_enabled", "var_pro_wl_only", "var_pro_confirm", "var_pro_search",
             "ent_pro_host", "spin_pro_port", "spin_pro_hour", "spin_pro_day", "lbl_pro_status"):
    check_true(f"控件 {attr}", hasattr(app, attr))

print("\n=== 3. 初始加载 ===")
check("Agent 默认开启", app.var_agent_enabled.get(), True)
check("Agent 地址", app.ent_agent_url.get().strip(), "http://127.0.0.1:44708/a2a")
check("主动发送勾选与配置文件一致", app.var_pro_enabled.get(), SEED_PRO_ENABLED)
check("端口默认", str(app.spin_pro_port.get()), "8765")

print("\n=== 4. 通讯录增删改 ===")
before = len(app.tree_contacts.get_children())
app.ent_ct_remark.delete(0, "end")
app.ent_ct_remark.insert(0, "测试联系人")
app.ent_ct_wxid.delete(0, "end")
app.ent_ct_wxid.insert(0, "wxid_abcdefghijklmno")
app.add_contact()
app.update()
rows = [app.tree_contacts.item(i, "values") for i in app.tree_contacts.get_children()]
check_true("新增后能在表格里看到", any(r[0] == "测试联系人" for r in rows))

app.ent_ct_remark.delete(0, "end")
app.ent_ct_remark.insert(0, "测试联系人")
app.ent_ct_wxid.delete(0, "end")
app.ent_ct_wxid.insert(0, "wxid_changed")
app.add_contact()
app.update()
rows = [app.tree_contacts.item(i, "values") for i in app.tree_contacts.get_children()]
hits = [r for r in rows if r[0] == "测试联系人"]
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
check("proactive.enabled", saved["proactive_send"]["enabled"], True)
check("proactive.port", saved["proactive_send"]["port"], 8899)
check("proactive.max_per_hour", saved["proactive_send"]["max_per_hour"], 5)
check("proactive.require_confirm", saved["proactive_send"]["require_confirm"], True)
check_true("proactive.confirm_expire_hours 未丢失",
           "confirm_expire_hours" in saved["proactive_send"])
contacts = saved.get("contacts") or []
check_true("contacts 已保存且含测试联系人",
           any(str(c).find("测试联系人") >= 0 for c in contacts))
check_true("原有 whitelist 未被破坏", isinstance(saved.get("whitelist"), list))

print("\n=== 6. 重新加载能还原 ===")
app.destroy()
app2 = gui.WeChatBotGUI()
app2.update()
check("重载 agent.url", app2.ent_agent_url.get().strip(), "http://127.0.0.1:9999/a2a")
check("重载 proactive.enabled", app2.var_pro_enabled.get(), True)
check("重载端口", str(app2.spin_pro_port.get()), "8899")
rows2 = [app2.tree_contacts.item(i, "values") for i in app2.tree_contacts.get_children()]
check_true("重载通讯录含测试联系人", any(r[0] == "测试联系人" for r in rows2))
app2.destroy()


print("\n=== 7. 启停按钮不卡界面（异步化） ===")
import time as _time

_orig_kill = gui.WeChatBotGUI.kill_bot_processes
gui.WeChatBotGUI.kill_bot_processes = lambda self: _time.sleep(0.7)  # 模拟真实耗时
gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.showerror = lambda *a, **k: None
gui.messagebox.showwarning = lambda *a, **k: None

app3 = gui.WeChatBotGUI()
app3.update()

_t0 = _time.time()
app3.start_bot()
check_true("点击启动立刻返回（<0.25s）", _time.time() - _t0 < 0.25)
check("启动中 busy 标记", app3._bot_action_running, True)
check("启动中按钮禁用", str(app3.btn_start.cget("state")), "disabled")
for _ in range(100):
    app3.update()
    _time.sleep(0.05)
    if not app3._bot_action_running:
        break
check("启动完成后 busy 标记复位", app3._bot_action_running, False)
check("启动完成后按钮恢复", str(app3.btn_start.cget("state")), "normal")

_t0 = _time.time()
app3.stop_bot()
check_true("点击停止立刻返回（<0.25s）", _time.time() - _t0 < 0.25)
for _ in range(100):
    app3.update()
    _time.sleep(0.05)
    if not app3._bot_action_running:
        break
check("停止完成后 busy 标记复位", app3._bot_action_running, False)
check("停止完成后按钮恢复", str(app3.btn_stop.cget("state")), "normal")

gui.WeChatBotGUI.kill_bot_processes = _orig_kill
app3.destroy()

print("\n" + "=" * 60)
if FAIL == 0:
    print(f"✅ 全部用例 PASS（共 {PASS} 项），GUI 配置面板零回退")
else:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
shutil.rmtree(tmpdir, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
