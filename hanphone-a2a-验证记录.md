# @hanphone/dsh-a2a 验证记录（0.5.2）

> 目的：验证"每个关系一个人格端点"是否可行 —— 一个 A2A 端点绑一个 DSH agent preset，
> bot 按联系人关系选 URL，人格彻底搬到 DSH 侧。

## 一、装在哪（隔离，不动主 profile）

| 项 | 值 |
|---|---|
| profile | `a2a-test`（全新，从 web 模板初始化） |
| 目录 | `/home/long/.dsh/profiles/a2a-test` |
| 安装命令 | `dsh plugin --profile a2a-test add @hanphone/dsh-a2a` |
| 实例 | `A2A_PORT=8900 dsh --profile a2a-test --host 127.0.0.1 --port 3100 --no-open`（PID 6301） |
| 面板地址 | `http://127.0.0.1:3100/?token=-SGDlBSx3EYiodG3KFjzlefweKy80I4oDM52UKCXrm8` |
| 主 profile | **未改动**，`web/package.json` 里没有 hanphone，bot 和当前对话都不受影响 |

停止：`pkill -f "profile a2a-test"`

## 二、兼容性（本机 DSH 0.2.1-rc.5 / Node 24.22.2）

插件声明的 peer 与宿主实际版本：

| peer | 要求 | 宿主实际 | 结果 |
|---|---|---|---|
| @deepseek-ai/cordis | `~4.0.2 \|\| 4.0.5-alpha.1 \|\| 4.0.6-alpha.1` | 4.0.5 | ✅ |
| @deepseek-ai/schemastery | `3.*` | 3.18.5 | ✅ |
| @deepseek-ai/dsh-commands | `>=0.1.3 <0.3` | 0.2.1-rc.5 | ✅ |
| @deepseek-ai/dsh-storage-domain | `>=0.1.3 <0.3` | 0.2.1-rc.5 | ✅ |

`pnpm peers check` 会报 6 个 missing peer，**是正常现象** —— dsh 用运行时模块解析器提供这些包，不要手动装。

## 三、实测结果

建了两个入站端点（走的是面板同一套 API，POST `/a2a/api`，action=`inbound.create`）：

| 名称 | 端点 | preset |
|---|---|---|
| 家人·体贴 | `/a2a/family` | `standard` |
| 日常本人大号 | `/a2a/daily` | `minimal` |

同一句话「晚上吃的啥？」打到两个端点，各自真实回复（A2A v1.0.1 JSON-RPC SendMessage）：

- `/a2a/family` → 1.99s，`TASK_STATE_COMPLETED`
- `/a2a/daily` → 1.46s，`TASK_STATE_COMPLETED`
- 会话按 `a2a-<serverId>-<contextId>` 落盘：`/home/long/.dsh/storages/session_projcache/sessions/a2a-server-a5a058e1-ctx-fam-1.json`

**结论：端点级的 preset 隔离是真的生效**，一个 URL 一个 preset 一套会话。

## 四、三个必须先知道的坑

1. **面板 API 只允许本机访问。** `lib/index.js:1213` 的 `isLoopback(req)` 判定 `remoteAddress` 必须是 127.0.0.1/::1，否则直接 403 `forbidden`。
   → 面板必须在**本机浏览器**打开 `http://127.0.0.1:3100/...`；从 192.168.31.20 打开会 403。
   主实例现在是 `--host 127.0.0.1` 挂在 80 反代后面，浏览器来源会被判成非 loopback —— **这点得先想清楚**（要么本机开，要么改插件）。

2. **响应形状跟现在不一样。** 现在是 `result.task.status.state` + `result.task.artifacts[].parts[]`，
   而且文本被**切成逐字 chunk**（`{"text":"我"},{"text":"这边"}...`），
   我们 bot 的解析层得改成：拼 `parts[].text` → 判断 `TASK_STATE_COMPLETED`。
   对比：@ryubyte 是同步文本返回，我们现在的 `_parse_decision` 直接吃 text。

3. **preset 得先有。** `/a2a/api/presets` 现在只能看到宿主自带的 4 个：`standard` / `ptc` / `minimal` / `cordis`。
   要「家人·体贴」这种人格，得先有自定义 preset —— 正好是 dsh-tavern 生成的 `.agent-presets/<dir>/`。

