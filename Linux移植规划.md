# Linux 移植规划（暂不实施，仅存档）

> 状态：**已调研，未开工**。记录于 2026-11-01。等你决定要做了再按本文执行。
> 结论：**可行，但输入层必须从「鼠标点击」改成「纯键盘驱动」。**

---

## 1. 一句话结论

Windows 版有 90% 的逻辑（OCR 决策、Agent 大脑、通讯录映射、白名单、限流、HTTP 主动发送接口、待确认队列、GUI）是纯 Python，可以直接复用；
只有「找窗口 / 抓图 / 点击 / 键盘 / 剪贴板」这 5 组是 Win32 硬编码，需要换成 X11 实现。
**鼠标点击在 Linux 微信上无效（已实测），所以找人/发送全部改走键盘。**

---

## 2. 目标机实测环境

| 项目 | 值 |
|---|---|
| 主机 | `hy@<linux-host>`，Windows 侧 `<windows-host>`（同网段，ping 2ms） |
| 系统 | Ubuntu 26.04.1 LTS，kernel 7.0.0-42-generic |
| 桌面 | GNOME Shell **50.1** + GDM，会话类型 **wayland** |
| 显示服务 | Xwayland `:0`，屏幕 **1920×1200**，缩放 100% |
| X 授权 | `XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.NBA5X3` |
| 微信 | **4.1.13.26**，`/usr/bin/wechat` → `/opt/wechat/wechat`（181,208,032 字节，整包 754MB） |
| Python | 3.14.4（`/usr/bin/python3`），pip3 26.1.2 |
| 已有工具 | `xdotool` `xwininfo` `xclip` |
| 缺的工具 | `wmctrl` `scrot` `import` `xwd` `grim` `gnome-screenshot` `ffmpeg` `tesseract` **`python3-tk`** |
| sudo | 需要交互密码，无 NOPASSWD |

**重要**：`/usr/share/xsessions/` 目录不存在，`xorg` 未安装，**GNOME 50 已移除 X11 会话**。
所以「切到 Xorg 会话」这条路在本机不成立，除非 `sudo apt install xorg xfce4` 另开一个 X11 会话。

---

## 3. 实测结论（证据）

### ✅ 能用

| 能力 | 做法 | 验证结果 |
|---|---|---|
| 窗口发现 | `xdotool search` / `xwininfo` | 找到微信主窗，`_NET_CLIENT_LIST` 正确列出 |
| 抓图 | python-xlib `window.get_image(..., X.ZPixmap, 0xffffffff)` + Pillow | 抓到 880×640 主窗，像素完全正确 |
| 键盘注入 | `xdotool key` | **Ctrl+F 画面变化（diff 0.052），Esc 后 md5 完全还原原图** |
| 剪贴板 | `xclip -selection clipboard` | 可写可读 |
| OCR | `onnxruntime 1.31.1` 有 **cp314** manylinux wheel | 依赖可装 |

抓图关键代码（已验证，注意 stride 要自己算）：

```python
from Xlib import display as xdisplay, X
from PIL import Image

d = xdisplay.Display()
w = d.create_resource_object("window", wid)
g = w.get_geometry()
raw = w.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff)
data = raw.data
stride = len(data) // g.height          # raw 没有 bytes_per_line 属性
img = Image.frombytes("RGB", (g.width, g.height), data, "raw", "BGRX", stride)
```

### ❌ 不能用

**鼠标点击**：`xdotool mousemove` + `click 1` 发出去后，微信画面 diff = **0.0000**，完全没反应。
排除过：窗口激活成功（`getactivewindow` 变成微信）、指针坐标正确（`getmouselocation` 反馈准确）、
窗口没被遮挡。结论是 **XTest 合成点击不被微信接受**（Qt/Chromium 只认真实 seat 事件）。

---

## 4. 核心前提：微信必须用 X11 模式启动

