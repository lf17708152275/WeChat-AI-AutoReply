<div align="center">

# 🤖 WeChat-AI-AutoReply
### 微信 AI 自动回复 (Zero-Mouse & Off-Screen Edition)

**基于视觉感知与 Win32 无痕挂载的 PC 微信 AI 自动回复**  
*100% 物理鼠标 0 移动 · 虚拟离屏隐形挂载 · 毫秒级视觉双轨感知 · 白名单硬核物理防火墙*

<p align="center">
  <a href="https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply/releases/latest">
    <img src="https://img.shields.io/badge/Release-v1.0.5-brightgreen?style=flat-square" alt="Release">
  </a>
  <img src="https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6?logo=windows&style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/WeChat-3.x%20%7C%204.x%20Qt-07C160?logo=wechat&style=flat-square" alt="WeChat">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&style=flat-square" alt="Python">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-orange?style=flat-square" alt="License">
  </a>
</p>

</div>

---

## 🌟 核心技术与产品亮点

- **🖱️ 100% 物理鼠标 0 移动**  
  摒弃传统自动化工具（PyAutoGUI、按键精灵）抢夺光标的落后方式，采用纯底层的 Win32 消息句柄投递（PostMessage / SendMessage）。在后台极速切入、粘贴、发送消息，**您的鼠标指针完全不移动、键盘打字不被打断**！

- **👻 虚拟离屏隐形挂载（Off-Screen Mode）**  
  微信窗口可一键收纳进 Windows 虚拟离屏空间（坐标负向离屏区域），桌面 0 弹窗、无视觉干扰。即便微信被完全遮挡或离屏隐身，DWM / PrintWindow 依然能高保真捕捉画面并静默秒回。

- **👁️ 毫秒级双轨视觉感知**  
  - **几何与像素级感知**：动态咬合微信侧边栏与未读红点，自适应 100% ~ 250% 任意 DPI 缩放及窗口自由拉伸。
  - **RapidOCR 本地高频引擎**：纯本地 ONNX Runtime 毫秒级文字切片识别，新消息 0.2 秒极速捕获，支持连发聚合应答。

- **🛡️ 白名单物理级硬核防火墙**  
  - 严格仅对**白名单内的指定好友**触发回复；
  - 物理级阻断任何微信群聊、服务号、订阅号、陌生人私聊；
  - 配备发送前双重二次校验与重定向熔断机制，杜绝任何串发事故。

- **🎭 千人千面个性化人设（Personas）**  
  支持针对不同好友定制专属语气模型：死党互怼、商务伙伴礼貌回应、老友温馨问候等，无缝贴合主人本人口吻。

- **🌐 主流大模型无缝兼容**  
  原生支持 OpenAI 标准协议，可接入 **DeepSeek (V3/R1)**、**MiniMax**、**Qwen (通义千问)**、**Moonshot (Kimi)**、**智谱 GLM**、**ChatGPT (GPT-4o)** 等主流 API。

---

## 📊 架构机制对比

| 对比维度 | 传统 Hook / 内存注入 | 传统按键精灵 / 坐标点击 | 🤖 本项目 (视觉离屏+消息调度) |
| :--- | :--- | :--- | :--- |
| **安全性** | ❌ 易被特征风控/封号 | ⚠️ 易点击漂移 | ✅ **100% 外部感知无注入，零封号风险** |
| **鼠标干扰** | ✅ 不影响鼠标 | ❌ **严重抢夺光标，无法正常工作** | ✅ **100% 物理鼠标 0 移动，零干扰** |
| **窗口状态** | 必须在前台或特定状态 | ❌ 窗口必须顶层置顶 | ✅ **离屏挂载/后台静默/被覆盖均可秒回** |
| **防串发机制** | 依赖内存协议解析 | ❌ 极易错位串发 | ✅ **OCR + 句柄 + 白名单三重物理熔断** |
| **版本适应性** | 每次微信更新必须重写基址 | 依赖固定分辨率 | ✅ **完美通配微信 3.x / 4.x Qt 架构** |

---

