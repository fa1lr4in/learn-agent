# learn-agent

一个正在逐步开发的本地编程 Agent，使用 Python 实现核心能力。

课程进度、完整开发路线和公众号写作节点见 [学习与开发总计划](学习与开发总计划.md)。

## 已实现功能

- 调用 DeepSeek 官方 API，发送问题并查看完整响应。
- 单独展示助手回答、停止原因和 token 用量，提示输出被截断的情况。
- 通过 `--chat` 连续对话，每次请求携带本次会话的历史，输入 `/exit` 退出。
- 输入 `/clear` 清空当前对话历史，保留系统指令；清空操作本身不调用模型。
- 预览请求内容，无需密钥或网络调用。
- 从环境变量或终端读取 API Key，通过环境变量选择模型。
- 根据模型的工具请求，经用户确认后读取指定练习文件，再将结果交回模型。
- 提供 `list_files` 和 `read_file` 两个工具，支持逐层浏览选定工作目录中的目录并读取普通 UTF-8 文件，不再限定五个文件名。
- 提供 `search_text` 工具，在指定目录递归搜索区分大小写的单行关键词，返回相对路径、行号和受长度限制的行摘要，支持缩小范围与限制结果数。
- 所有工具共用工作目录路径检查，拦截绝对路径、`..`、盘符和解析后越界的链接；不访问点号开头的文件与目录。
- 提供 `write_file` 工具，在选定工作目录已有的目录中创建或覆盖 UTF-8 文件，支持修改子目录源码；课程输入资料和固定验收脚本按实际位置只读保护。写入前展示完整内容并提示创建或覆盖，经用户确认后才写入。
- 提供 `edit_file` 工具，按原文精确替换现有文件的一处内容；写入前展示统一差异，确认后重新核对原文件。未匹配、多处匹配、无改动或文件已变化时返回 `EditConflict`，不写入。
- 提供 `run_python` 工具，经用户确认后运行选定工作目录内的普通 `.py` 文件，以脚本所在目录为工作目录，支持导入同项目模块；将退出码、标准输出和标准错误交回模型，支持执行超时反馈。
- 提供 `run_command` 工具，经确认执行程序及参数列表，支持指定项目内工作目录、1 至 120 秒时限、输出上限与前台进程清理，可执行 `python -m unittest` 等测试命令。
- 支持脚本错误反馈与修复：模型根据实际 `stderr` 修改代码，再次运行并检查结果；每次写入和运行仍分别确认。
- 提供固定验收测试，检查 `sum_to(n)` 的四组输入与预期结果，支持根据测试失败信息修复计算逻辑；文件工具不允许改写验收脚本。
- 文件工具演示支持从命令行传入问题，模型可直接回答或提出工具请求。
- 支持 Agent 循环：每轮按顺序处理一个或多个工具请求，携带全部结果继续请求模型，直到模型正常回答。
- 文件工具返回 JSON：`ok: true` 携带操作结果；文件操作失败时返回 `ok: false` 和错误类型，让模型据此调整后续操作。
- 每次运行最多请求模型 8 次，包含最终回答所需的请求；用户拒绝、非法工具请求或模型异常结束时停止。
- 工具循环每次模型请求的输出上限为 2048 个 token；最终回答默认简要说明结论和执行证据，输出截断时明确提示并保留先前结果。
- 检查工具名称、参数和允许读写的文件；拒绝确认时停止执行。
- 每次 Agent 运行生成本地 JSONL 日志，记录模型请求与响应、工具确认与结果，以及结束原因。
- 支持 `--auto-read` 权限策略：自动允许浏览、搜索和读取工作目录内的可访问文件，写入、编辑和运行仍逐次确认，并记录授权来源。
- 支持 `--resume 日志路径 新任务`，恢复正常结束或因模型请求次数上限停止的会话；历史工具操作不会自动重放，未执行请求会标记状态。
- 将模型请求、Agent 循环、工具、权限和日志拆到 `learn_agent/` 包；工具统一注册，循环按注册表检查参数、申请权限并执行，工具说明也从同一注册表生成。
- 支持 `--workspace DIR` 选择已有项目目录，省略时使用本仓库的 `workspace/`；展示根目录并写入系统指令和日志，工具路径相对本次根目录，恢复日志时检查项目是否一致。
- 每次命令行任务保存文件工具检查点，支持 `--diff LOG` 查看本次变化、`--undo LOG` 确认后撤回；保留原有未提交内容和暂存区，检测待撤回文件的后续变化。
- 任务结束后将 `Session`、`Message` 和 `ToolRun` 用一个事务保存到本地 SQLite；支持 `--sessions` 列出会话、`--session ID 新任务` 继续同一个会话，预览不写数据库，历史工具不自动重放。
- 根据实际事件记录运行、等待确认、完成、失败、取消与暂停状态；支持 `--status LOG` 只读查看一次任务的状态变化，拒绝或结束后不启动后续操作。

模型调用、多轮对话、清空历史、连续读取两份文件、读取失败后改读其他文件、生成报告、运行日志、读取自动授权、同轮多工具、写入并执行 Python 脚本，以及脚本报错后修改并重新运行的完整流程已由使用者真实运行成功。验收测试已真实验证先失败、修正后通过，主程序实际输出 `5050`，最终回答完整且正确区分输出字段，结束原因为 `stop`。正常会话恢复与超限中断接续也已真实验证成功；工作目录浏览与子目录源码读取也已真实验证成功，模型找到了入口、核心函数并给出正确统计结果。多文件项目已验证先失败、模型修改后四项测试通过，入口输出包含 `min: 12`、`max: 30`，结束原因为 `stop`。局部编辑已验证拒绝时保留原文件、确认后只替换一处，四项测试通过，入口输出总和 30、平均值 10.0、最小值 5、最大值 15，正常结束。代码搜索已真实验证先出现 `truncated: true`，缩小范围后找到 `stats.py` 第 1 行的 `summarize()`，读取源码后正确回答，全程没有修改或运行代码，结束原因为 `stop`。第 25 课拆分后的真实任务已验证搜索与读取自动允许、两次运行分别确认、四项测试通过、入口输出正确，日志正常结束。第 26 课已真实验证根目录与相对路径正确、两次运行确认、测试与入口输出正确、日志记录选定 workspace 并正常结束。第 27 课已真实验证两次 run_command 分别确认、测试和入口输出正确、无超时或截断，授权来源为 user，正常结束。第 28 课已核对真实任务日志与撤回后的文件，第五篇正文与截图已处理完成。第 29 课已真实验证跨进程加载历史、同 ID 续聊且不重放工具，新增对话继续保存，停止原因与 run_end.reason 均为 stop。第 30 课任务状态与事件已实现；真实运行发现根目录前缀重复，已修正系统指令和工具说明中的旧示例，当前共 208 项自动化测试通过，拒绝操作的状态轨迹已真实验证，成功执行与完成状态的真实验收待重试。

## 代码结构与工具注册

```text
chat.py                 基础聊天命令行入口
tool_call.py            Agent 命令行入口：参数、请求配置、日志文件和结果展示
learn_agent/
  __init__.py           标记 Python 包
  model.py              call_model / get_api_key：HTTP 请求与本地密钥读取
  loop.py               run_agent：模型与工具之间的循环、批次校验和停止条件
  tools.py              内置工具实现、Tool / PreparedTool 和 TOOL_REGISTRY
  commands.py           前台命令、工作目录、时限、输出上限和进程清理
  checkpoints.py        文件工具快照、本次差异、冲突检查和受控撤回
  permissions.py        工作目录边界、只读规则和 approve_tool 确认策略
  events.py             JSONL 记录、历史恢复和消息与工具对应检查
  sessions.py           Session / Message / ToolRun、SQLite 事务保存与按 ID 恢复
  tasks.py              TaskStatus、事件推导状态与只读回放
tests/                  使用临时目录和模拟模型响应的自动化测试
workspace/              Agent 可以操作的练习目录
```

两个入口共用 `model.py`，Agent 入口调用 `loop.run_agent()`。循环通过工具名称从 `TOOL_REGISTRY` 取出 `Tool`，先校验整批请求，再依次准备操作、确认权限、执行工具、记录结果，将结果放进下一轮消息。原有命令和工具的参数、确认策略与结果字段保持一致。