默认启动的微信是 **Wayland 原生**客户端（`WeChatAppEx` 带 `--ozone-platform=wayland`），
`xwininfo` / `_NET_CLIENT_LIST` 里根本看不到它，`xdotool` 完全无法操作。

必须这样启动：

```bash
env -u WAYLAND_DISPLAY \
    QT_QPA_PLATFORM=xcb \
    XDG_SESSION_TYPE=x11 \
    DISPLAY=:0 \
    XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.NBA5X3 \
    XDG_RUNTIME_DIR=/run/user/1000 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    setsid nohup /usr/bin/wechat >/tmp/wechat.log 2>&1 &
```

加 `QT_QPA_PLATFORM=xcb` 后，微信会变成 Xwayland 客户端：
`xwininfo -root -children` 出现 `0xe00007 "微信": ("wechat" "wechat")`，`_NET_CLIENT_LIST` 也列出来了，
`WeChatAppEx` 的 `--ozone-platform` 自动变成 **x11**。

启动日志只有两条无害告警，可忽略：
```
vaInitialize failed: unknown libva error
InitializeSandbox() called with multiple threads in process gpu-process
```

---

## 5. 键盘流操作设计（替代鼠标）

### 5.1 找联系人并打开会话

```
1. xdotool windowactivate --sync <wid>       # 必须激活，键盘事件才进微信
2. xdotool key --clearmodifiers ctrl+f       # 打开搜索框
3. xclip -selection clipboard -i <名字文件>   # 中文必须走剪贴板
4. xdotool key --clearmodifiers ctrl+v       # 粘贴
5. 等待结果刷新（建议 1.5s）
6. xdotool key Down（可选）+ Return          # 选中第一条并打开
7. 抓图复核顶栏标题（走原有 OCR 逻辑）        # 失败就 Esc 回退，绝不盲发
```

### 5.2 发送消息

```
1. 确认当前会话是目标（顶栏 OCR 复核）
2. xclip 写入要发的话
3. xdotool key --clearmodifiers ctrl+v       # 粘贴进输入框
4. xdotool key Return                        # 发送
5. 抓图复核：绿底气泡条数 +1 即成功（沿用 Windows 的 bands 判据，需按深色主题重测阈值）
```

**优势**：全程不用点坐标，天然免疫 Windows 版踩过的「点错窗口 / DPI 偏移 / 窗口被别的程序盖住」那一整类坑。

---

## 6. 平台适配层设计

Windows 版现在的 5 组 Win32 函数（`wechat_ai_bot_dev.py`）：

| Windows 函数 | 职责 | Linux 替换方案 |
|---|---|---|
| `get_wechat_hwnd()` | 按类名+标题找主窗 | `xdotool search --class wechat` + 按 pid 过滤 |
| `grab_wechat_window(hwnd)` | `PrintWindow` 抓图 | python-xlib `get_image`（见 §3） |
| `post_click_client_point(hwnd,x,y)` | `PostMessage` 点击 | **删除**，改走键盘流 |
| 键盘（`keybd_event`/`ctrl+v`） | 组合键 | `xdotool key --clearmodifiers <combo>` |
| 剪贴板（`pyperclip`/`win32clipboard`） | 读写剪贴板 | `xclip`（子进程）或 python-xlib 自实现 |
| `ensure_wechat_visible(hwnd)` | 最小化/隐藏时还原 | `xdotool windowmap` + `windowactivate` |

**建议做法**：新建 `platform_win.py` / `platform_linux.py`，把上面 5 组抽成同名接口，
`wechat_ai_bot_dev.py` 顶部按 `sys.platform` 选一个导入。这样 Windows 侧零改动、可继续维护。

### 需要替换/新增的函数清单

