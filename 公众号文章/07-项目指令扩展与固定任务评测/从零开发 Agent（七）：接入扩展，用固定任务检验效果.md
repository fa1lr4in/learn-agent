上一篇，我给自己开发的 Agent 加上了会话保存、取消恢复、流式输出和上下文压缩。它已经能持续处理任务，也能在中断后接着工作。

继续用它检查 Python 项目时，我开始遇到另一类问题：每次都要重复解释项目约定；模型说“检查完成”，却不容易看出检查到了哪一步；想增加一个源码分析工具，还要分清工作指南和实际执行能力。

这一阶段，我补上了项目规则、带证据的任务计划、Skill、本地插件和一个只读子 Agent，然后加入固定任务评测。前半段解决“怎样组织工作”，后半段回答“怎样判断工作真的完成了”。

本文沿着一个很小的求和项目展开。先让 Agent 按约定检查代码，再接入源码结构检查工具和只读子任务，最后用三道固定题比较两种请求预算，核对结果、耗时和 token 消耗。

项目地址：[fa1lr4in/learn-agent](https://github.com/fa1lr4in/learn-agent)。本文的代码摘录均来自下面固定版本；部分片段只保留了与说明直接相关的几行。

## 1. 实验环境与复现版本

| 项目 | 本次环境 |
| --- | --- |
| 操作系统 | Windows 11，64 位 |
| 终端 | Windows PowerShell，命令按 5.1 核对 |
| Python | 3.13.9 |
| Git | 2.51.0.windows.1 |
| 模型服务 | DeepSeek 官方 API |
| 真实运行使用的模型 | `deepseek-flash` |
| 可选扩展依赖 | `PyYAML==6.0.3`、`jsonschema==4.26.0`、`mcp==2.3.0`，以仓库的 `requirements.txt` 为准 |
| 本文复现版本 | `90c808459d9c9285401360865aa7864af0ba5c8d` |

这个版本包含本阶段实现和已经核验的评测记录。下面重新克隆一个独立副本，演示项目、日志和评测结果都放在这个副本里，原开发目录可以继续保留。

各节命令在同一个 PowerShell 窗口按顺序执行。本文使用的 `$reproDir`、`$agentPython` 和报告路径变量均由前面的命令实际生成，不需要预先知道会话 ID 或日志文件名。

```powershell
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$reproDir = Join-Path $env:TEMP ('learn-agent-07-' + [guid]::NewGuid().ToString('N'))

git -c http.sslBackend=openssl -c http.version=HTTP/1.1 clone https://github.com/fa1lr4in/learn-agent.git "$reproDir"
if ($LASTEXITCODE -ne 0) { throw '克隆失败，请检查网络。' }
Set-Location -LiteralPath $reproDir
git checkout --detach 90c808459d9c9285401360865aa7864af0ba5c8d
if ($LASTEXITCODE -ne 0) { throw '未能切换到本文版本。' }

python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败。' }
$agentPython = (Resolve-Path -LiteralPath '.\.venv\Scripts\python.exe').Path
& $agentPython -m pip install -r .\requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }

$env:DEEPSEEK_MODEL = 'deepseek-flash'
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}

Get-Location
git rev-parse HEAD
$PSVersionTable.PSVersion
& $agentPython --version
git --version
& $agentPython -B -X utf8 -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { throw '自动化测试未通过，请先核对环境。' }
```

完整依赖安装后，这个固定版本应有 **427 项自动化测试通过**。测试中的模型响应使用模拟数据，文件操作、部分子进程、SQLite 和固定验收会实际执行。这一项检查不消耗模型 API。

为核对复现步骤，我还在独立副本中按顺序执行了本文全部 PowerShell 命令：模型回复和确认输入使用离线模拟，文件工具、源码检查、测试、会话保存与固定验收实际运行。后文的用量表来自此前已核验的真实 DeepSeek 调用，两类数据分别记录。

正式运行任务时，程序从 `DEEPSEEK_API_KEY` 读取密钥；已经配置过用户环境变量的电脑可以直接使用。没有配置时，程序会提示隐式输入，不回显密钥。后面的正式任务和两组评测都会产生真实 API 用量；`--preview`、日志读取和本地验收不会调用模型。

> 【截图 1】终端中的固定提交、Python/PowerShell 版本，以及 `Ran 427 tests`、`OK`。依赖安装输出可以略去，密钥无需展示。

## 2. 把项目约定和任务进度交代清楚

先准备三个独立的小项目。它们都来自固定评测题的求和项目：两个使用正确实现，分别演示任务计划与插件；另一个故意保留上界错误，供子 Agent 做静态检查。

```powershell
$demoRoot = Join-Path $reproDir 'logs\article-07'
$planProject = Join-Path $demoRoot 'plan-project'
$extensionProject = Join-Path $demoRoot 'extension-project'
$childProject = Join-Path $demoRoot 'child-project'

@'
import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
suite = json.loads(Path('evals/cases.json').read_text(encoding='utf-8'))
case = next(item for item in suite['cases'] if item['id'] == 'fix_sum')
for name in ('plan-project', 'extension-project', 'child-project'):
    root = base / name
    root.mkdir(parents=True, exist_ok=False)
    for path, text in case['files'].items():
        if name != 'child-project' and path == 'main.py':
            text = text.replace('range(1, n)', 'range(1, n + 1)')
        (root / path).write_text(text, encoding='utf-8')
(base / 'plan-project' / 'AGENTS.md').write_text(
    '只核对 main.py 与 test_main.py，不创建或修改文件。\n'
    '验收使用 python -m unittest -v test_main.py，然后运行 main.py。\n'
    '项目默认使用英文报告。\n', encoding='utf-8')
print('三个项目已准备：', base)
'@ | & $agentPython -B -X utf8 - $demoRoot
if ($LASTEXITCODE -ne 0) { throw '演示项目准备失败。' }

Copy-Item -LiteralPath .\extensions -Destination $extensionProject -Recurse
```

这段准备只执行一次。想从头重做时，重新执行第 1 节，创建新的独立副本。

`AGENTS.md` 保存项目的默认约定。本次增加 `--instructions AGENTS.md` 显式读取它，模型请求里会同时包含规则来源、原文和当前任务。这里不会自动扫描父目录，也不会因为文件名叫 `AGENTS.md` 就赋予它更高权限。

项目默认用英文报告，下面的当前任务明确要求中文。这是一个容易观察的优先级实验：规则提供背景，当前用户要求可以覆盖报告语言，文件内容不能替用户批准工具执行。

```powershell
$planTask = @'
本次用中文报告，覆盖项目默认的英文报告约定。只检查现有代码，不创建或修改任何文件。
先用 update_plan 建立三项计划：读取 main.py 与 test_main.py、运行验收、运行入口。
实际读取两份源码，然后用 run_command 执行 python -m unittest -v test_main.py，用 run_python 运行 main.py。
每项完成时，evidence 原样复制本次工具结果中的 evidence_id，或 available_evidence 中的编号，不要自行编造。
最终回答前将三项计划全部更新为 completed，根据真实退出码和输出简短报告。
'@

& $agentPython -B -X utf8 tool_call.py --workspace $planProject --instructions AGENTS.md --plan --auto-read --deny-tool edit_file --deny-tool write_file --preview $planTask
if ($LASTEXITCODE -ne 0) { throw '计划任务预览失败。' }
```

预览中应有项目规则、当前中文要求，以及新增的 `update_plan` 工具。预览会读取显式选择的规则文件，但不会联网或执行工具。

> 【截图 2】预览里的项目规则和当前任务，以及 `update_plan` 工具说明。保留“英文默认约定”和“本次中文报告”的对应关系。

接着正式运行。读取自动允许；测试命令和入口运行分别需要检查提示并输入 `y`。本次项目已经正确，预期四项测试通过、入口输出 `5050`。

```powershell
& $agentPython -B -X utf8 tool_call.py --workspace $planProject --instructions AGENTS.md --plan --auto-read --deny-tool edit_file --deny-tool write_file --stream --max-model-calls 16 $planTask
if ($LASTEXITCODE -ne 0) { throw '计划任务异常结束，请检查输出。' }

$planLog = (Get-ChildItem -LiteralPath .\logs -Filter 'run-*.jsonl' -File |
    Where-Object { $_.Name -notlike '*.child-*' } |
    Sort-Object LastWriteTime | Select-Object -Last 1).FullName
$planEvents = @(Get-Content -LiteralPath $planLog -Encoding UTF8 |
    ForEach-Object { $_ | ConvertFrom-Json })
if ($planEvents[0].workspace -ne $planProject) { throw '日志不属于当前计划项目。' }

($planEvents | Where-Object { $_.event -eq 'plan_updated' } |
    Select-Object -Last 1).steps |
    Select-Object step, status, @{Name='evidence'; Expression={ $_.evidence -join ', ' }} |
    Format-Table -Wrap -AutoSize
& $agentPython -B -X utf8 tool_call.py --status $planLog
```

> 【截图 3】三项计划均为 `completed`，每项带有实际工具调用编号；下方状态为 `completed / stop`。运行过程中的测试 `OK` 和入口 `5050` 也可以一并保留。

计划工具只更新状态，不会替程序执行测试。它的关键约束在 `learn_agent/planning.py`：

```python
expected = item.get("expected_outcome", "success")
if not evidence or any(expected not in self.supported_outcomes(call_id) for call_id in evidence):
    raise PlanError(...)
```

这段代码位于已完成步骤的检查分支，`evidence` 是工具调用 ID 列表。没有返回结果、编号不存在、执行失败，或者源码改过后仍引用旧运行结果，都不能直接支持“成功完成”。

开发时我曾遇到一个实际问题：程序已经修好并通过验收，模型却反复无法更新计划。原因是完成状态要求引用工具编号，而给模型看的结果没有提供便于复制的编号。后来补上 `evidence_id` 回显和 `available_evidence` 清单，模型才顺利完成更新。

这个问题让我意识到，约束模型的同时，也必须把满足约束所需的信息交给它。计划仍然需要人核对：程序能检查编号和结果状态，无法理解所有步骤描述的语义，成功读取源码也不能自动等同于测试通过。

## 3. Skill 给工作方法，插件给可执行工具

如果每次检查项目，都要重复描述“先看结构，再读源码，最后运行验收”，这套方法可以单独保存。示例目录如下：

```text
extensions/python-check/
├── SKILL.md
├── plugin.json
└── plugin.py
```

`SKILL.md` 告诉模型怎样组织工作：先核对文件，区分静态分析与实际运行，最后按“源码结构”“验收结果”“未完成事项”报告。加载它会增加模型可见的说明文本。

`plugin.json` 和 `plugin.py` 则注册一个实际工具：`plugin_python_check_inspect_python`。模型给出路径，Python 代码读取文件、解析 AST，返回函数和类的名称、类型、行号。

两者可以独立启用。只加载 Skill，程序不会凭空拥有新工具；加载插件，也不会自动附加 Skill 的工作指南。

插件核心代码来自 `extensions/python-check/plugin.py`，以下省略了前面的参数、路径与大小检查：

```python
tree = ast.parse(raw.decode("utf-8-sig"), filename=str(target))
definitions = [{"name": node.name, "kind": type(node).__name__, "line": node.lineno}
               for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
definitions.sort(key=lambda item: item["line"])
```

这段代码解析目标源码，不运行目标源码。结果中的 `source_executed: false` 很重要：找到 `sum_to()` 只能说明代码结构，不能证明它计算正确。

前面已经把扩展复制到独立的 `$extensionProject`。下面先预览，再运行同一个检查任务。两个插件调用、测试命令和入口运行都需要分别输入 `y`；内置读取仍自动允许。

```powershell
$extensionTask = @'
按本次 Skill 核对 main.py 与 test_main.py，不创建或修改文件。
先分别使用 plugin_python_check_inspect_python 检查两份文件的结构，必要时读取正文。
再用 run_command 执行 python -m unittest -v test_main.py，用 run_python 运行 main.py。
最后按 Skill 的三项格式简短报告，明确静态检查不等于测试通过。
'@

& $agentPython -B -X utf8 tool_call.py --workspace $extensionProject --skill extensions/python-check --plugin extensions/python-check --auto-read --preview $extensionTask
if ($LASTEXITCODE -ne 0) { throw '扩展预览失败。' }
& $agentPython -B -X utf8 tool_call.py --workspace $extensionProject --skill extensions/python-check --plugin extensions/python-check --auto-read --stream --max-model-calls 12 $extensionTask
if ($LASTEXITCODE -ne 0) { throw '扩展任务异常结束。' }

$extensionLog = (Get-ChildItem -LiteralPath .\logs -Filter 'run-*.jsonl' -File |
    Where-Object { $_.Name -notlike '*.child-*' } |
    Sort-Object LastWriteTime | Select-Object -Last 1).FullName
$extensionEvents = @(Get-Content -LiteralPath $extensionLog -Encoding UTF8 |
    ForEach-Object { $_ | ConvertFrom-Json })
if ($extensionEvents[0].workspace -ne $extensionProject) { throw '日志不属于当前扩展项目。' }

$extensionEvents | Where-Object { $_.event -eq 'tool_result' -and $_.tool -like 'plugin_*' } |
    ForEach-Object { $_.result.value } | ConvertTo-Json -Depth 8
$extensionEvents | Where-Object { $_.event -eq 'tool_approval' } |
    Select-Object tool, approved, source | Format-Table -AutoSize
& $agentPython -B -X utf8 tool_call.py --status $extensionLog
```

预期静态结果中能看到 `sum_to`、`SumTests` 和四个测试方法，`source_executed` 为 `false`。随后实际运行四项测试，成功报告写入 `stderr`，入口的 `stdout` 为 `5050`。最终回答按 Skill 的格式组织，并以 `completed / stop` 结束。

> 【截图 4】插件返回的函数/类结构、`source_executed: false`，以及授权来源表。插件和运行工具应为 `user`，内置读取才可能是 `auto_read`；可同时截取上方真实测试和最终三项报告。

当前插件是宿主进程内执行的受信任 Python 代码，插件格式也由本项目定义，没有实现操作系统隔离。示例通过现有路径检查限制读取范围；人工确认仍然保留。MCP 的本地接口示例也已接入项目，但真实模型联合验收暂缓，本文没有把它列为已完成的演示。

## 4. 把一个明确的阅读问题交给子 Agent

接下来，我把“阅读代码、指出问题”交给一个独立的子任务。

这里的子 Agent 仍然使用同一个 DeepSeek 模型和同一个项目目录。独立的是消息上下文和工具表：它只拿到委派描述、显式项目规则，以及三个内置只读工具，不继承父任务的完整聊天历史、插件和 Skill。

`learn_agent/subagents.py` 中的边界直接写在实际工具选择里：

```python
READ_TOOLS = ("list_files", "search_text", "read_file")
CHILD_MAX_CALLS = 4

payload = provider.build_request(
    [{"role": "system", "content": system}, {"role": "user", "content": task}],
    tools=[tools.TOOL_REGISTRY[name].schema for name in READ_TOOLS],
    stream=stream, max_tokens=1024,
)
```

模型在子任务里没有写文件、运行代码或继续委派的工具。程序还会在执行前检查实际请求，子任务沿用本次禁用策略，父子共用模型次数和 token 预算。当前实现一次串行委派一项任务，父任务等待返回。

下面使用准备好的 `$childProject`。它仍保留 `range(1, n)`，因此会漏掉最大的 `n`。正式运行时，委派本身需要输入 `y`；由于开启 `--auto-read`，子任务的内置读取自动允许。

```powershell
$childTask = @'
请调用 delegate_readonly，只委派以下检查：实际读取 main.py 与 test_main.py，
从源码判断 sum_to 是否满足 0、1、10、100 四组断言，指出问题所在行。
子任务只允许读取这两个文件，不修改、不运行，也不继续委派。
拿到结果后，你根据实际读取来源简短汇总；不要补充运行或修改，明确本次没有执行测试。
'@

& $agentPython -B -X utf8 tool_call.py --workspace $childProject --subagent --auto-read --preview $childTask
if ($LASTEXITCODE -ne 0) { throw '子任务预览失败。' }
& $agentPython -B -X utf8 tool_call.py --workspace $childProject --subagent --auto-read --stream --max-model-calls 12 $childTask
if ($LASTEXITCODE -ne 0) { throw '子任务异常结束。' }

$childParentLog = (Get-ChildItem -LiteralPath .\logs -Filter 'run-*.jsonl' -File |
    Where-Object { $_.Name -notlike '*.child-*' } |
    Sort-Object LastWriteTime | Select-Object -Last 1).FullName
$childParentEvents = @(Get-Content -LiteralPath $childParentLog -Encoding UTF8 |
    ForEach-Object { $_ | ConvertFrom-Json })
if ($childParentEvents[0].workspace -ne $childProject) { throw '日志不属于当前子任务项目。' }
$delegation = $childParentEvents |
    Where-Object { $_.event -eq 'tool_result' -and $_.tool -eq 'delegate_readonly' } |
    Select-Object -Last 1
if ($null -eq $delegation) { throw '本次没有委派结果，请先检查任务日志。' }
$childResult = $delegation.result

$childResult | Select-Object ok, reason, code_executed, model_calls, child_log | Format-List
$childResult.evidence | Select-Object tool, path, content_utf8_sha256 | Format-Table -Wrap -AutoSize
$childParentEvents[-1].summary | ConvertTo-Json -Depth 6
```

预期返回两份文件的读取来源和子任务日志路径，`code_executed: false`。从源码推算，`sum_to(0)` 为 0，另外三个输入会得到 0、45、4950；这些是静态分析结论，本次并没有运行测试。

> 【截图 5】子任务的两份实际读取来源、`code_executed: false`，以及父任务汇总中的 `task_model_calls`、`subagent_model_calls` 和总用量。

子任务用量已经进入父任务总用量，核算时不能再加一次。`report` 是模型整理的文字，`evidence` 则由程序从实际读取事件提取，两者分别保留。先前也单独验证过在子读取确认处按 `Ctrl+C`：父子任务都会取消，不再继续读取或请求模型。

## 5. 给“完成”设置运行前就确定的条件

到这里，Agent 可以按项目约定组织任务，也能接入工具和委派阅读。但演示成功一次，仍然难以回答以后每次改动是否有效。

于是我增加了一个很小的固定评测入口 `eval.py`。题目保存在 `evals/cases.json`，验收逻辑在 `evals/grade.py`，调度、判定与报告在 `learn_agent/evaluation.py`。

| 固定题目 | 要求 | 必须满足的条件 |
| --- | --- | --- |
| `read_facts` | 读取资料，返回项目名和实验编号 | 有实际读取记录；返回约定 JSON；没有改文件或执行代码 |
| `fix_sum` | 修复求和上界错误 | 只改 `main.py`；实际运行测试和入口；外部固定验收通过 |
| `extend_stats` | 增加最小值、最大值和空列表处理 | 只改 `src/stats.py`；实际运行测试和入口；外部固定验收通过 |

每题、每次重复都会从原始文件创建新的 workspace，并使用新会话。这样上一题改好的代码、上一轮的对话历史，都不会直接带进下一次尝试。

创建副本的代码很直接：

```python
workspace = folder / f"{case['id']}-{number}" / "workspace"
workspace.mkdir(parents=True)
for name, text in case["files"].items():
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
before = snapshot(workspace)
```

`snapshot()` 保存文件内容的 SHA-256。任务结束后，再比较文件集合和不允许修改的文件，检查有没有改动测试、额外创建文件或误改入口。

仅凭模型说“测试通过”还不够。编码题正常结束后，评测器会在另一个子进程中执行仓库外置的固定验收源码，直接加载改后的函数，检查四组固定输入。模型不能通过改写题目里的测试文件来改变这套条件。

例如求和题的固定条件是：

```python
function = runpy.run_path(str(root / "main.py"))["sum_to"]
examples = [(0, 0), (1, 1), (10, 55), (100, 5050)]
```

最终通过需要多个条件同时成立。`stop` 只说明模型完整结束回复；源码、实际执行、修改范围和固定验收也必须符合要求。

验收程序最长运行 10 秒，检查退出码、超时、输出截断和完成标记。它属于普通项目功能评测，没有构建对抗恶意程序的隔离环境。

另一个实现变化是 `learn_agent/providers.py`：循环通过统一接口构造请求、接收 `ModelResponse`，具体协议交给 `DeepSeekProvider`。目前实际接入的仍然是 DeepSeek；接口拆分便于单独测试协议和执行循环，不代表已经验证了多家真实服务。

## 6. 先让请求预算不足，确认失败会被保留下来

下面开始两组评测。它们都使用默认 Agent 配置，没有开启项目计划、Skill、插件或子 Agent；本次只比较**每道题最多请求模型 1 次与 8 次**。这组数据不能用来证明前面的扩展提升了成功率。

先运行低预算组。在本文版本中，第一次回复若提出工具调用，到达请求上限后就会暂停，不执行这一轮工具。三道题都要求真实工具证据，因此这组应为零题通过。

```powershell
& $agentPython -B -X utf8 eval.py --label limited-1 --max-model-calls 1
if ($LASTEXITCODE -ne 0) { throw '低预算评测异常结束，请检查输出和已保存报告。' }

$limitedReport = (Get-ChildItem -Path '.\logs\evals\eval-*\report.json' -File |
    Sort-Object LastWriteTime | Select-Object -Last 1).FullName
$limited = Get-Content -LiteralPath $limitedReport -Raw -Encoding UTF8 | ConvertFrom-Json
if ($limited.label -ne 'limited-1') { throw '选中的报告不是低预算组。' }
$limited.runs | Select-Object task_id, passed, reason, model_calls | Format-Table -AutoSize
$limited.summary | ConvertTo-Json -Depth 5
```

本次真实运行三题均为 `max_model_calls`，没有工具执行，结果为 `0/3`。失败没有被跳过，已经消耗的 token 也保留在汇总里。

报告中的 `complete: true` 表示“整组题目都已尝试”，与全部通过是不同指标。即使三题全部失败，整组评测也可以正常完成；如果中途取消或拒绝，后续题目不再启动，报告会保留部分结果，并标记为未完成。

> 【截图 6】低预算组三题的失败原因与汇总：`passed: 0`、`failed: 3`、`complete: true`，以及实际用量。再次运行的 token 数可能不同，以当前报告为准。

## 7. 再给足预算，检查实际修改与固定验收

继续使用同一终端中的 `$limitedReport`。正常组每题最多请求 8 次，通过 `--compare` 读取刚生成的低预算报告。遇到修改或运行确认时，检查内容后输入 `y`；读取仍自动允许。

```powershell
& $agentPython -B -X utf8 eval.py --label normal-8 --max-model-calls 8 --compare $limitedReport
if ($LASTEXITCODE -ne 0) { throw '正常组评测异常结束，请检查输出和已保存报告。' }

$normalReport = (Get-ChildItem -Path '.\logs\evals\eval-*\report.json' -File |
    Sort-Object LastWriteTime | Select-Object -Last 1).FullName
$normal = Get-Content -LiteralPath $normalReport -Raw -Encoding UTF8 | ConvertFrom-Json
if ($normal.label -ne 'normal-8') { throw '选中的报告不是正常组。' }

$normal.runs | Select-Object task_id, passed, reason, model_calls, agent_seconds | Format-Table -AutoSize
$normal.summary | ConvertTo-Json -Depth 5

$reportIndex = Join-Path $reproDir 'logs\article-07-reports.json'
@{ limited = $limitedReport; normal = $normalReport } |
    ConvertTo-Json | Set-Content -LiteralPath $reportIndex -Encoding UTF8
Write-Host "两组报告路径已保存：$reportIndex"
```

我在 2026 年 10 月 20 日完成并核验的真实结果如下。该次使用的功能源码提交为 `556a453e37c578fdfdff7191605a3ad40ad12f8a`；本文固定版本随后只补充了验收文档，功能源码和题目一致。

| 题目 | 是否通过 | 实际请求次数 | 总 token |
| --- | --- | --- | --- |
| `read_facts` | 通过 | 2 | 4,939 |
| `fix_sum` | 通过 | 4 | 11,649 |
| `extend_stats` | 通过 | 5 | 15,463 |

正常组实际请求合计 11 次，每题都没有超过上限 8。求和题只修改了 `main.py`；统计题只修改了 `src/stats.py`，补齐空列表时的平均值 0.0、最小值和最大值 `None`。两题的测试文件与初始版本逐字节一致。

> 【截图 7】正常组三题的判定、请求次数和汇总。上述表格记录已核验的历史运行；重新复现时，轮次、用量和是否一次成功都以新报告为准。

还可以展开编码题的判定和固定验收输出。这里读取的仍是 `$normalReport` 对应的一组，不重新猜测“最新日志”。

```powershell
foreach ($run in @($normal.runs | Where-Object { $_.task_id -ne 'read_facts' })) {
    Write-Host "`n[$($run.task_id)] $($run.workspace)"
    $run.checks | ConvertTo-Json
    if ($null -ne $run.acceptance) {
        $run.acceptance |
            Select-Object ok, exit_code, timed_out, stdout_truncated, stderr_truncated |
            Format-List
        $run.acceptance.stdout
        $run.acceptance.stderr
    }
}
```

本次两题的各项判定均为 `true`，固定验收退出码为 0，没有超时或截断，输出包括 `Ran 4 tests`、`OK` 和 `LEARN_AGENT_FIXED_ACCEPTANCE_PASSED`。

这些结果来自评测器真实运行的验收子进程，与 Agent 自己运行的业务测试分别保留。固定验收不调用模型，不额外产生 token。

> 【截图 8】至少一题的完整 `checks` 和固定验收输出，包含四项测试、退出码、完成标记及超时/截断字段。

## 8. 从每一轮回复重算用量，再核对差值

评测报告最容易看错的地方，是只盯着最终一轮的消耗，或者只统计成功任务。

每轮请求都会重新发送当前上下文，这些历史内容同样属于本轮输入。任务用量需要累加每次回复的 `usage`；失败任务也已经花掉了请求和 token，不能从统计中删除。某次用量缺失时保留未知值，`known_total_tokens` 只表示已知部分。

本次真实两组的总数为：

| 组别 | 通过率 | 请求次数 | 输入 token | 输出 token | 总 token | 平均 Agent 耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| `limited-1` | 0/3 | 3 | 7,241 | 205 | 7,446 | 1.348816 秒 |
| `normal-8` | 3/3 | 11 | 31,102 | 949 | 32,051 | 9.006701 秒 |

两组用量均完整，合计 39,497 token。正常组减低预算组，成功率差值为 1.0，即增加 100 个百分点；总 token 差值为 24,605；平均耗时差值约为 7.657885 秒。

`--compare` 要求题目及验收版本相同、重复次数相同，完整执行的两组才计算差值。`suite_hash` 标识固定题目、提示词、验收源码和判定版本；报告还记录模型、Git 提交和源码是否有未提交修改，帮助判断比较是否合理。

下面直接从六份 Agent 日志里的逐次模型回复重新计算 token，再与报告核对，最后检查对照差值。这些命令只读日志，不调用模型。

```powershell
foreach ($report in @($limited, $normal)) {
    $groupTokens = 0L
    foreach ($run in $report.runs) {
        $events = @(Get-Content -LiteralPath $run.log -Encoding UTF8 |
            ForEach-Object { $_ | ConvertFrom-Json })
        $responses = @($events | Where-Object { $_.event -eq 'model_response' })
        $inputTokens = 0L
        $outputTokens = 0L
        $totalTokens = 0L
        foreach ($response in $responses) {
            $usage = $response.response.usage
            if ($null -eq $usage.prompt_tokens -or $null -eq $usage.completion_tokens -or $null -eq $usage.total_tokens) {
                throw "本次用量不完整：$($run.task_id)"
            }
            $inputTokens += [long]$usage.prompt_tokens
            $outputTokens += [long]$usage.completion_tokens
            $totalTokens += [long]$usage.total_tokens
        }
        if ($responses.Count -ne $run.model_calls -or
            $inputTokens -ne $run.usage.prompt_tokens -or
            $outputTokens -ne $run.usage.completion_tokens -or
            $totalTokens -ne $run.usage.total_tokens) {
            throw "逐轮用量与报告不一致：$($run.task_id)"
        }
        $groupTokens += $totalTokens
        [pscustomobject]@{
            group = $report.label; task = $run.task_id
            calls = $responses.Count; input = $inputTokens
            output = $outputTokens; total = $totalTokens
        } | Format-Table -AutoSize
    }
    if ($groupTokens -ne $report.summary.usage.total_tokens) { throw '全组 token 合计不一致。' }
    Write-Host "$($report.label) 逐轮合计正确：$groupTokens token"
}