每个 `Tool` 一起登记四项信息：`schema` 是给模型看的名称、说明和参数；`validate` 检查参数值与路径；`prepare` 生成 `PreparedTool`，保存确认提示与待执行函数；`read_only` 决定该工具能否被 `--auto-read` 自动允许，默认是 `False`。准备编辑时只读取原文并展示差异，真正写入放在确认后的 `execute()` 中。

新增工具需要实现对应检查和准备函数，再调用 `register_tool(Tool(...))`；模型请求里的 `tools` 由 `tool_schemas()` 生成，循环不再增加工具名称判断。参数字段统一按 schema 的必填项与允许字段检查，具体类型、取值和路径限制仍由该工具的 `validate` 检查，这不是完整 JSON Schema 校验器。`read_only` 由本地开发者登记，模型的工具参数不能自行改变权限等级。

第 25 课先运行注册表测试，再用现有项目核对拆分后的完整流程：

```powershell
python -B -X utf8 -m unittest discover -s tests -p test_registry.py -v
python tool_call.py --preview "检查当前工具说明。"
python tool_call.py --auto-read "请先在 demo_project/src 中搜索 summarize，再读取函数文件和 demo_project/main.py，随后运行 demo_project/test_stats.py 与 demo_project/main.py，简要报告测试结果和实际统计值。本次只验证现有代码，不创建或修改任何文件；以工具实际返回为准。"
```

注册表测试会临时注册一个只用于测试的 `echo` 工具，验证它可以进入预览、模型请求、权限确认、消息历史和日志；测试结束后恢复正式注册表，echo 不会进入正常程序。真实任务中浏览、搜索和读取自动允许，两次 Python 执行仍需分别输入 `y`。保留第 23 课验证后的代码时，四项测试应通过，入口输出为 `count: 3`、`total: 30`、`average: 10.0`、`min: 5`、`max: 15`，最终停止原因和日志结束原因应为 `stop`。

## 运行

需要 Python 3.11 或更新版本，无第三方依赖。

```powershell
# 预览请求
python chat.py --preview "你好"

# 调用模型；按本地终端提示输入密钥
python chat.py "请用一句话介绍你自己。"

# 开始多轮对话，首条消息作为命令参数传入
python chat.py --chat "我的项目叫小灯。"

# 预览文件工具请求，不联网或读写文件
python tool_call.py --preview

# 运行 Agent 循环；默认先列文件、读取两份资料，再保存报告
python tool_call.py

# 自动允许列出和读取练习文件并发送结果；写入报告时仍需确认
python tool_call.py --auto-read

# 检查实际保存的报告
Get-Content -LiteralPath .\workspace\report.md -Encoding UTF8

# 也可以传入自己的问题
python tool_call.py "请读取 project.txt，告诉我项目使用什么语言。"

# 让 Agent 写入并运行脚本；写入和执行分别确认
python tool_call.py --auto-read "请用 write_file 创建 main.py，计算 1 到 100（包含 100）的整数和，只打印结果。然后用 run_python 运行它，根据实际输出告诉我结果。"
```

可以通过 `DEEPSEEK_API_KEY` 环境变量提供密钥，通过 `DEEPSEEK_MODEL` 指定模型；默认模型为 `deepseek-flash`。程序不会将密钥保存到文件。

`chat.py --chat` 的多轮历史保存在当前进程的内存中，每次请求会重新发送该历史。在 `你>` 提示后输入 `/clear`，后续请求只保留系统指令和清空之后的对话。该聊天程序退出后历史不保留。`--preview` 只展示首个请求，不进入交互或调用模型。`tool_call.py` 的任务历史可以通过下面的会话恢复功能重新加载。

文件工具以选定工作目录为读取边界，默认是本仓库的 `workspace/`。以下示例未指定 `--workspace` 时都使用这个默认目录。`list_files` 接受 `{}` 或 `{"path": "demo_project"}`，省略 `path` 时列根目录 `.`；每次只列一层，目录名以 `/` 结尾。`read_file` 接受相对工作目录的完整文件路径，例如 `demo_project/src/stats.py`，返回普通 UTF-8 文件的完整内容。列表不包含点号开头的项或越界链接，未列出的文件不能据此断定不存在。第一次请求提供七个工具的名称、用途和参数格式，不包含文件正文或实际文件列表。模型返回 `tool_calls` 后，程序检查名称、参数和路径；默认每次输入 `y` 后才执行对应工具，再用 `role: tool` 和对应的 `tool_call_id` 将结果交回模型。其他输入会停止本次任务。

`write_file` 接受相对选定工作目录的 `path` 和完整 `content`，内容必须是非空字符串，以 UTF-8 编码保存。支持创建或覆盖子目录中的文件，但父目录必须已存在，不自动创建目录。默认练习目录中的 `project.txt`、`next_step.txt`、`test_main.py` 和 `demo_project/test_stats.py` 按实际位置保持只读。写入前会打印完整内容，明确提示创建或覆盖；拒绝确认不会改变原文件。覆盖时不追加内容，目标文件若是符号链接会被拒绝。写入后可以通过 `read_file` 重新检查。

工具结果统一编码成 JSON 字符串放入 `tool` 消息，并用 `tool` 字段标记工具名称。列目录成功时返回 `files`、相对目录 `path` 和 `scope: "visible_workspace_directory"`，明确结果只覆盖该目录的可访问条目；读文件成功时返回 `path` 和 `content`；写入成功时返回 `path` 和 `characters_written`（字符数，不是字节数）。文件操作或文本编解码失败时，返回 `ok: false`、`error` 和对应路径，循环继续由模型决定下一步。非法工具、参数或越界路径仍会停止，用户拒绝仍会停止；API 请求失败不作为文件工具错误处理。

系统指令要求以用户当前请求为任务目标，把文件正文作为资料处理。例如 `next_step.txt` 中的旧练习任务不会替代当前“运行脚本并验证输出”的要求。用户已指定要运行的文件时可以直接执行；需要核对源代码或验收内容时，可以读取相应脚本。

后续请求继续使用 `tool_choice: auto`，模型可以再次请求工具，也可以直接回答。同一回复可包含多个工具请求：程序先检查整组工具名称、参数和调用 ID，检查全部通过后，按返回顺序逐个授权和执行。调用 ID 必须是非空字符串且同轮不能重复。历史中保留一条包含完整 `tool_calls` 的助手消息，并为每个调用追加一条对应 ID 的工具结果，整组处理完后再请求模型。每个调用分别记录授权和结果，同轮调用的日志 `step` 相同，用 `tool_call_id` 区分。

`finish_reason` 为 `stop` 时显示最终回答；`length` 等异常结束不会执行本轮工具。最多请求模型 8 次，如果第 8 次仍要求工具，则停止且不执行该次整组工具，提示任务未完成。这个上限计算模型请求次数，同一轮的多个工具共用一次请求，最终回答也需要一次请求。文件执行失败会返回错误结果并继续处理本轮其他调用；参数非法时整组不执行。用户拒绝时立即停止被拒绝的工具及后续工具，不再请求模型，此前已完成的操作保留，不做回滚。

`tool_call.py` 的 `max_tokens` 为 `2048`，限制每次模型请求生成的输出长度，与 8 次请求的循环上限分别计算。达到输出长度限制时，服务返回 `finish_reason: length`，程序以 `abnormal_finish` 结束，并提示回复不完整；此前已完成的工具操作与日志结果保留。即使部分文字已经给出正确结论，也不会把截断回复标记为正常完成，截断回复中的工具请求同样不会执行。

系统指令要求最终回答默认简洁，并按实际字段分别说明 `stdout` 和 `stderr`。`stderr` 非空本身不代表执行失败，例如 `unittest` 会把成功报告也写入该字段。

默认任务的常见顺序是：列文件 → 读取两份资料 → 写报告 → 最终回答。两次读取放在同一回复中时，共 4 次模型请求；分两轮读取时，共 5 次。上述两种安排均执行 4 个工具，默认模式需要 4 次确认；开启 `--auto-read` 后，3 次读取操作自动批准，仅写入报告需要确认。实际安排以模型返回的 `tool_calls` 为准，`stop` 只表示回复正常结束，仍需检查写入工具的成功结果和磁盘文件，确认任务完成。

