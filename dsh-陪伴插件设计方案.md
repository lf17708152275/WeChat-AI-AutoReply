# 方案 B 设计：dsh-wechat-companion

> 目标：人格、多媒体能力都搬到 DSH 插件侧；微信项目退化成**纯桥接**（OCR / 发送 / 转发）。
> 状态：设计稿，待用户确认后实施。

## 1. 总体架构

```
微信 ──截图/OCR──▶ bot（桥）──HTTP/A2A──▶ 入站端点 /a2a/family（preset=persona-family）
                    ▲                                    │
                    │                                    ▼
              发送文本/图片 ◀── reply text + outbox ◀── DSH agent（人格 preset 挂载）
```

| 层 | 负责 | 不负责 |
|---|---|---|
| bot（WeChat-AI-AutoReply） | 截图、OCR、点窗口、剪贴板粘贴、消费 outbox | 人格、提示词、模型选择、记忆 |
| @hanphone/dsh-a2a | 入站 HTTP 端点、任务执行、per-inbound preset/workspace/model | 人格内容 |
| **dsh-wechat-companion（新）** | preset 定义（人格）、媒体工具、outbox 协议 | 微信窗口操作 |
| DSH core | agent 运行时、模型调用、会话持久化 | — |

## 2. 包结构（一个仓库，三个包）

```
dsh-companion/                      pnpm workspace
├── packages/core/                  @wechat-companion/core —— 与 DSH 无关的纯逻辑
│   ├── outbox.ts                   appendAction()/readActions()/rotate()
│   ├── media.ts                    媒体库索引、贴纸解析、语音合成调用
│   └── types.ts                    Action 协议 v1
├── packages/dsh-companion/         @wechat-companion/dsh —— DSH bundle（本文重点）
│   ├── cordis.patch.yml            注入工具行
│   ├── presets/<persona>/preset.yml 人格 preset（每关系一个）
│   ├── src/index.ts                defineTool × N
│   └── package.json                dsh.bundle.patch
└── packages/cli/                   （可选）bridge-sync：把 bot 配置与 preset 对齐
```

**为什么这样拆**：`core` 无 `@deepseek-ai/*` 依赖，可以直接 vitest 跑、也能被将来的
`dsh-companion-voice` / `dsh-companion-vision` 复用。工具本体留在 `dsh-companion` 里，
避免一开始就做版本矩阵；等某个能力（语音/视觉）真的长大，再拆成独立包，只依赖 `core`。

## 3. 人格层：preset 就是 YAML

DSH 的 preset 是一个组合声明，人格只是其中一行 `@deepseek-ai/dsh-persona`。
聊天 preset **刻意不挂 fs/bash/web 工具**：不产生工具噪音、不输出 JSON 契约、更快。

```yaml
# packages/dsh-companion/presets/family/preset.yml
- insert:
    - id: preset-persona-family
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: persona-family
        name: 家人·体贴
        description: 微信家人对话：短句、口语、关心但不啰嗦
        order: 20
        plugins:
          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              prefix: |
                你在微信上和家人聊天。对方是你的长辈/亲人。
                说话规则：一句话能说完就别说两句；口语、短句；不解释、不总结、不用书面语；
                可以关心、可以开玩笑，但不要长篇大论；不说"作为AI"之类的话。
                收到对方消息时，直接像人一样回一句。
          - id: time-context
            name: '@deepseek-ai/dsh-time-context'
          - id: companion-media        # ← 我们自己的媒体工具，只挂在这个 preset 里
            name: '@wechat-companion/dsh'
            config:
              outbox: '~/.dsh/companion/outbox'
```

要点：
- `plugins` 里的行按 `name` 解析已安装包，**第三方包也能出现在 preset 内**（DSH 用同一个
  loader 组合 preset 子树，与 profile 顶层走同一套解析）。
- 工具**只在这个 preset 可见**，不会污染编码会话（对比：nekowhale-stickers 是全局挂载）。
- 5 个关系 → 5 个 preset；入站端点的 `preset` 字段绑定即可（GUI 已有下拉）。

### 人格素材来源：`~/桌面/project/26张人格卡/`

实测 23 张卡（`01 奶糖妹妹` … `26 反骨仔`，缺 04/05/09），每张目录含：

| 文件 | 用途 |
|---|---|
| `人格卡.md`（118–137 行） | **人设正文**，本身就是写给模型看的中文指令（含「〇、人味校准」「一、核心人格」「二、性格叠加权重」「三、表达配比」「四、语言指纹」「五、反应规则」「六、尾巴动作库」「七、颜文字池」「八、专属小设定」「九、边界」「十、语气示例」「十一、快速切换」） |
| `人格卡速览.png` | 卡片视觉，不入 prompt |
| `聊天记录_QQ实测.png` / `留档_人格卡+聊天.png` | 效果参考，不入 prompt |

生成规则（`scripts/gen-presets.mjs`）：
1. 读 `人格卡.md` 全文 → 作为 `persona.prefix`（Markdown 直接可用，约 4KB/卡）。
2. 文本替换：`QQ` → `微信`；去掉首行 `# 人格卡 NN｜X` 标题与 `> 一句话人设` 元信息行（保留正文）。
3. 第十一节「快速切换」里的 `角色名` 抽成 preset 的 `name`，`一句话系统提示` 存为 `description`。
4. 生成到 `packages/dsh-companion/presets/<NN-slug>/preset.yml`，可人工精修，生成器不覆盖已改动文件。
5. 卡目录路径可配（`cardsDir`），换素材集不用改代码。