$rateDelta = [double]$normal.summary.success_rate - [double]$limited.summary.success_rate
$secondsDelta = [double]$normal.summary.mean_agent_seconds - [double]$limited.summary.mean_agent_seconds
$tokensDelta = [long]$normal.summary.usage.total_tokens - [long]$limited.summary.usage.total_tokens
if (-not $normal.comparison.comparable -or
    [math]::Abs($rateDelta - [double]$normal.comparison.success_rate_delta) -gt 0.000000001 -or
    [math]::Abs($secondsDelta - [double]$normal.comparison.mean_agent_seconds_delta) -gt 0.000000001 -or
    $tokensDelta -ne $normal.comparison.total_tokens_delta) {
    throw '对照差值不一致，或两组不可比较。'
}
$normal.comparison | ConvertTo-Json
Write-Host '逐轮用量、报告合计和对照差值核对通过。'
```

本段针对本次未开启摘要、子任务且用量完整的默认评测。前面的委派演示另有子日志，不能用“父日志的普通回复之和”漏算子任务；一般任务应结合最终预算汇总检查。

> 【截图 9】逐轮重算的任务用量、两组总数、`comparison` 和最后的核对通过提示。保留真实数字，不为了与文章表格一致而修改报告。

平均耗时也有明确口径：`agent_seconds` 统计整个 Agent 命令进程，包含启动、人工确认等待和会话保存，不包含随后由评测器执行的固定验收。终端内部显示的循环耗时会略短。例如本次统计题的内部耗时为 10.947 秒，报告为约 11.120 秒，符合两种计时范围。

正常组执行了更多工作，还等待了人工确认，因此这个耗时差值不能解释成模型推理速度变化。三道小题、每题一次，也不足以推出通用成功率。本次先确认的是：题目能隔离、失败会保留、完成有证据、统计可核对。

如果稍后关闭了终端，回到本次独立副本的根目录后，可以重新加载保存的路径，查看 Markdown 报告，不需要重新运行评测：

```powershell
$savedReports = Get-Content -LiteralPath .\logs\article-07-reports.json -Raw -Encoding UTF8 | ConvertFrom-Json
$limitedReport = $savedReports.limited
$normalReport = $savedReports.normal
$limited = Get-Content -LiteralPath $limitedReport -Raw -Encoding UTF8 | ConvertFrom-Json
$normal = Get-Content -LiteralPath $normalReport -Raw -Encoding UTF8 | ConvertFrom-Json
Get-Content -LiteralPath ([System.IO.Path]::ChangeExtension($normalReport, '.md')) -Encoding UTF8
```

## 9. 这一阶段留下了什么

项目规则和 Skill 让工作方法有了稳定入口；计划把进度与实际工具结果联系起来；插件提供可执行的新工具；子 Agent 把一项明确的阅读检查放进独立上下文。它们各自解决不同问题，都仍然经过原有的权限、预算和日志流程。

固定评测则为这些后续改动留下了可重复使用的检查方法。以后调整提示词、工具实现或模型适配时，可以继续使用相同题目版本与重复次数，观察完成情况和消耗，再结合失败记录决定下一步。

对应代码主要分布在这些文件中：

| 文件 | 职责 |
| --- | --- |
| `learn_agent/instructions.py`、`planning.py` | 加载项目约定，维护有工具证据的计划 |
| `learn_agent/providers.py` | 统一模型请求与回复接口 |
| `learn_agent/extensions.py`、`extensions/python-check/` | 加载 Skill 与本地插件，提供静态源码检查示例 |
| `learn_agent/subagents.py` | 独立只读子任务、共享预算与取消传播 |
| `eval.py`、`learn_agent/evaluation.py` | 固定题调度、逐题判定、用量与对照报告 |
| `evals/cases.json`、`evals/grade.py` | 初始题目与外置固定验收 |

这次开发最有价值的变化，是我开始能够追问每个“完成”：读取来源是什么，修改落在哪个文件，测试是否真的运行，验收条件有没有改变，用量能不能从原始记录算回来。Agent 的能力逐步增多以后，这些可检查的依据也需要一起长出来。