## 工具权限

| 工具 | 默认模式 | `--auto-read` |
|---|---|---|
| `list_files` | 每次询问 | 自动允许 |
| `read_file` | 每次询问 | 自动允许 |
| `search_text` | 每次询问 | 自动允许 |
| `write_file` | 每次询问 | 每次询问 |
| `edit_file` | 每次询问 | 每次询问 |
| `run_python` | 每次询问 | 每次询问 |
| `run_command` | 每次询问 | 每次询问 |

传入 `--auto-read` 代表允许本次运行自动浏览、搜索、读取选定工作目录内的可访问文件，并把结果发送给 DeepSeek。这个选择由本地命令参数决定，不作为 API 请求参数发送。模型提出的工具请求必须先通过名称、参数和路径检查，再进入授权分支；自动读取策略不放宽这些检查。写入仍然展示完整内容并询问创建或覆盖，拒绝时原文件不变，任务停止；运行脚本仍需逐次确认。

该策略只对当前运行生效。日志中的 `run_start.auto_read` 记录本次选择，`tool_approval.source` 为 `auto_read` 表示读取策略自动批准，为 `user` 表示人工确认；`approved` 记录是否获准执行。

## 浏览多文件项目

演示项目位于 `workspace/demo_project/`：

```text
demo_project/
├── README.md
├── main.py
├── test_stats.py
└── src/
    └── stats.py
```

运行下面的任务，让模型从目录和源码中了解项目：

```powershell
python tool_call.py --auto-read "请先浏览 demo_project 的目录结构，再读取相关源码。告诉我入口文件和核心函数分别在哪，对列表 [12, 18, 30] 会返回什么统计结果。结论要引用文件路径和函数名；本次只阅读，不修改或运行代码。"
```

初始版本中，预期模型使用 `list_files` 逐层浏览，再用 `read_file` 读取相关文件；最终指出入口 `demo_project/main.py`、核心函数 `demo_project/src/stats.py` 中的 `summarize()`，以及 `count: 3`、`total: 60`、`average: 20.0`。具体浏览顺序由模型决定，程序不会预先把目录结构或源码放入请求。

`resolve_workspace_path()` 会先检查相对路径，再解析实际目标并确认仍位于选定工作目录内。请求路径或解析后的路径中存在点号开头的组件时会拒绝，包括 `.env` 和 `.git/`。目录列表按名称排序，隐藏这些项以及越界链接。读取不存在的文件、将目录当作文件读取或对文件列目录，会返回结构化错误供模型调整。

写入与运行也使用相同的路径边界：`resolve_write_path()` 额外检查只读资料和验收脚本，`resolve_python_path()` 检查 `.py` 后缀。两种操作均拒绝以符号链接作为目标；越界和只读检查在确认前完成，执行时再次检查。

## 修改并验证多文件项目

本轮目标是在 `demo_project/src/stats.py` 的 `summarize(values)` 返回值中增加 `min` 和 `max`，同时保留 `count`、`total`、`average`。空列表的两个新字段均为 `None`。

固定验收脚本 `demo_project/test_stats.py` 提供四组用例：普通列表、空列表、单个数字和负数。仓库中的初始 `stats.py` 尚未实现新字段，因此第一次运行这些测试应失败；由 Agent 修改源码后再验证。验收脚本不能通过 `write_file` 改写，入口文件无需修改。

```powershell
$taskPrompt = @'
请为 demo_project/src/stats.py 的 summarize(values) 增加 min 和 max 两个字段，空列表时这两个字段均为 None，保留原有 count、total、average 的行为。
先读取 demo_project/src/stats.py 和 demo_project/test_stats.py，再运行 demo_project/test_stats.py 观察现有结果。
只修改 demo_project/src/stats.py，不要改写或跳过测试，也不要修改入口文件。修改后再次运行 demo_project/test_stats.py，并运行 demo_project/main.py。
最后根据真实工具结果简要说明改动、测试和程序输出。
'@
python tool_call.py --auto-read $taskPrompt
```

读取自动允许，每次写入或运行仍需输入 `y`。预期先取得失败测试，再写入修改后的源码，最后测试的 `exit_code` 为 `0`，`stderr` 含 `Ran 4 tests` 和 `OK`；入口脚本的 `stdout` 为 `{'count': 3, 'total': 60, 'average': 20.0, 'min': 12, 'max': 30}`。任务结束原因和日志 `run_end.reason` 均应为 `stop`。模型决定实际工具顺序和调用次数，仍受每次运行最多 8 次模型请求的限制。

该过程把目录阅读、源码修改、测试反馈和入口运行串成一个完整的多文件项目开发流程。`write_file` 保存完整文件，局部修改可以使用下节介绍的 `edit_file`。

## 代码搜索与定位

`search_text` 必须提供 `query`，可选 `path`（默认 `.`）和 `max_results`（默认 20，范围 1 至 50）。`query` 是区分大小写的非空单行字符串，按字面匹配，不接收正则表达式。`path` 必须是选定工作目录内的可访问目录；非法参数和越界路径会在确认前拦截。

返回的 `matches` 每项包含相对工作目录的 `path`、从 1 开始的 `line`、`text` 和 `text_truncated`。每个匹配行只返回一次，摘要最多 200 个字符，优先保留关键词附近内容；`text_truncated` 表示该行没有完整展示。定位之后仍需通过 `read_file` 核对源码，再决定如何修改。

超过返回上限且确实找到额外命中时，`truncated` 为 `true`；恰好等于上限时不自动标记截断。可缩小 `path`，或在允许范围内调整结果数。无匹配是正常结果：`ok: true`、`matches: []`。目录不存在或把文件作为搜索目录，会返回 `NotADirectoryError`。

搜索仅覆盖 `scope: visible_workspace_text`：递归时跳过点号开头的项、符号链接和解析后指向其他位置的目录链接，以及 `node_modules`、`__pycache__`、`venv`、`build`、`dist` 子目录（名称不区分大小写）。这里只使用固定过滤规则，不解析项目的 `.gitignore`。每个文件最多读取 256 KiB 加一个检测字节；超过大小限制、包含零字节、UTF-8 解码失败或无法读取的文件计入 `skipped_files`，遍历目录的访问错误计入 `skipped_directories`。这些计数和范围限制意味着没有命中不等于整个项目不存在相关代码。

默认每次搜索都需确认；`--auto-read` 同时批准搜索、目录浏览和读取，结果会发送给 DeepSeek。搜索不修改文件，结果与授权记录仍写入本次日志。

```powershell
$taskPrompt = @'
请先使用 search_text 在 demo_project 中搜索 summarize，max_results 设为 2。
如果 truncated 为 true，请缩小到 demo_project/src 继续搜索，定位函数定义，再读取业务源码和 demo_project/main.py。
告诉我函数定义的文件和行号、入口当前的输入列表、空列表的返回值，以及当前输入的预期输出。结论请引用源码路径；本次只搜索和阅读，不修改或运行代码。
'@
python -B -X utf8 tool_call.py --auto-read $taskPrompt
```

第 23 课已完成时，预期先观察到结果截断，再找到 `demo_project/src/stats.py:1` 的 `summarize()`。入口输入为 `[5, 10, 15]`；按源码推算数量为 3、总和 30、平均值 10.0、最小值 5、最大值 15。空列表返回数量 0、总和 0、平均值 0.0、最小值和最大值均为 `None`。这次不运行代码，最终结论应明确来自源码阅读，结束原因和 `run_end.reason` 均为 `stop`。

## 局部编辑与差异确认

`edit_file` 接受 `path`、`old_text` 和 `new_text`。适合修改现有文件的一小段；创建新文件仍使用 `write_file`。`old_text` 必须是非空字符串，按字面匹配，不支持正则表达式；`new_text` 可以为空，表示删除匹配内容。

`prepare_edit()` 读取 UTF-8 文件，只在旧内容恰好匹配一处时计算替换结果，并展示 `--- a/路径`、`+++ b/路径`、`-旧行` 和 `+新行`。Windows CRLF 与 LF 换行均可匹配，未编辑部分的字节保持原样。用户输入 `y` 后，`apply_edit()` 重新检查路径和原始字节快照，再写入结果。确认期间文件发生变化时，不覆盖用户的新内容。

