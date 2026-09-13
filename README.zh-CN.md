# AgentBridge

AgentBridge 是一个本地 Codex 插件：它把用户配置的命令行 agent 作为插件托管的
“外部子智能体”启动，并把有大小上限的 stdout、stderr、退出状态、父子关系和生命周期
元数据返回给 Codex。

内置别名使用当前官方文档中的 Claude Code 命令 `claude -p` 和 Google Antigravity
CLI 命令 `agy -p`。Antigravity 默认保持禁用，避免未安装 `agy` 的机器无法使用回退
配置；安装并认证后可直接启用。其他兼容 agent CLI 也可以通过新增 JSON 别名接入。

Antigravity 的 argv 把提示词保存为单独一项 `-p={prompt}`，并把
`--output-format json` 放在它之前。这是 `agy` 1.2.0 的实际参数要求，可避免中间的
CLI flag 被误当成提示词。

## 主要能力

- 用 argv 数组直接启动进程，不隐式调用 shell；
- agent 别名和启动指令完全由一个 JSON 参数文件控制；
- 将统一的模型名和思考等级映射为每个 CLI 专属的 argv；
- 支持参数、stdin、临时提示词文件三种传递模式；
- 支持异步任务、进度感知的并发等待、分页日志和持久化元数据；
- 支持父任务/根任务关系、provider 会话续接和 follow-up；
- 支持并发限制、超时、POSIX 进程组取消和服务重启恢复；
- 支持工作目录白名单、别名级环境变量和额外参数策略；
- 仅使用 Python 标准库，无需安装第三方包。

AgentBridge 不是模型服务，也不会替其他 CLI 建立沙箱。外部 agent 使用该命令在本机
已有的权限和认证状态运行。“外部子智能体”是 AgentBridge 的编排抽象，不是 Codex
原生子智能体线程：它不会进入原生子智能体 UI，也不会继承 Codex 原生模型、沙箱或
线程设置。

## 环境要求

- Linux，或 Windows 11 + WSL 2 中的 Codex；
- Bash 4.4+；
- Python 3.10+；
- 每个外部 agent CLI 需单独安装并完成认证。

## 安装

从 GitHub 安装：

```bash
codex plugin marketplace add Syoong-s/AgentBridge
codex plugin add agent-bridge@agent-bridge
```

安装后新建一个 Codex 任务，使技能和 MCP server 被重新加载。

从源码目录测试时，在仓库根目录运行：

```bash
codex plugin marketplace add "$(pwd)"
codex plugin add agent-bridge@agent-bridge
```

## 配置 agent

AgentBridge 按以下优先级选择第一个存在的配置文件：

1. `AGENT_BRIDGE_CONFIG` 指定的文件；
2. `$PLUGIN_DATA/config.json`（Codex 管理的插件可写数据目录）；
3. `~/.config/agent-bridge/config.json`；
4. 插件内置的 `config/agents.example.json`。

可在源码仓库中复制一份用户配置：

```bash
mkdir -p ~/.config/agent-bridge
cp plugins/agent-bridge/config/agents.example.json \
  ~/.config/agent-bridge/config.json
```