- 新增 `platform/__init__.py`：`get_backend()` 按平台返回实现
- 新增 `platform/base.py`：`find_window` / `grab` / `key_combo` / `paste_text` / `activate` / `open_chat` / `send_text`
- 改 `wechat_ai_bot_dev.py`：`get_wechat_hwnd`、`grab_wechat_window`、`post_click_client_point`、`_key_combo`、`safe_clipboard_copy`、`ensure_wechat_visible`、`find_contact`、`_search_and_open`、`send_reply_instant`
- 不动：OCR 解析、气泡/红点识别、`AgentClient`、`contacts`、白名单、限流、`/send` `/status` `/contacts` `/pending` 接口、待确认队列
- GUI：`wechat_bot_gui_dev.py` 需要 `sudo apt install python3-tk`；进程管理部分从 `psutil + taskkill` 换成 `psutil` 通用写法

---

## 7. 依赖安装清单

```bash
# 系统层（需要 sudo 密码）
sudo apt install python3-tk xdotool xclip

# Python 层（不污染系统，装到 ~/.local）
pip3 install --user --break-system-packages \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
    python-xlib pillow numpy requests psutil rapidocr-onnxruntime
```

- `onnxruntime` 已确认有 **cp314** wheel（1.31.1，manylinux_2_28_x86_64），能直接装
- `rapidocr_onnxruntime` 走 py3-none-any wheel（镜像上是 1.2.3），装完需要确认 `RapidOCR(intra_op_num_threads=4)` 这个 kwarg 在旧版是否支持
- `python3 -m venv` 在这台机上**会失败**（缺 ensurepip），所以别用 venv，用 `--user`

---

## 8. 已踩过的坑（务必先看）

1. **单实例锁会伪装成「启动崩溃」**
   从 SSH 再启动一个微信，会 `exit 255`、48ms、无任何输出。**不是代码问题**，是旧进程还活着。
   而且 `pkill -f '/opt/wechat/wechat'` 杀不掉用 `/usr/bin/wechat` 启动的进程（argv[0] 不同）。
   正确做法：`pgrep -f '^/usr/bin/wechat$|^/opt/wechat/wechat$'` 精确定位后再 kill。

2. **SSH 启动 GUI 必须补全会话环境**
   缺 `WAYLAND_DISPLAY` / `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS` 会静默退出。

3. **X11 工具必须手动补 XAUTHORITY**
   否则报 `Authorization required, but no authorization protocol specified`。

4. **`xdotool search --name '微信'` 会误命中别的窗口**
   实测把 Chrome 的一个「文件传输助手 - 搜一搜」页面当成微信抓走了。
   **必须按 `--class wechat` 或按 pid 过滤**，不能只靠标题模糊匹配。

5. **GNOME 的截图 D-Bus 接口不好调**
   `org.gnome.Shell.Screenshot.Screenshot` 首参不是字符串，各种引号写法都报类型错误。
   直接用 python-xlib 抓图即可，别走这个接口。

6. **`xclip` 有进程存活依赖**
   `xclip` 是把内容挂在自己进程上的，进程退出剪贴板就没了。
   长时间运行的场景要用 `xclip -loops 0` 或自己用 X11 Selection 实现。

---

## 9. 尚未验证 / 风险项

| 风险 | 说明 | 应对 |
|---|---|---|
| **Enter 打开搜索结果** | 只验到「粘贴后画面变化」，Enter 之后是否真的打开会话还没复验（那次窗口 id 选错抓到了 Chrome） | 移植第一步就要复验这条 |
| 搜索结果在哪 | Linux 版的搜索结果是主窗内嵌还是独立弹窗，未确认（Windows 是独立 `Qt51514QWindowToolSaveBits` 弹窗） | 抓图 + OCR 判定，两种都兼容 |
| 深色主题判据 | Linux 版是深色主题，Windows 版是浅色；绿底气泡阈值 `(pg>pr+18)&(pg>pb+18)&(pg>45)` 需要重测 | 用「绿底横带条数 +1」的抗滚动判据，重新标定阈值 |
| 布局常量 | 微信 4.1.13.26（Linux）vs 4.1.15.13（Windows），`sidebar_w` / `chat_start_x` / `header_h` / `input_h` 四个常量要重测（Linux 实测 880×640 窗口） | 打印 OCR 框坐标重新标定 |
| OCR 版本差异 | Linux 侧 rapidocr 1.2.3 vs Windows 侧 1.4.4，API 可能有出入 | 装完先跑一次引号识别对比 |
| 无 sudo 时 | 装 `python3-tk` 需要密码 | 让用户执行一次，或 GUI 先不做 |
| 多用户/锁屏 | 锁屏后 X11 抓图可能返回黑图 | 加黑图检测 + 跳过本轮（Windows 版已有该逻辑） |