## 🚀 极简上手指南 (Quick Start)

### 源码运行与开发部署 (Run from Source)

#### 1. 克隆仓库
```bash
git clone https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply.git
cd WeChat-AI-AutoReply
```

#### 2. 安装依赖
```bash
pip install -r requirements.txt
```

#### 3. 配置文件准备
复制配置模板文件：
```bash
copy wechat_config.example.json wechat_config_dev.json
```
在 `wechat_config_dev.json` 中配置您的 `api_key`、`api_base` 以及好友白名单 `whitelist`。

#### 4. 运行可视化控制中心
```bash
python wechat_bot_gui_dev.py
```
或直接无界面静默守护运行核心：
```bash
python wechat_ai_bot_dev.py
```

---

## ⚙️ 核心配置说明 (`wechat_config.json`)

```json
{
    "ai_engine": "openai_api",
    "openai_api": {
        "api_key": "sk-your-key-here",
        "api_base": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.6-flash",
        "temperature": 0.7
    },
    "whitelist_mode": true,
    "contacts": [
        {"remark": "好友微信备注名A", "wxid": "wxid_aaaaaaaaaaaa"},
        {"remark": "好友微信备注名B", "wxid": ""}
    ],
    "whitelist": [
        "好友微信备注名A",
        "好友微信备注名B"
    ],
    "check_interval_seconds": 0.5
}
```

### 📇 通讯录映射（备注 + 微信号）

`contacts` 是白名单的进化版。它解决两个纯 OCR 链路的老问题：

| 问题 | `whitelist` 纯字符串 | `contacts` 带 `wxid` |
|---|---|---|
| 两个人重名 | ❌ 可能点错人 | ✅ 自定义微信号可搜，唯一 |
| 改了备注 | ❌ 记忆断了 / 要改配置 | ✅ 会话键用微信号，记忆不断 |
| 中文名字当会话键 | ❌ DSH A2A 建会话失败 | ✅ 微信号是 ASCII，天然稳 |

规则：

- `remark` = 微信里显示的名字（**有备注就填备注**），参与白名单匹配
- `wxid` = 微信号，**可选**。填了就用它做 Agent 会话键
- **搜索键（Ctrl+F）**：只认「自己改过的微信号」。
  微信官方下发的 `wxid_xxx` 这类**搜不到本地联系人**（只会给出「网络查找微信号」网页结果），
  所以 `wxid_` 开头的会自动降级改用 `remark` 搜；自定义微信号搜不到时也会再降级用 `remark` 重搜一次。
- 白名单 = `contacts` 的 `remark` **∪** 旧 `whitelist` 字符串，两者都写也能用
- 只想用 `contacts`，把 `whitelist` 设成 `[]` 即可
- 也支持纯字符串写法：`"contacts": ["张三", "李四"]`

**怎么拿微信号**：微信里点开该好友聊天 → 点顶栏名字 → 个人信息页里「微信号」那一行。

**DSH 侧随手改通讯录**（bot 运行时）：

```bash
# 读
curl http://127.0.0.1:8765/contacts
# 单条新增/覆盖
curl -X POST http://127.0.0.1:8765/contacts -H "Content-Type: application/json" \
  -d '{"remark":"张三","wxid":"zhangsan_88"}'
# 整体替换
curl -X POST http://127.0.0.1:8765/contacts -H "Content-Type: application/json" \
  -d '{"contacts":[{"remark":"张三","wxid":"zhangsan_88"}]}'
# 删除一条
curl -X POST http://127.0.0.1:8765/contacts -H "Content-Type: application/json" \
  -d '{"remove":"张三"}'
```

改完立即生效，不用重启 bot（每次读配置都重新读文件）。

### 🧠 Agent 大脑（可选，推荐）

把「直接调模型」换成「交给一个真正的 Agent 决策」。Agent 不只回话，还能自己判断
**要不要回**、**什么时候回**，返回结构化决策而不是一段文本。

