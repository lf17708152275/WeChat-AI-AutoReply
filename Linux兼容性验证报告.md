# Linux 兼容性验证报告（实机实测）

> 记录时间：2026-11-01
> 机器：Debian 13 (trixie)，kernel 6.12.113，Python 3.13.5，xrdp 虚拟屏 1920x1200
> 微信：Linux 原生版 4.1.26（xwechat_linux，`--ozone-platform=x11`），窗口 878x635
> 结论一句话：**能跑**。核心链路（抓图 → OCR 读消息 → 点击/粘贴 → 发送）已在实机全部跑通，
> 原项目需要替换的只有约 90 行 Win32 调用。

---

## 一、能力矩阵（全部实机验证过）

| 能力 | 实现方式 | 实测结果 | 耗时 |
|---|---|---|---|
| 抓微信窗口图 | `XGetImage` 抓 root + 按窗口几何裁剪（PIL） | ✅ 稳定 | 0.14s |
| 窗口定位 | 递归遍历窗树按 `WM_CLASS=wechat` 找 >400x300 的主窗 | ✅ 窗口被 WM 重父化也能找到 | <0.1s |
| 恢复/前置窗口 | `XMapWindow` + `_NET_ACTIVE_WINDOW` + `configure(Above)` + 轮询 `IsViewable` | ✅ 最小化后能拉回来 | 0.3s |
| 键盘注入 | `XTest` `XTestFakeKeyEvent`（keycode 由 keymap 反查） | ✅ Ctrl+F / Ctrl+A / Ctrl+V / Enter 全部生效 | <0.1s |
| 鼠标注入 | `XTest` `XTestFakeMotionEvent` + `XTestFakeButtonEvent` | ✅ 点会话行、点输入框、点发送均生效 | <0.1s |
| 中文输入 | 常驻 GTK3 剪贴板守护（`Gtk.Clipboard`）+ Ctrl+V | ✅ 中文/emoji 正常，无输入法干扰 | 0.6s |
| OCR 读屏 | RapidOCR 1.2.3（onnxruntime 1.31.1，CPU） | ✅ 中文识别准确，带坐标 | 1.2~1.9s/帧 |
| 打开会话 | 左侧列表 OCR 定位同名行 → 点击 → OCR 校验顶栏标题 | ✅ 5.8s（含 3 次 OCR），标题校验通过 | 5.8s |
| 读消息 | OCR 聊天区 + 按 x 坐标判左右（self/other） | ✅ 能读到自己/对方气泡文本与位置 | 1.5s |
| 发送消息 | Ctrl+A → 粘贴 → Enter（输入法抢占时改点「发送」按钮） | ✅ 消息落到会话里 | 3s |

实测截图证据：`/tmp/wx_now.png`（会话）、`/tmp/open_ok2.png`（打开"文件传输助手"）、
`/tmp/dad1.png`（张三会话气泡）。

---

## 二、原项目要改的地方（很小）

`wechat_ai_bot_dev.py` 共 2904 行，Win32 相关只有 12 个函数、约 90 行：

| 函数 | 行号 | Windows 做法 | Linux 替换 |
|---|---|---|---|
| `ensure_default_desktop()` | 43-48 | `OpenInputDesktop` | 直接 `return`（无操作） |
| `get_dpi_scale()` | 227-232 | `user32.GetDpiForWindow` | 返回 `1.0`（本机 96dpi 实测 1:1） |
| `get_wechat_hwnd()` | 259-295 | `EnumWindows` + 类名 | 递归 `query_tree()` 按 `WM_CLASS` |
| `grab_wechat_window()` | 304-410 | `PrintWindow`/`ImageGrab` | root `XGetImage` + 裁剪 |
| `activate_wechat()` | 410-451 | `SetForegroundWindow` | `_NET_ACTIVE_WINDOW` client message |
| `_nonclient_offset()` | 451-468 | 边框偏移 | `(0, 0)` |
| `ensure_wechat_visible()` | 468-491 | `ShowWindow` | `XMapWindow` + `configure` |
| `post_click_client_point()` / `_real_click_client_point()` | 491-519 / 1706-1712 | `PostMessage`/`SendInput` | `XTest` 绝对坐标点击 |
| `_key_combo()` | 1508-1515 | `keybd_event` | `XTest` 按键 |
| `safe_clipboard_copy()` | 1403 | `win32clipboard` | GTK 剪贴板守护 |
| `send_reply_instant()` | 2055-2115 | 点击 + `keybd_event` | 点击 + `XTest` |
| `_wechat_pid()` / `_find_wechat_popup()` | 1604-1647 | `GetWindowThreadProcessId` | 窗树遍历按 class 过滤 |

**平台无关、可直接复用**（约 2600 行）：`compute_wechat_layout()`（纯比例算法）、
`parse_chat_bubbles_chromatic()`、`extract_pending_incoming_messages()`、
`generate_ai_reply()` / `generate_agent_reply()`、配置/联系人/outbox、HTTP 服务。