## 五、下一步（如果继续）

1. 在 a2a-test 里装 dsh-tavern，建 2~3 个角色卡 → 生成 2~3 个 preset。
2. 每个 preset 建一个入站端点，验证"同一个人格端点回复稳定带上人设"。
3. 通了之后，bot 侧改动：`agent.url` 从单值改成 `relation_urls` 映射表 + 响应解析改成 task/parts 版。
4. 再决定要不要把主 profile 切过来（切换需要重启 dsh web，会打断当前对话）。

## 六、附：面板 API 速查（本机 curl）

```sh
# 拿 cookie（token 每次启动都变，看启动日志）
curl -c cj.txt "http://127.0.0.1:3100/?token=<TOKEN>"

# 总览
curl -b cj.txt http://127.0.0.1:3100/a2a/api
# 可用 preset
curl -b cj.txt http://127.0.0.1:3100/a2a/api/presets
# 建入站端点
curl -b cj.txt -H 'content-type: application/json' -X POST http://127.0.0.1:3100/a2a/api \
  -d '{"action":"inbound.create","name":"家人·体贴","preset":"standard","endpointPath":"/a2a/family"}'
# 调用
curl -b cj.txt -H 'content-type: application/json' -X POST http://127.0.0.1:3100/a2a/family \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"role":"user","messageId":"m1","parts":[{"kind":"text","text":"晚上吃的啥？"}],"contextId":"ctx-1"}}}'
```

---

# 迁移到主 profile（2026-11-02 17:2x）

## 已完成
| 步骤 | 结果 |
|---|---|
| 备份主 profile | `/tmp/webprofile_backup/`（package.json / cordis.yml / cordis.patch.yml / pnpm-lock.yaml） |
| 卸载旧插件 | `dsh plugin --profile web remove @ryubyte/dsh-a2a` → 0.3.1 已移除 |
| 安装新插件 | `dsh plugin --profile web add @hanphone/dsh-a2a` → 0.5.2，已进 `dsh.profile.bundles` |
| 插件图校验 | `dsh --profile web --dump-config` 只出现 `id: a2a / name: '@hanphone/dsh-a2a'`，无 ryubyte 残留 |
| 清理测试 profile | `/home/long/.dsh/profiles/a2a-test` 已删（含 node_modules/profiles 元数据） |
| 清理测试实例 | 进程 6301 已停，3100 端口已释放 |
| 清理测试会话 | `a2a-server-5814671e-ctx-day-1.json`、`a2a-server-a5a058e1-ctx-fam-1.json` 已删 |
| 保留 | 旧插件的历史 agent 会话（`~/.dsh/sessions/--home-long-.dsh-profiles-web-.a2a-sessions--`，940K）——那是老爸/老朋友的历史记忆，没删 |

## bot 侧已适配（重启 dsh 后即可用）
1. `agent_client._extract_text()` 支持新插件的**逐字 chunk**：新增 `_join_parts()` 把同一 artifact 的所有 `parts[].text` 拼起来（原来只取最后一个 part，会拿到单个字）。
2. 兼容老格式：`result` 直接是字符串 / `result.text` 也能兜住。
3. 新增「关系 → 端点」路由：`agent.relation_urls`（家人→`/a2a/family`，其余→`/a2a/default`），`AgentClient.url_for(target)` 用 `persona_cards.relation_of()` 判关系。
4. 回归测试_Agent大脑.py 补 6 项断言（逐字拼句 / 老格式 / 失败任务抛错 / 关系端点路由 ×3），全绿。

## 还差一步：重启 dsh（会打断当前对话）
```bash
# 停
kill 1041
# 起（原样的命令，脱离终端）
(setsid node /usr/local/bin/dsh web --host 127.0.0.1 --port 25675 \
  --trusted-host 192.168.31.20 --public-url http://192.168.31.20 --no-open \
  >/tmp/dsh_web.log 2>&1 < /dev/null &)
```