```json
{
    "agent": {
        "enabled": true,
        "url": "http://127.0.0.1:44708/a2a",
        "card_url": "http://127.0.0.1:44708/.well-known/agent-card.json",
        "context_prefix": "wx-",
        "timeout_seconds": 180,
        "max_defer_seconds": 900,
        "fallback_replies": [
            "嘿嘿，我这会儿有点懵，等下再聊~",
            "啊呀，脑子突然卡壳了，晚点回你哈"
        ]
    }
}
```

**按关系分流到不同 A2A 端点**（可选，配 `relation_urls`）：通讯录里给每个人填「关系」，bot 就会把消息发到该关系的专属端点；端点上的 agent preset 决定人格，所以「家人」「损友」可以完全是两种说话风格：

```json
{
    "agent": {
        "relation_urls": {
            "家人": "http://192.168.31.20/a2a/relation-family",
            "朋友": "http://192.168.31.20/a2a/relation-friend",
            "损友": "http://192.168.31.20/a2a/relation-bro"
        }
    }
}
```

- 优先级：`relation_urls[关系]` → `agent.url`（没填关系的人走默认端点）。
- 首次给某个好友分流时日志会打一行 `🔀 [A2A 路由] 好友【老爸】→ 关系「家人」→ http://…`，方便核对。
- **人格卡不在 bot 里**：bot 只负责 OCR/发送/桥接，人设由 DSH 微信端点的 `preset`（如 `card-22` 撩人精）提供，改卡不用重启 bot。

Agent 返回的契约（一行 JSON）：

```json
{"action": "reply|ignore|defer", "segments": ["第一条", "第二条"], "urgency": "now|soon|later", "reason": "一句话理由"}
```

| 字段 | 含义 |
| --- | --- |
| `action` | `reply` 该回 / `ignore` 不用回 / `defer` 现在不合适 |
| `segments` | 要**逐条连发**的消息数组（1–4 条，每条 5–30 字，像真人分条发）。只有一条时也可写 `"text": "..."`；老契约的 `text` 继续兼容，`text` 里用 `\|\|\|` 分隔也会被拆开 |
| `urgency` | `now` 立即 / `soon` 30–90 秒 / `later` 3–15 分钟（带 ±20% 抖动） |
| `reason` | 一句话理由，只用于日志 |

| action | 机器人行为 |
| --- | --- |
| `reply` | 按 `urgency` 延迟后逐条发送 `segments`（段间按字数停顿 0.5–1.5 秒，像真人在打字） |
| `ignore` | 不发，但记下签名防止重复处理 |
| `defer` | 整组消息进延时队列，超过 `max_defer_seconds` 放弃 |

- **为什么分条**：DSH 单次回复有长度限制，长段落挤成一条既难看又容易被截断。让模型输出 `segments` 数组，bot 逐条发出去，阅读节奏跟真人连发一致。
- 段间停顿可用 `segment_delay_min_seconds` / `segment_delay_span_seconds` 调（默认 0.45 / 0.9，另加 0–0.35 秒随机抖动）。
- **Agent 不可用时**：记 ERROR 日志 + 发一条 `fallback_replies` 里的固定可爱回复，不会静默，也不会退回旧的直连模型。
- **并发**：每个联系人一个 `contextId`，多线程并行决策，互不阻塞。
- `contextId` 只允许 ASCII，中文昵称会自动哈希成 `wx-u<sha1前12位>`，映射表落在 `wechat_agent_contexts.json`。

### 📤 主动发送（可选）

让 Agent / 外部脚本主动给指定好友发消息，不需要等对方先开口。

```json
{
    "proactive_send": {
        "enabled": false,
        "host": "127.0.0.1",
        "port": 8765,
        "whitelist_only": true,
        "max_per_hour": 3,
        "max_per_day": 20,
        "require_confirm": false,
        "allow_search": true,
        "confirm_expire_hours": 12
    }
}
```

HTTP 接口（只监听 `127.0.0.1`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 运行状态、本小时/今日已发条数、待确认数 |
| GET | `/pending` | 待人工确认的消息列表 |
| POST | `/send` | `{"target":"张三","text":"在忙吗？"}` 发一条 |
| POST | `/confirm` | `{"id":"p1730..."}` 人工确认后发送 |
| POST | `/reject` | `{"id":"p1730...","reason":"算了"}` 丢弃 |