> 也就是说：这不是"重写一遍"，而是把 12 个 I/O 函数换掉，业务逻辑原样跑。

---

## 三、踩到的坑（后续实现必须处理）

1. **输入法是最大干扰**：微信进程带 `GTK_IM_MODULE=fcitx` / `QT_IM_MODULE=fcitx`，
   XTest 逐键打 ASCII 会被 fcitx5 吞成候选词（打 "hello linux" 出"合理我…"）。
   → 一律走剪贴板粘贴，不要逐键打字。
2. **`xwd -id` 抓窗口会拿到过期内容**（Qt + xrdp 下），必须抓 root 再按几何裁剪。
3. **窗口会漂移**：用户拖动/WM 重排都会改 `+x+y`，点击前必须现查几何，不能缓存。
4. **搜索下拉会误开「搜一搜」内嵌浏览器窗**（`WeChatAppEx`）：搜索建议/历史行点下去
   不是开聊天。→ 优先点左侧列表已有行；用搜索时只点"文字与名字完全相等"的那行，
   并关掉残留的 `WeChatAppEx` 窗。
5. **剪贴板要有常驻 owner**：一次性 GTK 进程粘贴要等它退出（8s+）；
   改成常驻守护后单次粘贴 0.2s。
6. **无 sudo、无 pip、无 tkinter、无 xclip/xdotool**：pip 用 `python3 -m zipfile -e` 解 wheel
   到 `~/.local/lib/python3.13/site-packages` 解决；剪贴板用 GTK 解决；GUI 需装 `python3-tk`。
7. **OCR 误判**：公众号文章卡片（居中、多行）会被当成"自己发的消息"。
   → 判左右之外还要校验气泡颜色/圆角，或按 `layout["chat_start_x"]` 分栏。

---

## 四、验证状态

| 项 | 状态 |
|---|---|
| 抓图 / OCR / 点击 / 粘贴 / 发送 | ✅ 实机通过 |
| 打开指定会话（列表点击 + OCR 校验标题） | ✅ 实机通过 |
| 读自己发的消息 | ✅ 实机通过 |
| 读**对方**发来的消息 | ⏳ 待一条真实来信（手机发到"文件传输助手"未同步成功） |
| 群聊（ai聊天群）消息读取 | ⏳ 未测 |
| 六套回归测试 | ❌ 无法在 Linux 跑（断言里含 Win32 句柄/截图差异），需 Windows 真机 |

---

## 五、建议的落地顺序

1. **P0（0.5 天）**：把 `linux_wechat_driver.py` 的 12 个方法接进 `wechat_ai_bot_dev.py`，
   加 `sys.platform` 分支，Windows 行为零改动。
2. **P1（0.5 天）**：用真实来信校准"左右气泡"判定 + 消息去重（签名 = 文本+坐标哈希）。
3. **P2（1 天）**：回归测试里把"截图比对"改为"OCR 文本断言"，让同一套用例在两端都能跑。

---

## 附：驱动用法

```bash
export PYTHONPATH=$HOME/.local/lib/python3.13/site-packages
export DISPLAY=:10 XAUTHORITY=$HOME/.Xauthority

python3 linux_wechat_driver.py probe          # 窗口/几何/焦点
python3 linux_wechat_driver.py grab out.png   # 截图
python3 linux_wechat_driver.py open 张三       # 打开会话（OCR 校验）
python3 linux_wechat_driver.py send "你好"     # 发消息
```

```python
from linux_wechat_driver import WeChatLinux
wx = WeChatLinux()
wx.open_chat("文件传输助手")        # True/False（顶栏标题校验）
wx.read_messages()                 # [('self'|'other', text, cx, cy), ...]
wx.last_incoming()                 # (text, cy) | None
wx.send("自动回复内容")
```

---

## 六、移植实施结果（2026-11-01，已完成）

原技能矩阵里的 12 个 Win32 替换点已全部落地，**Windows 代码路径逐字未改**，
所有改动都是 `if IS_WIN: … else: …` 分支。

### 6.1 新增/改动的文件

| 文件 | 作用 |
| --- | --- |
| `wechat_linux_io.py` | **新增**。X11 后端：窗口发现/几何/抓图/激活/点击/按键/剪贴板/搜索弹窗，全部函数签名对齐原 Win32 实现 |
| `wechat_ai_bot_dev.py` | 12 处 Win32 调用加 Linux 分支；新增 `_ocr_call()`（rapidocr 只吃 ndarray）、`_linux_search_and_open()`、`_linux_send_reply()` |
| `wechat_bot_gui_dev.py` | Win32/ctypes 条件化；`fast_process_names()` 走 `/proc`；启停守护进程用 `Popen`；打开日志用 `xdg-open` |
| `start_linux.sh` | **新增**。自动挑可用 DISPLAY（本机 `:0` 无 cookie，实际是 `:10`），带 tkinter 兜底环境变量 |

### 6.2 跑起来

```bash
cd ~/桌面/project/WeChat-AI-AutoReply
./start_linux.sh          # AI 守护进程
./start_linux.sh gui      # Tk 控制中心
```