旧内容未匹配、匹配到多处、新旧内容相同，或预览后的文件已变化，都会返回 `ok: false` 和 `error.type: EditConflict`。不满足匹配条件时不会询问确认，也不写入；模型可以重新读取并补充定位上下文。参数或路径非法仍会停止。编辑复用 `write_file` 的只读限制，`--auto-read` 不会自动批准编辑。

成功结果包含 `path`、`replacements: 1` 和实际 `diff`，差异会进入工具历史与日志。只在成功写入后返回 `ok: true`。同一批次内的编辑逐个准备、预览和确认，后一次会看到前一次已保存的文件内容。当前实现核对确认前后的文件快照，没有提供操作系统文件锁或多文件事务。

第 22 课已完成时，可以继续修改同一项目的入口输入，保留统计函数和四项验收不变：

```powershell
$taskPrompt = @'
请先读取 demo_project/main.py，再使用 edit_file 只把 values = [12, 18, 30] 改成 values = [5, 10, 15]。
不要使用 write_file，不修改其他内容或文件。编辑后运行 demo_project/test_stats.py 和 demo_project/main.py，根据实际结果简要说明改动、测试和输出。
'@
python -B -X utf8 tool_call.py --auto-read $taskPrompt
```

预期编辑前的差异只有输入列表这一行发生变化，经确认后返回 `replacements: 1`。四项测试仍通过，入口的 `stdout` 包含 `count: 3`、`total: 30`、`average: 10.0`、`min: 5`、`max: 15`，最终停止原因和 `run_end.reason` 均为 `stop`。本任务会实际修改入口；拒绝编辑确认时，入口保留原样，本次任务以 `user_denied` 停止。

## 运行 Python

`run_python` 接受相对选定工作目录的 `.py` 文件路径，例如 `{"path": "main.py"}`、`{"path": "test_main.py"}` 或 `{"path": "demo_project/main.py"}`。程序使用当前 Python 解释器启动子进程，以脚本所在目录为工作目录，通过参数列表启动，最长运行 10 秒，关闭标准输入，按 UTF-8 捕获输出。脚本必须是存在的普通文件，符号链接会被拒绝。

子进程使用 `-E -s -B -X utf8`：忽略 Python 配置环境变量、不加载用户 site-packages、不生成字节码缓存，并启用 UTF-8。脚本目录会加入模块搜索路径，因此 `main.py` 能通过 `from src.stats import summarize` 导入业务代码。

脚本执行结果包含 `exit_code`、`stdout` 和 `stderr`。退出码为 `0` 时 `ok` 为 `true`，非零时为 `false`，语法错误和运行错误会保留在 `stderr` 中交回模型。超时会终止直接启动的 Python 子进程，返回 `ok: false` 和 `TimeoutExpired` 错误；脚本不存在或无法启动时也返回错误结果。模型应根据工具结果判断运行情况，`stop` 仍然只表示模型回复结束。

执行失败后，循环保留报错和先前写入的代码，模型可据此调用 `write_file` 覆盖脚本，再调用 `run_python` 验证修复。系统指令要求先修改再重试，具体修改仍由模型决定；程序不保证每次修复成功，仍受 8 次模型请求上限约束。

可以用下面的任务验证完整修复过程。`nubmers` 是故意写错的变量名，第一次运行应返回 `NameError`，修正为 `numbers` 后应输出 `5050`。这会覆盖之前的 `workspace/main.py`，两次写入和两次运行都需要确认。

```powershell
$taskPrompt = @'
这是一次错误恢复练习。先用 write_file 将 main.py 原样写为下面的代码，不要提前修正：
numbers = list(range(1, 101))
print(sum(nubmers))
然后用 run_python 运行一次，拿到真实报错后再修复，并重新运行验证。最后根据两次执行结果说明错误和修复。
'@
python tool_call.py --auto-read $taskPrompt
```

`--auto-read` 不会自动批准执行代码，写入和运行需要分别确认。子进程不会直接继承 `DEEPSEEK_API_KEY` 环境变量。当前实现没有操作系统沙箱，脚本仍可访问当前账户有权限的文件和网络，确认执行前应检查代码内容。

## 验收测试

退出码为 `0` 只表示脚本正常退出，仍需要检查是否符合需求。例如 `sum(range(1, 100))` 会正常得到 `4950`，但没有包含题目要求的 `100`。

`workspace/test_main.py` 使用标准库 `unittest` 验收 `main.py` 中的 `sum_to(n)` 函数，检查 `0 → 0`、`1 → 1`、`10 → 55`、`100 → 5050`。它通过 `runpy.run_path` 加载同目录的主程序，取得函数后逐项比较结果；加载时不会执行主程序的 `if __name__ == "__main__"` 分支。验收脚本随项目提供，`write_file` 的允许范围不包含它。

在项目根目录运行下面的任务，会覆盖现有 `workspace/main.py`。初始实现故意漏加最后一个数，先取得真实测试失败信息，再让模型修改主程序并重新验收。

```powershell
$taskPrompt = @'
这是一次验收测试练习。sum_to(n) 应返回从 1 到 n（包含 n）的整数和，n=0 时返回 0。
固定验收脚本 test_main.py 会检查 sum_to(0)=0、sum_to(1)=1、sum_to(10)=55、sum_to(100)=5050。
先用 write_file 将 main.py 原样写为以下代码，不要提前修正：
def sum_to(n):
    return sum(range(1, n))

if __name__ == '__main__':
    print(sum_to(100))
然后直接用 run_python 运行 test_main.py。取得真实失败信息后，只修正 main.py 的计算逻辑，再运行 test_main.py 验证。保留函数和命令行输出，不要删除或跳过测试，最后根据两次验收结果说明问题和修复。
'@
python tool_call.py --auto-read $taskPrompt
```

常见流程为写入错误代码 → 运行验收测试 → 覆盖修正代码 → 再次运行验收测试 → 最终回答，分五次请求模型可以完成。如果模型在测试通过后再运行主程序验证输出，则需要第六次请求给出最终回答。每次写入和运行都需要确认，整个流程受 8 次模型请求上限约束；模型也可能在同一回复中提出多个工具请求，实际轮数以返回结果为准。

第一次验收应返回 `ok: false`、非零退出码，`stderr` 含断言失败信息，例如 `4950 != 5050` 及 `FAILED`。修复后的验收应返回 `ok: true`、退出码 `0`，`stderr` 含 `Ran 4 tests` 和 `OK`。`unittest` 默认将测试报告写入标准错误，因此成功时 `stderr` 也可以非空。测试通过表示满足当前四个测试用例，不能保证所有输入下都正确。

## 执行测试命令

`run_command` 将执行能力扩展为“程序及参数列表”，例如：

```json
{
  "command": ["python", "-m", "unittest", "-v", "test_stats.py"],
  "path": ".",
  "timeout": 30
}
```

`command` 必须是非空字符串数组，首项是程序，其余每一项都是独立参数，参数中的空格不需要再次加引号。`python`、`python3`、`python.exe` 使用当前运行 Agent 的 Python 解释器；其他程序按明确路径或 PATH 查找，不直接执行 `.bat`、`.cmd`。程序以参数列表调用 `subprocess.Popen(shell=False)`，不会自动解析管道、重定向或 `&&`；若显式指定 shell 程序，其行为由该程序和用户确认的参数决定。

`path` 是相对选定项目根目录的工作目录，默认 `.`，必须是项目内已有的可访问目录；它与 `run_python` 默认使用脚本所在目录的规则不同。`timeout` 默认 30 秒，范围为 1 至 120 的整数秒。启动前展示实际程序、完整参数、工作目录和时限，`--auto-read` 不会自动批准命令。标准输入关闭，Python 输出使用 UTF-8 且默认不生成字节码缓存，子进程不会直接继承 `DEEPSEEK_API_KEY`。

命令返回实际 `command`、绝对工作目录 `cwd`、相对目录 `path`、`exit_code`、`stdout`、`stderr`、`timed_out` 和两个输出截断标记。退出码非零时 `ok: false`，保留失败输出；无法启动程序时退出码为 `null`，并提供错误类型。不能用 stderr 是否为空判断测试成功：unittest 的正常报告通常就在 stderr。