---

## 10. 备选方案（如果键盘流走不通）

按优先级：

1. **纯键盘流**（本文主方案）——零额外权限，最稳
2. **`ydotool`**（走 `/dev/uinput` 模拟真实输入）——能解决点击问题，但需要 root 或把用户加进 `input` 组
3. **Mutter RemoteDesktop portal**（D-Bus）——GNOME 官方远程控制通道，需要用户授权一次
4. **Xvfb + x11vnc**（无头 X 服务器）——微信跑在虚拟屏里，XTest 百分百有效；代价是得用 VNC 看，且要另配一套启动脚本

---

## 11. 验收标准

移植完成后，以下全部通过才算完：

- [ ] Linux 上微信以 xcb 模式启动，`xdotool search --class wechat` 能稳定拿到主窗
- [ ] `grab` 抓图成功，OCR 能正确读出侧栏 5 个以上会话名
- [ ] 「按备注找联系人」端到端成功，顶栏 OCR 复核通过（< 20s）
- [ ] 「发一条消息」端到端成功，绿底气泡判据通过，**只发 1 条不重发**
- [ ] Agent 大脑接通 DSH A2A（`http://<windows-ip>:44708/a2a`），JSON 契约解析正常
- [ ] 主动发送 HTTP 接口 `POST /send` 可用
- [ ] 白名单 / 通讯录 / 限流 / 待确认队列行为与 Windows 版一致
- [ ] Windows 侧六套回归测试**全部仍然全绿**（证明重构没破坏原平台）

---

## 12. 工作量预估

| 项 | 预估 |
|---|---|
| `platform/` 平台层（两份实现） | ~250 行新代码 |
| `wechat_ai_bot_dev.py` 抽接口 | ~80 行改动 |
| Linux 键盘流 `find_contact` / `send` | ~150 行 |
| GUI 跨平台进程管理 + tk 依赖 | ~60 行 |
| 深色主题判据重标定 + 布局常量重测 | 半天实测 |
| **合计** | **约 1.5 ~ 2 个工作日** |

---

## 13. 执行顺序（真要做的时候）

1. 用户执行 `sudo apt install python3-tk xdotool xclip`，并确认微信已用 §4 的命令启动
2. 复验「Ctrl+F → 粘贴 → Enter 打开会话」这一条（唯一未闭环的关键链路）
3. 抽 `platform/` 层，先只实现 `find_window` + `grab` + `key_combo` + `paste_text`
4. 移植 `find_contact`（键盘流），在真机上跑通找人
5. 移植 `send_reply_instant`，重标定绿底气泡阈值
6. 装 OCR，重标定四个布局常量
7. 接 Agent A2A、接 HTTP 主动发送接口
8. 跑全量验收清单（§11），并回归 Windows 六套测试

---

## 14. 当前现场快照（2026-11-01 04:20）

- Linux 微信 **已登录**（账号「杠精」），主窗 **880×640**，深色主题
- 微信以 **xcb 模式** 在跑（`WeChatAppEx` 带 `--ozone-platform=x11`），pid 20999
- 探针脚本留在 `E:\ai\WeChat-AI-AutoReply\`：`_probe_linux.py` ~ `_probe_linux28.py`，都是一次性的，可随时删
- 抓到的截图：`_linux_wx_x11.png`（登录窗）、`_linux_wx_c.png`（主窗）、`_linux__s1.png`（误抓的 Chrome 页）