请求体支持三种写法，随便挑一种：

```bash
# 1) 标准 JSON
curl -X POST http://127.0.0.1:8765/send -H "Content-Type: application/json" \
     -d '{"target":"张三","text":"在忙吗？"}'

# 2) 表单（注意是 = 不是 :，中文无需转义）
curl -X POST http://127.0.0.1:8765/send -d "target=张三&text=在忙吗？"

# 3) 直接挂在 query 上（Windows 命令行最不容易出错的一种）
curl -X POST "http://127.0.0.1:8765/send?target=张三&text=在忙吗？"
```

> **Windows 命令行两个坑，服务端都已兜住：**
>
> 1. **引号被吃掉**：PowerShell 会把 `-d '{"target":"张三"}'` 里的双引号吃掉，
>    服务端只收到 `{target:张三}`，于是返回 `{"ok":false,"code":"bad_json"}`。
> 2. **终端编码是 GBK**：`curl -d "target=微信 2"` 发出去的是 GBK 字节，
>    按 UTF-8 解会变成 `΢�� 2`，白名单匹配不上 → `{"ok":false,"code":"not_whitelisted"}`。
>    服务端会自动在 UTF-8 / GB18030 之间择优解码。
>
> 所以三种写法里，**写法 3（挂在 query 上）最稳**；也可以用 PowerShell 原生 cmdlet：
>
> ```powershell
> Invoke-RestMethod -Uri http://127.0.0.1:8765/send -Method Post `
>   -ContentType "application/json" `
>   -Body (@{target="张三"; text="在忙吗？"} | ConvertTo-Json)
> ```

**安全护栏**（写在代码里，不靠配置自觉）：

1. 群聊、黑名单、系统号（文件传输助手 / 服务通知…）**永远拒绝**，即使关掉 `whitelist_only`。
2. 白名单严格匹配，`张三` 不会误命中 `张三丰`。
3. 频率上限 `max_per_hour` / `max_per_day`，超了直接 403。
4. 找人走三级策略（顶栏已是目标 → 侧栏可见行 → Ctrl+F 搜索），**每一步都要复核顶栏标题**，复核不过就放弃，宁可发不出去也绝不发错人。
5. 发送失败不记账（不占配额）。
6. `require_confirm: true` 时，消息落到 `pending_confirm.jsonl`，GUI 每 2 秒轮询并弹窗，你点「发送」才真发。人工确认**只**绕过确认这一道，白名单和频率照旧。

---

## 📁 仓库结构

```text
WeChat-AI-AutoReply/
├── wechat_ai_bot_dev.py          # 后台常驻感知与调度核心 (OCR+Win32消息路由+离屏抓图)
├── wechat_bot_gui_dev.py         # 现代化控制中心 GUI (Tkinter + 实时日志 + 待确认弹窗)
├── agent_client.py               # Agent 大脑客户端 (A2A 协议 + JSON 契约 + 降级兜底)
├── kill_wechat_bot_dev.py        # 安全进程清理终结脚本 (精准定向关闭守护)
├── Linux移植规划.md              # Linux 平台移植调研与实施方案（已调研，未开工）
├── 下一步详细方案.md              # P1 群聊 / P2 群聊回复 / P3 人格关系 / P5 收尾 的详细设计
├── wechat_config.example.json    # 工业级通用脱敏配置文件模板
├── .env.example                  # 环境变量配置模板
├── requirements.txt              # Python 依赖清单
├── .gitignore                    # 工业级排除规则 (严防私密配置与日志外泄)
├── LICENSE                       # MIT 开源许可证
└── README.md                     # 项目说明文档
```

### 🧪 回归测试