## 重启后要做的
1. 面板建两个入站端点（本机或经 nginx 都行，nginx 反代来源是 127.0.0.1 能过 loopback 判定）：
```bash
curl -b cj.txt -H 'content-type: application/json' -X POST http://192.168.31.20/a2a/api \
  -d '{"action":"inbound.create","name":"日常本人大号","preset":"standard","endpointPath":"/a2a/default"}'
curl -b cj.txt -H 'content-type: application/json' -X POST http://192.168.31.20/a2a/api \
  -d '{"action":"inbound.create","name":"家人·体贴","preset":"standard","endpointPath":"/a2a/family"}'
```
2. 起 bot：`./start_linux.sh`
3. 验证：日志里出现 `🔌 [Agent 大脑已接入]`；发一条消息看是否 `✅ [Linux 静默秒回成功]`。

## 注意
- **nginx 的 location 正则**是 `^/(a2a/|...)`（带斜杠），新端点路径 `/a2a/default`、`/a2a/family` 都匹配，能正常反代。
- 旧端点 `/a2a`（无斜杠）在插件换掉后**不再存在**，config 里已改成 `/a2a/default`，别用旧地址。
- 现在两个端点都是 `preset: standard`（宿主自带）；要真人格，得先有自定义 preset（tavern 生成的 `.agent-presets/`），再 `inbound.update` 换 preset。

## 重启后实测（2026-11-02 17:40，已完成）

| 项 | 结果 |
|---|---|
| 插件版本 | `/a2a/api` → `{"version":"0.5.2"}`，旧端点 `/a2a` 已 404 |
| 入站端点 | `server-a5a058e1` 家人·体贴 → `/a2a/family`（preset=standard）<br>`server-5814671e` 日常本人大号 → `/a2a/default`（原 `/a2a/daily`，已 `inbound.update` 改名） |
| 端点直连 | 两个端点均 `TASK_STATE_COMPLETED`，9.6s / 7.9s |
| bot 启动 | PID 见 `pgrep -f wechat_ai_bot_dev.py`，日志 `🔌 [Agent 大脑已接入] http://192.168.31.20/a2a/default` |
| **真实来信闭环** | 老爸发「还在不」→ 🎴《家人·体贴》（来源：**关系:家人**，走 `/a2a/family`）→ 决策 1.3s「在呢 咋了爸」→ ✅ 秒回上屏 |

## 这次踩到并修掉的三个坑

### 1. 顶栏标题被切掉 → 收到消息也不回（最隐蔽）
- 现象：换了插件、bot 正常起、微信里明明有未读，却完全没日志。
- 原因：`compute_wechat_layout()` 里 `header_h = max(85, int(0.110 * H))`。窗口被拉高到 756 后，微信标题行 cy≈90，而 header_h 仍只给 85 → `extract_active_chat_title()` 的 `cy <= header_h` 把标题过滤没了 → 白名单匹配失败 → 判定"不是目标会话"。
- 修法：`header_h = max(88, int(0.140 * H))`（H=756→105，H=635→88，原行为不变）。

### 2. Agent 回复里混进 markdown 表情包
- 现象：profile 装了 dsh-nekowhale-stickers，Agent 会在自然语言后追加 `![扒饭](http://127.0.0.1:25675/x-nekowhale-stickers/s/xxx.gif)`，发到微信就是一行乱码。
- 修法：`wechat_ai_bot_dev.sanitize_outgoing_text()`，在 `poll_agent_results` 里统一清洗：去 `![alt](url)`、`[文字](url)`→文字、整行裸 URL、代码块/反引号、压缩空行。

### 3. 逐字 chunk 只取到最后一个字
- 新插件把回复切成 `{"text":"我"},{"text":"这边"}…`，旧 `_extract_text()` `reversed(parts)` 取最后一个非空 part → 只拿到单个字。
- 修法：`agent_client._join_parts()` 把同一 artifact 的全部 parts 拼起来。

## 当前回归状态（2026-11-02 17:45）
8 套全绿：防串发匹配、待确认队列 44、主动发送 64、Agent大脑（含新增 6 项）、人格卡 59、通讯录映射 77、GUI配置面板 59、待确认GUI 23。

> GUI 两套需要 tkinter 环境：`source /tmp/linux_env.sh`（TKROOT=/home/long/.local/tkroot）。