参数文件使用版本化 JSON。完整范例见
[`plugins/agent-bridge/config/agents.example.json`](plugins/agent-bridge/config/agents.example.json)。
内置命令依据 [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)
和 [Antigravity headless mode](https://antigravity.google/docs/cli/headless/)；实际运行前仍应
用本机 `claude --help` 或 `agy --help` 核对已安装版本。

全局参数：

| 字段 | 含义 |
| --- | --- |
| `max_concurrent_tasks` | 同时处于活动状态的任务上限，范围 1–32 |
| `max_retained_tasks` | 保留的终态任务目录数量 |
| `default_timeout_sec` | 别名未指定时的默认超时 |
| `max_timeout_sec` | 配置值和运行时超时覆盖值的全局上限 |
| `default_max_output_bytes` | stdout、stderr 各自的默认存储上限 |
| `allowed_work_roots` | 规范化绝对路径白名单；空数组表示允许任意现有目录 |

每个 agent 别名支持：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 禁用的别名可以查看，但不能启动 |
| `description` | `list_agents` 显示的人类可读说明 |
| `command` | 非空 argv 字符串数组，不进行 shell 解析 |
| `prompt_mode` | `argument`、`stdin` 或 `file` |
| `inherit_env` | 是否继承 MCP server 环境，再叠加 `environment` |
| `environment` | 支持占位符的静态环境变量映射；任务 `PWD` 由桥接层管理 |
| `timeout_sec` | 该别名的默认超时 |
| `max_output_bytes` | 每个输出流的存储上限，1 KiB–100 MiB |
| `allow_extra_args` | 是否允许在固定命令后附加独立的运行时 argv 项 |
| `model` | 可选的统一模型选择器及其 provider argv 映射 |
| `reasoning_effort` | 可选的统一思考/effort 选择器及其 provider argv 映射 |
| `session` | 可选的 provider 会话创建、提取和续接协议 |

支持 `{prompt}`、`{prompt_file}`、`{cwd}`、`{task_id}` 四种占位符。
`argument` 模式必须恰好包含一个 `{prompt}`；`file` 模式必须恰好包含一个
`{prompt_file}`，并在任务结束后删除私有临时文件；`stdin` 模式不能包含这两个提示词
占位符。`command[0]` 不允许包含提示词占位符。

为使环境变量与真实的 `Popen(cwd=...)` 工作目录一致，AgentBridge 会在继承并合并别名
环境变量后，把子进程的 `PWD` 强制设置为解析后的任务 cwd。因此，配置中的
`environment.PWD` 会被有意覆盖。

### 模型名与思考等级

`model` 和 `reasoning_effort` 都接受 `arguments` 数组、可选 `default` 和可选
`allowed_values`。两个参数数组必须分别恰好包含一个 `{model}` 或
`{reasoning_effort}`。AgentBridge 会先验证调用值，再展开为独立 argv，并插入到提示词
或提示词文件参数之前。省略 `allowed_values` 表示允许任意有长度上限的字符串，适合
经常更新的模型目录。

Codex 通过 `start_child_agent`（或兼容的 `start_task`）的 `model` 和
`reasoning_effort` 字段指定它们。调用时省略某个值会采用别名内的 `default`；没有默认
值时则完全不传对应 provider 参数。未配置某类映射的 CLI 会明确拒绝该运行时参数，
不会静默忽略。

任务元数据记录的是 AgentBridge 解析出的选择值，不是 provider 对实际执行模型的独立
证明。启用 `allow_extra_args` 后，调用方不应再附加冲突的模型或 effort 参数；重复参数
最终如何解释由 provider 自己的解析器决定。

### Provider 会话与 follow-up

`session.id_source` 支持两种方式：

- `generated_uuid`：由 AgentBridge 生成 UUID，`start_arguments` 和
  `resume_arguments` 用 `{session_id}` 接收它。Claude 别名分别映射到
  `--session-id` 和 `--resume`。
- `stdout_json`：任务成功后沿 `id_json_path` 从 stdout JSON 中提取字符串，再传给
  `resume_arguments`。Antigravity 别名用 `--output-format json` 返回
  `conversation_id`，并用 `--conversation` 续接。

`send_followup` 会新建一个持久化子任务，同时复用 provider 会话，并继承别名、工作
目录、模型、思考等级、超时和根任务关系。为避免同一会话损坏，只能续接该会话最新的
终态任务，而且同一时刻只允许该会话存在一个活动任务。

参数模式下，提示词可能出现在系统进程列表中；敏感任务优先使用 stdin 或文件模式。
如果某个 CLI 必须使用管道、重定向或其他 shell 语法，请把这些逻辑写进经过审查的
可执行包装脚本，再把脚本路径配置为 `command[0]`。AgentBridge 不会把命令字符串交给
shell。

修改活动参数文件后，让 Codex 调用 `reload_config`。已经运行的任务继续使用启动时的
配置快照；修改状态目录需要重启 MCP server。

## 在 Codex 中使用

例如：

```text
使用 $agent-bridge，让 Claude Code 以 opus 模型和 high 思考等级审查当前仓库，
并把结果返回给我。
```

```text
使用 $agent-bridge 和 antigravity 别名在当前工作目录实现修改；完成后由 Codex
检查 diff 并在本地验证。
```

插件提供以下 MCP 工具：

| 工具 | 用途 |
| --- | --- |
| `list_agents` | 查看别名、限制、提示词模式和可执行文件可用性 |
| `reload_config` | 重新验证活动参数文件并供后续任务使用 |
| `start_child_agent` | 用模型、思考等级和可选父任务启动外部子智能体 |
| `start_task` | `start_child_agent` 的向后兼容别名 |
| `send_followup` | 续接最新 provider 会话，生成新的关联子任务 |
| `get_task` | 不等待，读取当前元数据和分页输出 |
| `wait_task` | 在出现未读输出、进度/完成、取消或最长 50 秒到期时返回 |
| `list_tasks` | 列出最近任务，可按状态过滤 |
| `cancel_task` | 幂等取消一个活动进程组 |

任务状态包括 `queued`、`running`、`cancelling`、`succeeded`、`failed`、
`timed_out`、`cancelled` 和 `interrupted`。`succeeded` 表示进程返回码为零，而且必要的
输出捕获和终态元数据持久化均正常完成；Codex 仍需核实重要结论和工作区改动。
若出现 `persistence_error`，说明终态元数据写入失败，内存中的结果已保守地改为
`failed`。

每个任务都会报告 `task_kind: external_child_agent`、`parent_task_id`、
`root_task_id`、`child_task_ids`、`invocation`、桥接层解析的 `model`/
`reasoning_effort`，以及 provider 支持时的 `session_id`。Codex 因此可以重建外部委派
树，同时不会把它误报为原生 Codex agent 线程树。

输出按字节偏移分页；当 `has_more` 为 true 时继续使用该流的 `next_offset`。出现
运行中任务的 `incomplete_utf8_tail` 为 true 时，应等待外部进程写完多字节字符后再读。
出现 `stdout_truncated` 或 `stderr_truncated` 表示达到了配置的存储上限。

当出现输出或其他任务变化时，`wait_task` 可以在任务进入终态前返回。后续调用应复用
两个流返回的 `next_offset`，并在状态仍非终态时继续等待。MCP
`notifications/cancelled` 只停止对应的等待响应，不会终止持久存在的外部任务；如需
结束 provider 进程，应显式调用 `cancel_task`。

## 状态与安全边界

状态目录依次取自 `AGENT_BRIDGE_STATE_DIR`、`$PLUGIN_DATA/tasks`、
`$XDG_STATE_HOME/agent-bridge/tasks`，最后回退到
`~/.local/state/agent-bridge/tasks`。支持 POSIX 权限的文件系统上，目录使用 `0700`，
元数据和日志使用 `0600`。

持久化命令元数据会隐藏提示词和运行时额外参数；文件模式的提示词会在结束后删除。
但是外部 CLI 自己控制 stdout/stderr，可能把提示词、参数、源码、凭据或其他敏感数据
重新输出到日志。不要在提示词或命令行参数中放入秘密，并妥善保护状态目录。

一个已启用别名意味着：当 Codex 调用启动或 follow-up 工具时，允许执行对应 argv。
应把参数文件和包装脚本当作可执行配置审查。使用 `allowed_work_roots`、禁用别名、外部 CLI 的
保守权限模式以及 Codex 审批设置来匹配风险模型。AgentBridge 不会绕过登录、工作区
信任、访问挑战或权限控制。

## 开发与验证

运行时没有第三方 Python 依赖。本项目在 WSL 2 中开发，已使用 Pixi CPython 3.10.21、
3.12.13 和系统 CPython 3.14.4 运行测试；本地 Bash 为 5.3.9。支持目标为 Linux/WSL
上的 Python 3.10+ 和 Bash 4.4+。

在仓库根目录执行：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q plugins/agent-bridge/scripts tests
bash -n plugins/agent-bridge/scripts/*.sh
python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
python3 -m json.tool plugins/agent-bridge/.codex-plugin/plugin.json >/dev/null
python3 -m json.tool plugins/agent-bridge/.mcp.json >/dev/null
```

测试套件只调用本地 fake agent，不会向 Claude Code、Antigravity 或网络服务发送提示词。

## 许可证

AgentBridge 使用 MIT License，见 [LICENSE](LICENSE)。