## 4. 多模态协议：outbox（本项目最关键的设计）

**问题**：A2A 的任务结果只回「assistant 文本」，工具产生的图片/语音拿不到。
**方案**：工具不返回内容，而是往 outbox 追加一行 JSON；bot 在收到回复后消费。

```
~/.dsh/companion/outbox/pending.jsonl
```

```json
{"v":1,"id":"01J...","ts":1762000000,"kind":"text","payload":{"text":"等我两分钟"}}
{"v":1,"id":"01J...","ts":1762000000,"kind":"sticker","payload":{"name":"坏笑"}}
{"v":1,"id":"01J...","ts":1762000000,"kind":"image","payload":{"path":"/tmp/x.png","caption":""}}
{"v":1,"id":"01J...","ts":1762000000,"kind":"voice","payload":{"path":"/tmp/x.mp3","text":"哈哈"}}
{"v":1,"id":"01J...","ts":1762000000,"kind":"file","payload":{"path":"/tmp/x.pdf","name":"报告.pdf"}}
```

约定：
- **顺序**：bot 先发 A2A 回复文本，再按 outbox 顺序执行其余动作。
- **归属**：单用户单并发，按时间窗消费（任务开始 → 任务结束之间写入的行）。多并发时可用
  `X-Companion-Request` 头 + 目录分桶，v1 不做。
- **清理**：成功发送后整文件轮转为 `sent-<date>.jsonl`；未知 `kind` 丢弃并记日志。
- **降级**：bot 不认识的 kind 或发送失败 → 只记日志，不影响文本回复。

工具表：

| 工具 | 参数 | 行为 |
|---|---|---|
| `companion_send_text` | `text` | 追加 text（多气泡） |
| `companion_send_sticker` | `name` / `emotion` | 从贴纸库解析图片，追加 sticker |
| `companion_send_image` | `path` 或 `prompt`（生图，二期） | 追加 image |
| `companion_send_voice` | `text`、`emotion`（TTS，三期） | 合成音频，追加 voice |
| `companion_send_file` | `path` | 追加 file |

模型视角：它只是"说一句 + 可能丢个表情包"，不需要知道微信怎么发。

## 5. 桥接侧（WeChat-AI-AutoReply）改动

1. **联系人 → 端点映射**：每个联系人一个入站端点 `contact-<slug>`，`preset` = 该联系人
   分配的人格卡，`workspace` = 该联系人专属目录（`~/.dsh/companion/contacts/<slug>`，
   顺带把会话/记忆天然隔离）。映射表由 `scripts/provision.mjs` 自动创建/更新，产物写进
   bot 配置 `contact_endpoint.json`。
2. **纯文本直发**：回复不是 JSON 就整段发（JSON 契约保留兼容）。
3. **outbox 消费**：每次回复后读 `pending.jsonl` → 按序发送 → 归档。
4. **媒体发送**（本层唯一有真实风险的部分）：
   - 文本：现有 clipboard + Ctrl+V ✅
   - 图片/贴纸：用 python3-gi 的 `Gtk.Clipboard.set_image + store()` 写剪贴板 → Ctrl+V
     （微信 Linux 是否吃粘贴图片**待实测**）
   - 文件/语音：剪贴板 `text/uri-list` → Ctrl+V（是否作为文件发送**待实测**）
   - 失败一律降级为文本占位（如「[图片]」）

## 6. 分阶段

| 阶段 | 内容 | 工作量 | 出口标准 |
|---|---|---|---|
| **P0 探针** | preset 挂载 + 工具可见性 + outbox 端到端 + 纯文本人格回复 | 0.5 天 | 微信发一句，AI 用人格口吻回一句（无 JSON） |
| **P1 表情包** | `companion_send_sticker` + bot 贴图发送 | 1 天 | 回复里带一张表情包上屏 |
| **P2 图片** | `companion_send_image`（本地图/生图） | 1–2 天 | 图片上屏 |
| **P3 语音** | TTS → outbox → 发送（文件形式） | 1 天 | 音频上屏（**语音气泡几乎不可行**，见风险） |
| **P4 有思想** | per-contact 记忆：`companion_remember/recall` + workspace 目录 | 1–2 天 | 同一联系人跨天记得上下文 |

## 7. 风险（按严重度）

1. **微信 Linux 的媒体粘贴**：图片/文件能否通过剪贴板粘贴发送，未验证。P0 同批做 30 分钟探针；
   若不行 → 降级为「只发文本 + 表情包用文字描述」，或改用窗口内"发送文件"按钮的坐标自动化。
2. **语音气泡**：微信语音是录音生成，无法由文件注入；只能发音频文件（对方看到的是文件）。
   若必须语音气泡 → 需要虚拟麦克风（pulseaudio null sink + parec 注入）再按住说话，成本高、稳定性差。
3. **会话共享**：一个入站端点一个 session，同关系多联系人会串上下文 → 用 P4 记忆工具或按人建端点。
4. **preset 内引用第三方包**：理论上可解析，P0 必须实测；不行就把工具全局挂载 + preset 内用提示词约束。
5. **A2A 任务超时**：媒体生成（TTS/生图）会拉长任务时间，需调 `taskTimeoutMs`。

## 8. 待拍板

1. 端点粒度：按**关系**（5 个端点）还是按**联系人**（几十个端点）？
2. 语音接受度：文件形式 OK，还是必须语音气泡？
3. 工具作用域：只挂聊天 preset（推荐），还是全局可用？
4. 表情包来源：复用已装 `dsh-nekowhale-stickers` 的素材库，还是独立贴纸库？