### 6.3 实测结果（DISPLAY=:10，微信 4.1.26）

| 项目 | 结果 |
| --- | --- |
| 抓图 | 0.08 s / 876×635 |
| `_scan_window` 顶栏识别 | 0.86 s，标题 `文件传输助手` |
| `_search_and_open("文件传输助手")` | ✅ 7.3 s |
| `find_contact("张三")` / `("文件传输助手")` | ✅ 2.0 s / 3.1 s |
| `send_reply_instant()` | ✅ 3.9 s（日志 `✅ [Linux 静默秒回成功]`） |
| 主循环 | 3 轮各 ~1.3 s，无异常 |
| GUI 启动 | ✅ 窗口 1320×880，顶栏显示「🟢 微信客户端: 正常连接 (WeChat 4.x)」 |
| GUI 启动/停止守护进程 | ✅ 启动得到 PID，停止后进程确实消失 |
| 七套回归测试 | ✅ 全部 PASS（防串发 6+、通讯录 77 断言、待确认队列 44、主动发送 64、Agent 大脑、待确认 GUI 23、GUI 配置 58） |

### 6.4 环境依赖（本机无 sudo，全部装到用户目录）

```bash
# Python 包
python3 -m pip install --user numpy pillow python-xlib rapidocr-onnxruntime
# tkinter（系统没装 python3-tk 时）
cd /tmp && apt-get download python3-tk python3.13-tk libtk8.6 libtcl8.6 tk8.6 tcl8.6 libxss1
for d in /tmp/*.deb; do dpkg-deb -x "$d" ~/.local/tkroot; done   # start_linux.sh 会自动带好环境变量
```

### 6.5 真实来信闭环（已闭环 ✅）

2026-11-01 17:58 用户从手机发来「等下吃什么？」，桌面端收到（侧栏红点 + 微信 DB mtime 同步更新）；
18:01 在 Linux 上跑主循环，日志完整走通：

```
📩 [当前会话锁定·连发聚合] 收到当前好友【张三】新消息(1条): "等下吃什么？"
🧠 [已交给 Agent] 好友【张三】: "等下吃什么？"（后台思考中，主循环继续巡检）
🚨 [Agent 降级] transport_error: ... 127.0.0.1:44708 ... Connection refused → 改用固定可爱回复兜底
💬 [Agent 决策] 好友【张三】: 啊呀，脑子突然卡壳了，晚点回你哈  (urgency=now, 耗时 0.8s)
📤 正在回复【张三】: 啊呀，脑子突然卡壳了，晚点回你哈
✅ [Linux 静默秒回成功] 已回复【张三】
```

界面上左侧灰色气泡（对方 17:58）+ 右侧绿色气泡（机器人 18:01）同框，肉眼可核。

> 注：白名单为空时主循环不会回复任何人——测试时临时把 `whitelist` 置为 `["张三"]`，
> 测完已还原为 `[]`。上线前记得在 GUI「白名单与防扰」里把要托管的好友加进去。

### 6.6 仍未覆盖

1. **群聊未读数**：`ai聊天群` 侧栏显示 3 条未读，聊天区却整块空白（OCR 也读不到），
   像是该会话的消息是图片/表情或渲染未触发，待确认。
2. **Agent 大脑未接入**：`agent.url` 指向 `http://127.0.0.1:44708/a2a`，该端口当前没有服务，
   所以走的是兜底回复。要真人级回复，需要把 `agent.url` 指向实际在跑的 DSH Agent（或在 GUI 里配大模型 API）。
3. **Windows 侧未回归**：本次改动全部走分支（Windows 代码逐字未动），但按项目硬约束，
   仍需在 Windows 真机跑一遍六套回归 + 冒烟。

---

## 七、遮挡误读的修复（2026-11-01 晚）

实测事故：GUI 窗口压在微信上面时，`grab_window` 直接抓 root 再裁剪，把 **GUI 里的人设 Prompt 文本**
OCR 成了"好友发来的消息"，日志里出现：

```
📩 收到当前好友【张三】新消息(1条): "随和幽默/接梗达人，你现在是微信账号的主人……"
📩 收到当前好友【张三】新消息(1条): "口实时运行日志（Auto-Scroll)"
```

修复（`wechat_linux_io.py`）：

- 新增 `stacked_above(wid)`：读 `_NET_CLIENT_LIST_STACKING`，取压在微信之上的可见窗口
- 新增 `occlusion_ratio(wid)`：按矩形交集算被遮挡面积占比
- `grab_window` 抓图前先判遮挡：超过 `_OCCL_TOLERATE = 0.12` 就先 `activate()` 抬窗口再测；
  抬起后仍被压住就 **返回 None 跳过本轮**，并打印一次 `[遮挡]` 日志（同一比例只打一次，防刷屏）

效果：被遮挡时不再把别人的界面当成微信内容；轻微遮挡（<12%）照常工作，不会频繁抢焦点。
