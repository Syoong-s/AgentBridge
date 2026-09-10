# AgentBridge

AgentBridge 是一个本地 Codex 插件：它可以通过用户配置的命令行 agent 启动任务，
并把有大小上限的 stdout、stderr、退出状态和生命周期元数据返回给 Codex。

仓库内置的 Claude Code 示例使用已经验证的非交互 `-p` 模式。Antigravity 以禁用
模板提供，因为其具体参数应以本机所安装版本的帮助信息为准。其他 agent CLI 也可以
通过新增 JSON 别名接入。

## 主要能力

- 用 argv 数组直接启动进程，不隐式调用 shell；
- agent 别名和启动指令完全由一个 JSON 参数文件控制；
- 支持参数、stdin、临时提示词文件三种传递模式；
- 支持异步任务、有界等待、分页日志和持久化元数据；
- 支持并发限制、超时、POSIX 进程组取消和服务重启恢复；
- 支持工作目录白名单、别名级环境变量和额外参数策略；
- 仅使用 Python 标准库，无需安装第三方包。

AgentBridge 不是模型服务，也不会替其他 CLI 建立沙箱。外部 agent 使用该命令在本机
已有的权限和认证状态运行。

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
| `environment` | 支持占位符的静态环境变量映射 |
| `timeout_sec` | 该别名的默认超时 |
| `max_output_bytes` | 每个输出流的存储上限，1 KiB–100 MiB |
| `allow_extra_args` | 是否允许在固定命令后附加独立的运行时 argv 项 |

支持 `{prompt}`、`{prompt_file}`、`{cwd}`、`{task_id}` 四种占位符。
`argument` 模式必须恰好包含一个 `{prompt}`；`file` 模式必须恰好包含一个
`{prompt_file}`，并在任务结束后删除私有临时文件；`stdin` 模式不能包含这两个提示词
占位符。`command[0]` 不允许包含提示词占位符。

参数模式下，提示词可能出现在系统进程列表中；敏感任务优先使用 stdin 或文件模式。
如果某个 CLI 必须使用管道、重定向或其他 shell 语法，请把这些逻辑写进经过审查的
可执行包装脚本，再把脚本路径配置为 `command[0]`。AgentBridge 不会把命令字符串交给
shell。

修改活动参数文件后，让 Codex 调用 `reload_config`。已经运行的任务继续使用启动时的
配置快照；修改状态目录需要重启 MCP server。

## 在 Codex 中使用

例如：

```text
使用 $agent-bridge 让 Claude Code 审查当前仓库，并把结果返回给我。
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
| `start_task` | 用明确的别名、提示词和 `cwd` 启动异步任务 |
| `get_task` | 不等待，读取当前元数据和分页输出 |
| `wait_task` | 最多等待 50 秒，再返回元数据和输出 |
| `list_tasks` | 列出最近任务，可按状态过滤 |
| `cancel_task` | 幂等取消一个活动进程组 |

任务状态包括 `queued`、`running`、`cancelling`、`succeeded`、`failed`、
`timed_out`、`cancelled` 和 `interrupted`。`succeeded` 只表示进程返回码为零，
Codex 仍需核实重要结论和工作区改动。

输出按字节偏移分页；当 `has_more` 为 true 时继续使用该流的 `next_offset`。出现
运行中任务的 `incomplete_utf8_tail` 为 true 时，应等待外部进程写完多字节字符后再读。
出现 `stdout_truncated` 或 `stderr_truncated` 表示达到了配置的存储上限。

## 状态与安全边界

状态目录依次取自 `AGENT_BRIDGE_STATE_DIR`、`$PLUGIN_DATA/tasks`、
`$XDG_STATE_HOME/agent-bridge/tasks`，最后回退到
`~/.local/state/agent-bridge/tasks`。支持 POSIX 权限的文件系统上，目录使用 `0700`，
元数据和日志使用 `0600`。

持久化命令元数据会隐藏提示词和运行时额外参数；文件模式的提示词会在结束后删除。
但是外部 CLI 自己控制 stdout/stderr，可能把提示词、参数、源码、凭据或其他敏感数据
重新输出到日志。不要在提示词或命令行参数中放入秘密，并妥善保护状态目录。

一个已启用别名意味着：当 Codex 调用 `start_task` 时，允许执行对应 argv。应把参数
文件和包装脚本当作可执行配置审查。使用 `allowed_work_roots`、禁用别名、外部 CLI 的
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