输出先写入系统临时文件，避免把无限输出全部读进内存，也不等待管道 EOF。程序约每 50 毫秒检查一次时限和输出量，任一输出流超过 16 KiB 时停止命令，并且每个流最多读取这个数量的原始字节返回；检查间隔内临时文件可能超过上限，这不是操作系统级磁盘配额。超时标记 `timed_out: true` 和 `TimeoutExpired`，输出超限标记 `OutputLimitExceeded`；二者均返回 `ok: false` 和已有输出。即使命令已经自行退出，输出被截断时也不能作为完整验收结果。

对仍在运行的前台命令，超时、输出超限和用户中断会触发进程清理：Windows 使用本次 PID 的 `taskkill /T /F`，再回收直接子进程；其他系统使用本次新建进程组终止。强制停止的工具结果会记录 `cleanup_succeeded`。若为 `false`，说明无法确认进程树完整清理，不能声称全部停止；必要时需用户核对。当前面向会等待子进程的前台命令，不管理已脱离或在父进程退出后留下的后台服务；运行仍按当前账户权限进行，没有操作系统沙箱。工作目录检查不会把程序及其参数中的文件访问限制在项目内。

在 learn-agent 根目录验证第 27 课：

```powershell
$project = (Resolve-Path -LiteralPath .\workspace\demo_project).Path
python tool_call.py --workspace $project --preview "请用 run_command 执行项目测试。"
python tool_call.py --workspace $project --auto-read "请两次都用 run_command，先执行 python -m unittest -v test_stats.py，再执行 python main.py，工作目录均为 .。本次不创建或修改文件。根据真实退出码、stdout、stderr、超时和截断标记简要报告测试与运行结果。"
```

两次执行仍分别输入 `y`。预期第一个命令的退出码为 0，stderr 有 `Ran 4 tests` 和 `OK`；第二个命令退出码为 0，stdout 有 `count: 3`、`total: 30`、`average: 10.0`、`min: 5`、`max: 15`。两次均不超时、不截断，日志的执行授权来源为 `user`，最终 `run_end.reason` 为 `stop`。`run_python` 的旧接口与 10 秒时限保留。