```bash
python 回归测试_防串发匹配.py    # 防串发 / 名字匹配 / 气泡解析
python 回归测试_通讯录映射.py    # contacts 解析、备注↔微信号、白名单并集、/contacts 接口
python 回归测试_Agent大脑.py     # Agent 客户端、契约解析、降级、并发总线
python 回归测试_主动发送.py      # 主动发送护栏、HTTP 接口、找人三级策略
python 回归测试_待确认队列.py    # 待确认队列落盘/恢复/确认/拒绝/过期
python 回归测试_待确认GUI.py     # GUI 侧队列读写与接口对齐
python 回归测试_GUI配置面板.py   # GUI 配置面板控件与保存逻辑
```

联调（需要 DSH 的 A2A 服务在跑）：

```bash
python 联调脚本_真实Agent.py     # 提交 -> 后台决策 -> 取回 -> 发送/不发 全链路
```

---

## 🗺️ 下一步规划 (Roadmap)

按 P1 → P5 推进。**硬约束**：每项完成后六套回归必须全绿 + Windows 真机冒烟一轮。

| 优先级 | 事项 | 一句话目标 | 状态 | 预估 |
|---|---|---|---|---|
| P1 | 群聊主动发送 | `POST /send` 能把消息发进指定群 | 待做 | 0.5 天 |
| P2 | 群聊自动回复 | 群里 @我 或命中关键词时自动回复，且不刷屏 | 待做 | 1.5 天 |
| P3 | 人格 / 关系字段 | 按「和这个人的关系」改变说话语气 | 待做 | 0.5 天 |
| P4 | Linux 适配 | 在 Linux 微信上跑通同一套能力（键盘流） | 已调研，未开工 | 1.5 ~ 2 天 |
| P5 | 工程收尾 | 清探针、一键回归、日志分离 | 待做 | 0.5 天 |

详细方案：

- P1 / P2 / P3 / P5 → **[下一步详细方案.md](下一步详细方案.md)**
- P4 Linux → **[Linux移植规划.md](Linux移植规划.md)**

---

## 🐧 跨平台说明

Windows 与 Linux/X11 都已支持，平台差异全部收敛在 `wechat_linux_io.py`（Windows 分支逐字未改）：

| 能力 | Windows | Linux / X11 |
|---|---|---|
| 抓图 | `PrintWindow`（窗口被挡也能抓） | `XGetImage` 抓 root 再裁剪；离屏时走 XComposite pixmap |
| 点击 | `PostMessage` 后台点击 | XTest（会移动真实光标；离屏模式下不用鼠标） |
| 键盘 | `SendInput` | XTest + 剪贴板粘贴（fcitx 会吞逐键字符） |
| 隐藏微信 | 窗口移到 `(-3000, -3000)` | XComposite 离屏重定向 |

**👻 隐藏微信至离屏（Linux）**：GUI 顶栏按钮。开启后窗口被 Composite 重定向
（画面改画到服务器端 pixmap）并移到屏幕外，桌面看不见、也不抢你的键盘焦点；
bot 依旧能抓图 / OCR / 发消息（已实测离屏静默发送成功）。需要切换会话时会临时召回窗口，
切完自动藏回去。依赖 X 服务器的 Composite 扩展（xrdp 的 Xorg、Xvfb 都自带），
不支持时会明确报错而不是静默失败。窗口几何记在 `wechat_config_dev.json` 的 `saved_window_rect`。

> 离屏时 Chromium 会降帧渲染，气泡出现可能晚 1~2 秒：发送侧改为多轮轮询，
> 一旦 Enter 已发出就**绝不重发**（避免给同一个人发两遍）。

Linux 依赖安装（无 pip 环境的手动装法）、`DISPLAY` / `XAUTHORITY` 设置、七套回归测试的跑法，
见 **[Linux移植规划.md](Linux移植规划.md)** 与本文档「🧪 回归测试」一节。

---

## ⚠️ 免责声明 (Disclaimer)

1. 本项目仅供技术交流与计算机视觉/自动化技术学习研究使用；
2. 本项目不涉及任何反编译、DLL 注入或针对微信协议的破解篡改，全流程遵循 Windows 标准窗口消息与辅助感知交互规范；
3. 请合理配置并使用自动化助手，使用者须自行承担使用过程中的相关法律与合规责任。

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 许可协议开源。