实现可对照 [Python subprocess 文档](https://docs.python.org/3.13/library/subprocess.html) 和 [Windows taskkill 文档](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)。当前实际验证平台为 Windows，其他系统的进程组分支尚未做实际平台验证。

## 运行日志

运行 `tool_call.py` 时，程序会在项目的 `logs/` 目录创建一个新的 `run-年月日-时分秒-微秒.jsonl` 文件，并在终端显示路径。`--preview` 不创建日志。日志逐条写入并刷新，每一行都是一个完整的 JSON 对象，包含 UTC 时间和事件类型。

| 事件 | 记录内容 |
|---|---|
| `run_start` | 本次任务开始、工作目录、检查点路径、模型请求次数上限及读取授权策略 |
| `model_request` | 请求序号 `step` 和发送给模型的请求体 `payload` |
| `model_response` | 请求序号和服务返回的完整 JSON，包含停止原因及服务提供的 token 用量 |
| `tool_approval` | 工具名、调用 ID、是否获准执行及授权来源 |
| `tool_result` | 工具名、调用 ID 和实际执行结果 |
| `run_end` | 结束原因；发生异常时包含异常类型 |

`run_end.reason` 的取值包括 `stop`（模型正常结束）、`user_denied`（用户拒绝）、`max_model_calls`（达到请求上限）、`abnormal_finish`（模型异常结束）、`error`（请求或程序错误）、`cancelled`（用户中断）。异常记录只保存类型，不保存原始异常字符串。

日志记录问题、模型回复和工具读写内容，不记录 API Key 或 HTTP 请求头。`logs/` 已在 `.gitignore` 中排除，普通 Git 提交不会包含运行日志。默认不会加载旧日志；通过 `--resume` 明确选择日志时，才会将其中的历史带入新任务。

在 PowerShell 中查看最近一次运行的事件顺序：

```powershell
$runLog = Get-ChildItem -LiteralPath .\logs -Filter *.jsonl -File |
    Sort-Object LastWriteTime | Select-Object -Last 1
$events = Get-Content -LiteralPath $runLog.FullName -Encoding UTF8 |
    ForEach-Object { $_ | ConvertFrom-Json }
$events | Select-Object event, step, tool, reason | Format-Table -AutoSize
```

## 选择自己的项目目录

使用 `--workspace DIR` 选择一个已经存在的项目目录。优先级是本次命令行指定目录，其次是仓库中固定的默认 `workspace/`；不指定时不受终端当前目录变化影响。指定相对路径时，从当前终端目录解析，支持带中文和空格的路径，需用引号包住。程序会展开 `~`、解析实际根目录，并拒绝空路径、不存在的目录和普通文件，不自动创建目录。

这里先把现有 `demo_project` 作为独立项目验证。在 learn-agent 根目录执行：

```powershell
$project = (Resolve-Path -LiteralPath .\workspace\demo_project).Path
python tool_call.py --workspace $project --preview "浏览当前项目。"

python tool_call.py --workspace $project --auto-read "请先列出当前项目根目录，在 src 中搜索 summarize，再读取 main.py 和 src/stats.py，运行 test_stats.py 与 main.py。简要报告根目录条目、函数位置、测试结果和实际输出。本次不创建或修改文件，所有工具路径相对当前项目根目录。"
```

指定根目录后，工具应读取 `main.py`、`src/stats.py`，运行 `test_stats.py`、`main.py`，不再在这些路径前加 `demo_project/`。预览和运行首行会显示选定根目录，系统指令也会告知模型根目录与当前课程只读文件。读取仍自动允许，两次运行分别输入 `y`；预期四项测试通过，入口输出 `count: 3`、`total: 30`、`average: 10.0`、`min: 5`、`max: 15`，最终与日志结束原因均为 `stop`。换成自己的项目时，将 `$project` 设置为那个项目的已有目录，再提供对应任务。

文件操作和 run_python 继续复用当前根目录的路径检查：模型提供的 `path` 仍不能是绝对路径、包含 `..`、访问点号开头的项或解析后越界。课程固定只读文件按实际文件位置保护，所以选中课程 `demo_project` 后，`test_stats.py` 仍不能通过文件工具改写；其他项目中的同名 `test_main.py` 不会因此被锁定，写入仍需确认。运行 Python 仍不是操作系统沙箱，其进程权限限制见前文。

自定义项目没有提供任务时，默认只浏览和阅读项目，介绍用途、入口与模块；默认练习目录继续使用原来的报告任务。API Key 和模型仍使用原有环境变量。日志统一保存在 learn-agent 的 `logs/` 中，不写入所选项目；`run_start.workspace` 记录本次根目录的绝对路径。目录选择只对本次运行生效，退出或出错后恢复调用前的目录配置；当前是单任务命令行实现。

恢复自定义项目的会话时，需要再次指定同一个项目根目录。可以先预览：

```powershell
$previousRun = Get-ChildItem -LiteralPath .\logs -Filter *.jsonl -File |
    Sort-Object LastWriteTime | Select-Object -Last 1
python tool_call.py --workspace $project --resume $previousRun.FullName --preview "根据历史简要说明上次测试与运行结果，不调用工具。"
```

新日志的根目录与本次不一致时，在读取密钥或联网前拒绝恢复；路径会先规范化，因此同一目录的等价写法仍可使用。第 26 课之前没有 `workspace` 字段的旧日志只允许在默认练习目录恢复。程序不会从日志自动切换项目或继承自动读取授权。

## 检查与撤回本次修改

每次新的 Agent 命令行任务会在 JSONL 日志旁保存同名的 `.checkpoint.json`，`run_start.checkpoint` 和终端提示记录它的位置。检查点只追踪本次经过 `write_file`、`edit_file` 执行的文件：第一次写入前保存当时的原始字节，每次成功写入后保存预期结果与文件状态。同一个文件多次修改仍以第一次修改前的内容为基准。检查点是本地代码备份，位于已被 Git 忽略的 `logs/` 中。

`--diff LOG` 查看该次运行的文件工具差异；`--undo LOG` 展示反向差异，输入 `y` 后撤回。两者都需使用原项目的 `--workspace`，不读取 API Key、不请求模型，也不执行历史工具。检查会同时展示任务开始时和当前的 Git 状态，Git 不可用时仍能按文件快照工作。Git 状态可能包含用户原有修改，不能把所有 `M` 都认作本次 Agent 的修改。

撤回恢复的是本次第一次改文件前的磁盘内容，包含当时已有的未提交修改；它不以 Git HEAD 为恢复目标，不调用 reset、checkout、clean，也不修改 Git 暂存区或提交记录。只撤回该检查点记录的文件：原有文件恢复内容，本次新建文件在仍保持本次版本时删除，其他未跟踪文件保留。恢复的是文件内容，不是整个目录和全部文件元数据。

先检查全部待撤回文件的字节和文件状态，再询问确认；确认后重新检查。如果待撤回文件后来又被改写、重新保存、删除或重定向，整批尚待撤回的文件都不会开始处理。拒绝确认也不修改文件。已撤回文件会记为 `undone`，重复撤回不会再次覆盖；任务仍在运行时不能撤回。

在 learn-agent 根目录验证第 28 课，先保存当前入口文件的哈希：

```powershell
$project = (Resolve-Path -LiteralPath .\workspace\demo_project).Path
$beforeHash = (Get-FileHash -LiteralPath (Join-Path $project "main.py") -Algorithm SHA256).Hash

python tool_call.py --workspace $project --auto-read "请先在项目根目录搜索 values = 并读取命中的入口文件，再用 edit_file 将 main.py 的输入从 [5, 10, 15] 改为 [6, 12, 18]。随后用 run_command 执行 python -m unittest -v test_stats.py，再执行 python main.py。保留其他文件和已有内容，最后简要说明修改与验证结果。"
```

编辑与两次执行分别确认。预期四项测试通过，入口输出总和 36、平均值 12.0、最小值 6、最大值 18，任务正常结束。随后选择这次新日志查看差异：

```powershell
$runLog = Get-ChildItem -LiteralPath .\logs -Filter *.jsonl -File |
    Sort-Object LastWriteTime | Select-Object -Last 1
python tool_call.py --workspace $project --diff $runLog.FullName
```

应仅看到 `main.py` 的本次输入变化，原有备注和其他文件不在本次差异内。然后执行撤回，第一次输入 `n`，确认新输入仍保留；再执行同一条命令并输入 `y`：

```powershell
python tool_call.py --workspace $project --undo $runLog.FullName
```

允许后返回 `ok: true`，`restored` 包含 `main.py`。用下面两条命令核对原始内容恢复：

```powershell
(Get-FileHash -LiteralPath (Join-Path $project "main.py") -Algorithm SHA256).Hash -eq $beforeHash
python -B -X utf8 .\workspace\demo_project\main.py
```

哈希比较应为 `True`，入口恢复为总和 30、平均值 10.0、最小值 5、最大值 15。Git 中仍可能有前面课程留下的修改，这是被保留的原有工作。检查与撤回不会改写原 JSONL 日志；撤回状态只更新在检查点中，重新 `--diff` 应显示尚待撤回 0 个文件。

本版范围与限制：命令、其他程序和用户手动操作产生的变化不自动归属给文件检查点；命令若再次修改了已追踪文件，会导致撤回冲突。Git 暂存与提交操作不被撤销。撤回不是跨文件事务；处理途中发生 I/O 失败或并发变化时，可能已有部分文件恢复；检查点会保留已完成或 `undo_pending` 等中间状态，需要重新查看差异，不会自动强制覆盖。进程崩溃留下的未完成记录也拒绝自动撤回。第 28 课之前的旧日志没有原始字节快照，不能用它们反推撤回。当前仅管理单任务检查点，不自动合并后续修改或连续回退多次任务。

## 结构化保存会话

第 29 课增加 `logs/sessions.sqlite3`，使用 Python 标准库 `sqlite3`，不增加第三方依赖。一次命令行任务结束后，程序从本次 JSONL 提取消息和工具记录，用一个 SQLite 事务保存：

- `sessions`：会话 ID、项目根目录、创建与更新时间、结束原因、最近一次日志。
- `messages`：有顺序的用户、助手和工具消息，保留工具请求及 `tool_call_id`。恢复时使用本次新生成的系统指令，不保存和复用旧系统指令。
- `tool_runs`：本会话各次运行实际提出的工具名称、原始参数字符串、授权来源及结果。没有执行结果时保持空值，不能算作执行成功。

普通任务创建新会话，终端打印 `[本次会话]` 和结束后的 `[会话已保存]`。使用 `--session ID` 继续时，会话 ID 不变，每次运行仍有独立 JSONL 和检查点；消息保存为完整的新快照，历史部分不会重复追加，工具运行记录保留。`--resume LOG` 仍按原方式恢复 JSONL，并创建新的 SQLite 会话；它继承消息，不自动导入旧日志为新的工具执行记录。

在 learn-agent 根目录验证，先完成一个只读任务：

```powershell
$project = (Resolve-Path -LiteralPath .\workspace\demo_project).Path
python -B -X utf8 tool_call.py --workspace $project --auto-read "请读取 main.py 和 src/stats.py，说明入口当前使用的 values 以及 summarize 返回的字段。本次只读取，不创建、修改或运行文件，回答保持简短。"
if ($LASTEXITCODE -ne 0) { throw "任务异常结束，请先检查输出。" }
$runLog = Get-ChildItem -LiteralPath .\logs -Filter *.jsonl -File |
    Sort-Object LastWriteTime | Select-Object -Last 1
$sessionId = (Get-Content -LiteralPath $runLog.FullName -TotalCount 1 -Encoding UTF8 |
    ConvertFrom-Json).session_id
Write-Host "本次会话 ID：$sessionId"
$sessionId | Set-Content -LiteralPath .\logs\lesson29-session-id.txt -Encoding UTF8
python -B -X utf8 tool_call.py --sessions
```

预期两份源码读取成功，任务以 `stop` 结束，并显示保存到 `sessions.sqlite3` 的会话 ID。列表操作不读取密钥或调用模型。数据库与原有 JSONL、检查点一起位于被 Git 忽略的 `logs/` 中，不提交到 GitHub。

会话 ID 由本地程序在真实任务开始时生成；先完成上面的任务，再从本次新日志取得 ID 并保存到 `logs/lesson29-session-id.txt`。这些步骤在同一个终端顺序执行，期间不启动其他任务。预览不会创建会话，不预先填写某个已有 ID。可以关闭终端，再打开 PowerShell 并进入 learn-agent 根目录，从本地文件加载刚才的 ID。程序会在读取密钥或调用模型前拒绝空字符串和纯空白 ID。

```powershell
$project = (Resolve-Path -LiteralPath .\workspace\demo_project).Path
$sessionId = (Get-Content -LiteralPath .\logs\lesson29-session-id.txt -Raw -Encoding UTF8).Trim()
python -B -X utf8 tool_call.py --workspace $project --session $sessionId --preview "只根据历史，告诉我入口的 values 和 summarize 返回的字段，不调用工具。"
python -B -X utf8 tool_call.py --workspace $project --session $sessionId "只根据历史，告诉我入口的 values 和 summarize 返回的字段，不调用工具。"
```

预览应包含上次的用户、助手、工具请求和工具结果，再接上本次问题；不读取密钥、不调用模型、不创建日志或写入数据库。正式续聊应从历史回答，保存后仍显示同一个会话 ID。当前练习代码中的输入是 `[5, 10, 15]`，字段为 `count`、`total`、`average`、`min`、`max`。

还可直接读出这份会话的结构化数据，不调用模型。以下代码是 ASCII，Windows PowerShell 管道也可执行：

```powershell
@'
from pathlib import Path
import sys
from learn_agent.sessions import SessionStore

session, messages, tool_runs = SessionStore(Path('logs/sessions.sqlite3')).read(sys.argv[1])
print('session_id:', session.id)
print('reason:', session.reason)
print('messages:', len(messages))
print('tool_runs:', len(tool_runs))
for run in tool_runs:
    print(run.name, 'approved=', run.approved, 'source=', run.approval_source,
          'ok=', run.result.get('ok') if run.result is not None else None)
'@ | python -B -X utf8 - $sessionId
```

不调用新工具的续聊会增加用户与助手消息，原有工具结果保留。授权按当前命令决定，不继承之前的 `--auto-read`；需要重新运行工具时，仍必须重新提出请求并按本次策略确认。跨项目或不存在的会话 ID，在读取密钥或调用模型前拒绝。

数据库通过 `PRAGMA user_version = 1` 标记格式，不自动覆盖不认识的数据库或迁移其他版本。消息快照、当前工具记录和会话信息一起提交，保存失败会回滚并明确显示 JSONL 位置；旧版本已被其他运行更新时，拒绝覆盖。当前是单任务 CLI，勿并发修改同一会话。

本课先在命令行任务结束时保存。拒绝、异常和取消等已结束运行也会留下可查询记录，但仅正常结束和请求次数上限两种状态允许续聊。强制关闭进程或断电可能只留下部分 JSONL，尚未完成的这次运行不会自动进入 SQLite；逐步持久化、任务状态和异常恢复留到后续课程。SQLite 保存的是当时的会话，不保证文件后来没有变化，需要验证当前项目时仍应调用工具。

此前的旧日志不会自动批量导入 SQLite，需要时继续使用下方 `--resume`。

## 任务状态与事件

第 30 课把一次命令行任务的状态与实际事件关联起来。同一个 Session 可以有多次任务；一次任务可以请求模型多次。模型回复 `tool_calls` 时，当前任务仍在运行，并没有结束。

| 状态 | 含义 |
| --- | --- |
| `running` | 正在请求模型、处理回复或执行已经允许的工具 |
| `waiting_approval` | 操作准备完成，等待用户输入确认；尚未执行工具 |
| `completed` | 模型正常回答，本次循环以 `stop` 结束 |
| `failed` | 请求或参数处理异常，或模型输出被截断等异常结束 |
| `cancelled` | 用户拒绝工具，或通过 Ctrl+C 中断本次任务 |
| `paused` | 达到模型请求次数上限，本次停止；仍可按原规则恢复历史 |

`completed` 表示循环正常结束，业务需求是否满足仍要核对实际输出和验收测试。单个工具返回 `ok: false` 时，任务通常仍是 `running`，模型可以根据错误继续调整。

新日志在原有事件中增加 `task_state`。需要人工确认时，先写入并刷新 `tool_waiting`，再等待输入；确认后回到 `running`，拒绝后变为 `cancelled`。自动允许的只读工具不进入等待状态。`run_end` 继续保留原有 `reason`，终止状态与后续操作的检查发生在请求模型或执行工具之前。SQLite 格式不变，任务状态保存在每次独立的 JSONL 中。

在 learn-agent 根目录验证，先运行下面的任务，出现运行 `main.py` 的确认时输入 `n`：

```powershell
$project = (Resolve-Path -LiteralPath .\workspace\demo_project).Path
$task = "请只调用 run_python，path 参数必须是 main.py，表示当前 workspace 根目录下的脚本。不要添加 demo_project/ 前缀。不要读取、创建或修改文件；根据真实运行结果简要回答。"
python -B -X utf8 tool_call.py --workspace $project $task
$deniedLog = Get-ChildItem -LiteralPath .\logs -Filter *.jsonl -File |
    Sort-Object LastWriteTime | Select-Object -Last 1
python -B -X utf8 tool_call.py --status $deniedLog.FullName
```

预期状态依次为 `running → waiting_approval → cancelled`，结束原因 `user_denied`；没有这次脚本的执行结果，没有后续模型请求。随后在同一终端重新运行这个任务，确认时输入 `y`：

```powershell
python -B -X utf8 tool_call.py --workspace $project $task
$completedLog = Get-ChildItem -LiteralPath .\logs -Filter *.jsonl -File |
    Sort-Object LastWriteTime | Select-Object -Last 1
python -B -X utf8 tool_call.py --status $completedLog.FullName
```

预期 `running → waiting_approval → running → completed`，实际输出总和 30、平均值 10.0、最小值 5、最大值 15，停止原因与 `run_end.reason` 均为 `stop`，最后一条事件中的 `task_state` 为 `completed`。两次任务分别保存日志；查看状态时不调用模型、工具或申请授权，也不改写日志和数据库。

也可以在等待确认时按 Ctrl+C：任务状态为 `cancelled`，结束原因 `cancelled`，不启动工具。`--status` 支持第 30 课之前的完整日志，但旧日志没有记录等待开始的时刻，无法补出这段过程。对有完整 JSON 行却没有 `run_end` 的日志，只显示最后保存的状态并提示结束未知，不能据此确认进程仍在运行；损坏的 JSON、事件顺序或状态不一致时拒绝推断。强制退出后的恢复和选择性重试留到第 31 课处理。

## 会话恢复

`--resume` 接受以 `run_end.reason: stop` 正常结束，或以 `max_model_calls` 达到请求次数上限而停止的 JSONL 日志，同时必须提供新的问题或任务。以下示例适用于最近一次运行正常结束的情况：

```powershell
$previousRun = Get-ChildItem -LiteralPath .\logs -Filter *.jsonl -File |
    Sort-Object LastWriteTime | Select-Object -Last 1

# 先查看恢复后的请求，不调用模型或创建新日志
python tool_call.py --resume $previousRun.FullName --preview "根据上次会话，告诉我验收检查了哪四个输入，主程序输出了什么。不调用工具。"

# 退出过程序也可以接上这份历史
python tool_call.py --resume $previousRun.FullName "根据上次会话，告诉我验收检查了哪四个输入，主程序输出了什么。不调用工具。"
```

对于正常结束的日志，`load_history()` 从最后一次模型请求取出完整消息历史，再补上最后一条正常结束的助手回答。新请求的消息顺序为：当前系统指令 → 上次的用户、助手和工具消息 → 上次最终回答 → 本次新问题。模型获得上下文，是因为程序把历史重新发送给 API。

达到请求次数上限时，程序在执行最后一批工具前就已停止。恢复这类日志会保留已完成的历史，并为最后一批工具请求分别补充状态消息：

```json
{
  "ok": false,
  "tool": "run_python",
  "source": "resume",
  "error": {
    "type": "NotExecuted",
    "message": "上次运行达到模型请求次数上限，该请求未执行；如仍需要，请重新提出工具调用。"
  }
}
```

`NotExecuted` 由恢复代码生成，表示上次没有执行该操作，不能当作代码运行失败或成功的证据。每条状态消息仍用 `tool_call_id` 对应原请求，保持消息历史完整。恢复时不会执行这批请求；模型如需继续，必须重新提出工具请求，并按本次权限策略确认。新运行从第 1 次请求重新计数，仍受本次 8 次请求上限约束。

已有超限日志时，将下面的路径替换为自己的日志路径，可先预览再继续：

```powershell
$stoppedLog = ".\logs\run-20261017-195639-335886.jsonl"
$task = "继续完成上次因请求次数上限停止的任务。请重新运行 main.py 核对实际输出，再根据已有验收记录和本次执行结果简要总结。"
python tool_call.py --resume $stoppedLog --preview $task
python tool_call.py --resume $stoppedLog $task
```

恢复只加载消息，不会执行历史中的工具调用。模型、输出预算、API Key 和权限策略均按本次运行决定；上次的 `--auto-read` 不会自动继承，写入和运行仍分别确认。旧日志保持原样，新任务创建新的日志，`run_start.resumed_from` 记录来源路径；普通新任务中该字段为 `null`。新的日志已经包含继承后的历史，因此也可以再次用于恢复。

这一版支持正常结束和请求次数上限两种恢复。日志缺失结束记录、以拒绝或输出截断等其他原因结束、消息中的工具请求与结果不对应时，会在读取密钥或请求 API 前停止。标为超限停止的最后一轮若已有工具执行记录，也会拒绝按未执行状态恢复。恢复后的工具结果描述过去的执行状态；需要验证当前文件时，应重新运行相应工具。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试模拟服务响应，不调用真实 API，检查多轮消息历史、清空历史、退出与预览行为，以及多工具分发、文件列表过滤、连续工具结果对应关系、读取错误反馈、失败后改读其他文件、调用次数上限、直接回答、拒绝执行、异常结束和非法请求。写入测试使用临时目录，覆盖完整五次请求流程、中文报告保存、覆盖确认、拒绝时保留原文件、非法写入和写入错误反馈。

日志测试也使用临时目录，检查 JSONL 事件顺序、工具结果与下一轮消息的一致性、密钥不进入日志、不同运行分别保存，以及拒绝、错误、中断、达到上限和预览等分支。

权限测试检查开启自动读取后完整五次请求流程只询问一次写入，写入拒绝保留旧文件，非法请求仍被拦截，读取失败仍反馈给模型，以及预览不执行工具或创建日志。

同轮多工具测试复现先列文件、再在一个回复里读取两份资料的场景，检查助手消息只追加一次、每个结果与调用 ID 对应、每次写入分别确认、拒绝后停止剩余调用，以及错误反馈、整组参数检查、重复调用 ID 和请求次数上限。

`tests/test_run_python.py` 在临时目录实际启动 Python，检查写入后运行、中文输出、工作目录、API Key 环境变量不继承、语法与运行错误、超时终止、缺失脚本、非法参数、拒绝执行和预览行为。完整修复测试覆盖写入错误代码、实际报错、覆盖修正、再次运行的五次请求流程，并核对历史、日志和逐次确认。模型响应仍使用模拟数据，测试不调用真实 API。

验收流程测试还会确认错误算法可以正常退出但得到 `4950`，再通过固定测试实际发现错误并在修复后通过四个用例；随后再次运行主程序得到 `5050`，第六次模型请求正常结束。测试核对错误反馈、日志和逐次确认，并检查验收脚本内容未变。权限测试同时覆盖拒绝运行验收脚本，以及通过文件工具改写验收脚本时在确认前拦截。

新一轮验证测试会在临时目录预先放入正确脚本，再经过列文件、读取两份源代码、运行验收和主程序，检查工具之间的数据一致性、列表范围标记及执行确认。模型回复使用模拟数据，这些测试不保证真实模型的最终文字判断一定正确。

输出截断测试覆盖验收和主程序已成功运行、最终回复却以 `length` 结束的情况：检查两次请求均使用 2048 个输出 token 的上限，已有执行结果保留在日志中，截断回复中的写入请求不会执行，源文件保持原样，结束原因仍为 `abnormal_finish`。

`tests/test_resume.py` 检查最终助手回答和工具结果一同恢复、当前设置与凭据生效、旧工具不重放、旧日志不改写、新日志记录来源、预览无网络和新日志、损坏或未完成日志的拒绝，以及自动读取权限不继承。

超限恢复测试还检查未执行批次逐个获得 `NotExecuted` 状态，预览不会执行工具；模型重新请求后只执行新调用并再次确认，在临时目录实际运行 Python 得到 `5050`，此前的写入请求不重放，来源日志与脚本均保持原样。

`tests/test_workspace.py` 检查逐层列目录、子目录源码读取、点号开头的项过滤、绝对路径与目录穿越拦截、模拟链接解析后越界的拒绝，以及完整浏览流程、授权前参数检查和文件系统错误反馈。测试不调用真实 API。

多文件修改测试在临时目录保留初始源码并实际运行：先确认新增需求未通过，模拟模型修改业务模块，再执行四组固定测试和项目入口，核对导入、输出、日志和逐次确认。另检查脚本相对目录读取资料、只读验收脚本拦截、子目录操作拒绝、写入父目录不存在的错误反馈，以及写入和运行对越界路径的拦截。原始练习源码不在测试过程中修改。

`tests/test_edit_file.py` 检查差异展示先于确认、精确替换和删除、字面匹配、重复与重叠匹配的拒绝、中文与 LF/CRLF 换行保留、无末尾换行的差异标记，以及拒绝确认和确认期间文件变化时不覆盖。完整流程在临时项目中读取、编辑、实际运行 Python 并检查日志正常结束；还覆盖冲突后重新定位、只读和越界拦截、文件缺失或编码错误、同批次连续编辑与无副作用预览，不调用真实 API。

`tests/test_search_text.py` 检查字面和中文搜索、路径与行号、结果上限和准确的截断标记、长行摘要、固定目录过滤、二进制/编码/文件大小限制、模拟链接解析和访问错误计数。完整只读任务验证搜索截断后缩小范围、读取源码、自动读取授权和正常结束日志；另检查默认确认与拒绝、非法参数、无匹配、目录错误和预览无副作用，模型响应使用模拟数据。

拆分前原有 109 项测试全部通过；拆分后保留原有场景与断言，将导入和模拟目标调整到对应模块，并新增 `tests/test_registry.py` 的 4 项测试，共 113 项通过。新测试验证增加工具无需修改循环、预览和执行使用同一注册表、重复注册不能覆盖原工具、新工具默认仍需确认，以及自定义参数检查失败时整批工具均不执行。测试不调用真实 API。

第 26 课新增 `tests/test_project_directory.py` 的 12 项测试，共 125 项通过。在带中文和空格的临时项目中，验证绝对与相对目录选择、默认目录稳定、预览无副作用、非法目录在联网前拒绝、六个工具遵循新根目录、写入与执行确认、错误后目录恢复、固定课程文件保护与同名普通文件可写、越界路径和模拟越界链接的拦截，以及同项目恢复、跨项目拒绝、旧日志兼容与无效目录记录。Python 脚本在临时项目中实际执行，模型响应使用模拟数据。

第 27 课新增 `tests/test_commands.py` 的 19 项测试，共 144 项。测试在临时项目中实际执行 unittest 的成功与失败、普通命令的非零退出、中文输出、参数边界、stdin 关闭、密钥环境变量移除、指定目录、缺失程序、超时与输出超限。Windows 测试实际创建等待中的父子进程，超时后确认子进程已退出；另覆盖中断清理、清理失败标记、参数校验、拒绝执行、整批预检查、预览与完整日志和消息历史。模型调用使用模拟数据，不消耗真实 API。

第 28 课新增 `tests/test_checkpoints.py` 的 25 项测试，共 169 项。覆盖精确字节、中文与 CRLF、二进制原文、多次写入、新建文件删除和无变化写入；后续修改、同内容重存、删除、路径重定向、只读固定文件、损坏元数据和确认后的变化都会拒绝危险恢复。实际临时 Git 仓库验证已暂存与未暂存内容、索引字节和用户未跟踪文件均保留。另覆盖拒绝与重复撤回、运行中禁止撤回、中断、存储失败及对应的 error 结束日志、部分写入/撤回失败、命令变更不归属，以及完整 CLI 修改、实际运行、查看、拒绝和确认撤回流程。模型响应使用模拟数据。

第 29 课新增 `tests/test_sessions.py` 的 22 项测试，累计 191 项通过。测试使用真实临时 SQLite，覆盖三种数据结构、中文内容、数据库版本、同 ID 续聊且不重复消息、历史工具不重放、授权不继承、只读预览与列表、跨项目和未知 ID 拒绝、实际子进程输出、工具失败与拒绝、超限未执行状态、异常与取消记录、旧 JSONL 兼容、事务回滚和过期写入拒绝。另在独立 Python 进程读取数据库，并移除临时 JSONL 验证恢复不依赖原日志。另验证空字符串和纯空白会话 ID 在读取凭据、调用模型和创建存储前被拒绝。模型响应使用模拟数据；跨进程恢复与同 ID 真实续聊已验证成功。

第 30 课新增 `tests/test_tasks.py` 的 15 项测试，累计 206 项通过。覆盖状态变化、确认前日志已刷新、实际子进程输出、拒绝后整批停止、自动读取不假装等待、工具失败后继续、请求次数超限、输出截断、请求异常、中断、结束后拒绝新的模型请求、只读状态查询、旧日志兼容和不完整日志不推断存活。状态回放也检查损坏记录、结束后的多余事件和状态不一致；原有 SQLite 会话与消息恢复保持兼容。模型响应使用模拟数据，真实模型验收待完成。

第 30 课验证中修正了系统指令及 read_file、write_file、run_python 说明中固定的 demo_project/ 示例。现在明确区分根目录脚本 main.py 与 src 子目录文件 src/stats.py，不随项目根目录名称自动添加前缀。新增 2 项回归测试，覆盖默认根目录、选定子项目和其他项目的预览，以及真实 Python 执行：错误的重复前缀返回 FileNotFoundError，改用 main.py 后输出 5050；不通过自动删前缀掩盖错误。当前共 208 项测试通过。

## 许可证

[MIT](LICENSE)。
