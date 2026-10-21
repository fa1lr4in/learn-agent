上一篇，我给自己开发的 Agent 接入了项目规则、扩展工具和只读子任务，又用三道固定题检查它的实际表现。到这里，Agent 已经能阅读代码、修改文件、运行测试，并保存执行过程。

不过，使用它仍然要盯着终端：输入任务，看一长串日志，在提示处输入 `y`，再从输出里找测试结果。任务稍微长一点，就容易把模型的说明、等待确认的操作和已经发生的执行混在一起。

这一阶段，我给它做了一个极简网页工作台。左边是项目和历史会话，右边是对话、工具过程和确认按钮，支持浅色、深色与跟随系统主题。输入任务后，可以直接在网页里检查代码差异、批准操作、查看运行结果，也可以拒绝或停止任务。

界面只是开始。接下来更需要弄清楚：网页刷新以后，任务还在吗？按钮点一次，会不会执行两次？模型说“完成了”，是否意味着结果真的保存了？

本文沿着一次小修改展开：把统计程序的输入从 `[6, 12, 18]` 改为 `[7, 14, 21]`，运行四项测试和入口，再检查刷新、拒绝、停止及历史读取。

项目地址：[fa1lr4in/learn-agent](https://github.com/fa1lr4in/learn-agent)。下面的关键代码来自固定提交，演示在独立副本中进行。

## 1. 实验环境与复现版本

| 项目 | 本次环境 |
| --- | --- |
| 操作系统 | Windows 11，64 位 |
| 终端 | Windows PowerShell 5.1、PowerShell 7；下文命令兼容两者 |
| Python | 3.13.9 |
| Git | 2.51.0.windows.1 |
| 浏览器 | 支持 Fetch、ReadableStream 和 localStorage 的现代浏览器 |
| 模型服务与真实运行模型 | DeepSeek 官方 API，`deepseek-flash` |
| 后端 | `fastapi==0.152.0`、`uvicorn==0.54.0`、`httpx==0.28.1` |
| 其他依赖 | `mcp==2.3.0`、`jsonschema==4.26.0`、`PyYAML==6.0.3`，统一按 `requirements.txt` 安装 |
| 前端 | 原生 HTML、CSS、JavaScript，无需前端构建服务 |
| 前端测试环境 | Node.js 24.16.0；只运行网页时不需要 Node.js |
| 本文固定版本 | `d357c153cba2a011377b6b56d4592d90d5e9b086` |

先克隆独立副本，避免影响开发目录。下面会把副本路径保存到当前用户临时目录中的 `learn-agent-article-08.json`。后面每个终端都从这个文件重新取得路径，不依赖另一个窗口里的变量。

```powershell
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$reproDir = Join-Path $env:TEMP ('learn-agent-08-' + [guid]::NewGuid().ToString('N'))
git -c http.sslBackend=openssl -c http.version=HTTP/1.1 clone https://github.com/fa1lr4in/learn-agent.git "$reproDir"
if ($LASTEXITCODE -ne 0) { throw '克隆失败，请检查网络。' }
Set-Location -LiteralPath $reproDir
git checkout --detach d357c153cba2a011377b6b56d4592d90d5e9b086
if ($LASTEXITCODE -ne 0) { throw '未能切换到本文版本。' }
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败。' }
& .\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
@{ repro_dir = $reproDir; project = (Join-Path $reproDir 'logs\article-08\project') } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Encoding UTF8
Get-Location
git rev-parse HEAD
$PSVersionTable.PSVersion
& .\.venv\Scripts\python.exe --version
git --version
```

> 【截图 1】副本路径、固定提交、PowerShell、Python 与 Git 版本。依赖安装的长输出可以略去。

API Key 继续从后端环境变量读取，不写入 Markdown、项目源码或网页。正式发送任务会产生模型 API 用量；安装依赖、读取日志和运行本地测试不会调用模型。

## 2. 准备同一个统计项目

演示项目仍然很小：

```text
project/
├── main.py
├── test_stats.py
└── src/
    └── stats.py
```

入口准备一组数字，`summarize()` 返回数量、总和、平均值、最小值和最大值。四项测试覆盖普通列表、空列表、单个数字和负数。

这里有一个需要交代的初始条件：仓库固定版本里的练习代码保留了较早的输入和统计字段，而本地开发时已经补齐了 `min`、`max`。直接克隆后执行测试，会出现初始状态不一致。因此下面从版本中的文件复制一个独立项目，明确补齐这两个字段，再把输入设为 `[6, 12, 18]`。

这段准备只执行一次；需要从头重做时，重新执行上一节，建立新的副本。

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
@'
import shutil
import sys
from pathlib import Path

target = Path(sys.argv[1])
assert not target.exists(), '演示项目已存在；从头重做请重新建立副本。'
shutil.copytree(Path('workspace/demo_project'), target,
                ignore=shutil.ignore_patterns('__pycache__'))
main = target / 'main.py'
stats = target / 'src/stats.py'
text = main.read_text(encoding='utf-8')
assert text.count('values = [12, 18, 30]') == 1
main.write_text(text.replace('values = [12, 18, 30]', 'values = [6, 12, 18]'), encoding='utf-8')
text = stats.read_text(encoding='utf-8')
marker = '        "average": total / count if count else 0.0,\n'
assert text.count(marker) == 1 and '"min"' not in text
text = text.replace(marker, marker +
    '        "min": min(values) if count else None,\n'
    '        "max": max(values) if count else None,\n')
stats.write_text(text, encoding='utf-8')
print('演示项目：', target)
'@ | & .\.venv\Scripts\python.exe -B -X utf8 - $article.project
if ($LASTEXITCODE -ne 0) { throw '演示项目准备失败。' }
Push-Location -LiteralPath $article.project
try {
    & (Join-Path $article.repro_dir '.venv\Scripts\python.exe') -B -X utf8 -m unittest -v test_stats.py
    if ($LASTEXITCODE -ne 0) { throw '初始业务测试未通过。' }
    & (Join-Path $article.repro_dir '.venv\Scripts\python.exe') -B -X utf8 main.py
    if ($LASTEXITCODE -ne 0) { throw '入口运行失败。' }
} finally { Pop-Location }
```

初始四项测试应全部通过，入口输出：

```text
{'count': 3, 'total': 36, 'average': 12.0, 'min': 6, 'max': 18}
```

> 【截图 2】初始项目的四项测试和入口输出，确认总和为 36、平均值为 12.0。

## 3. 用一个本地服务打开工作台

最初做前端时，页面使用固定数据来练习消息、工具卡和确认按钮。接入真实任务以后，我把页面和 API 都放在同一个 Python 服务中。

现在只需要打开 `http://127.0.0.1:8000/`。原来的静态前端地址 `5173` 不再用于本文流程，也不需要运行 `npm install` 或前端开发服务器。

在终端 A 启动后端。若原来已有服务占用 8000，先在原终端按 `Ctrl+C`，等服务退出；不要同时启动两份。

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$env:DEEPSEEK_MODEL = 'deepseek-flash'
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    $secret = Read-Host '请输入 DeepSeek API Key（不回显）' -AsSecureString
    $env:DEEPSEEK_API_KEY = (New-Object System.Net.NetworkCredential('', $secret)).Password
    Remove-Variable secret
}
& .\.venv\Scripts\python.exe -B -X utf8 api.py --workspace $article.project
```

保留终端 A 运行，在浏览器打开上面的地址。页面应显示项目名 `project` 和“已连接本地服务”。右上角可以切换浅色、深色或跟随系统；选择深色后刷新，偏好仍会保留。

> 【截图 3】空白工作台的整体布局；切换深色后再截一张。保留项目、会话侧栏、输入区和主题入口。

页面的职责分成了几个小文件：

| 文件 | 主要职责 |
| --- | --- |
| `web/index.html`、`styles.css` | 页面结构与极简布局 |
| `web/theme.js` | 首屏主题、系统跟随与偏好保存 |
| `web/client.js` | 本地 HTTP 请求、SSE 解码和续收 |
| `web/view-model.js` | 将真实消息与事件转换成消息、工具状态 |
| `web/app.js` | 发送、审批、停止、历史选择和页面更新 |

深色模式通过根元素的 `data-theme` 切换；偏好在样式加载前应用，避免保存了深色主题以后，页面先闪出浅色。`localStorage` 还会保存当前会话和任务 ID，模型 API Key 不存进浏览器。

网页取得的是本次后端进程的本地访问令牌。它用于访问本机 API，与 DeepSeek API Key 是两种凭据；后端重启会更换这个本地令牌，刷新页面后重新取得。

## 4. 网页提交任务，Python 继续负责执行

这次没有在浏览器里重写一个 Agent。原来的核心循环仍在 Python 中，命令行和网页后端复用同一套工具、预算、权限、日志和会话保存逻辑。

一次操作大致经过这条路径：

```text
网页输入任务
    ↓ POST：创建并提交任务
Python Agent 循环 → 调用模型 → 得到工具请求
    ↓ SSE：推送等待确认事件
网页展示真实代码差异
    ↓ POST：提交本项审批 ID 和决定
Python 执行工具 → 将真实结果交回模型
    ↓ SSE：推送工具结果、回答与保存完成通知
网页更新工具卡和最终状态
```

API 把“会话”和“任务”分开：会话保存聊天历史，一个会话可以先后包含多次任务；任务对应一次提交、审批和执行过程。

| 接口 | 用途 |
| --- | --- |
| `POST /sessions` | 建立会话 |
| `POST /sessions/{session_id}/tasks` | 提交本次任务 |
| `GET /tasks/{task_id}` | 读取任务快照 |
| `GET /tasks/{task_id}/events` | 观察持续事件 |
| `POST /tasks/{task_id}/approvals` | 确认或拒绝指定审批 |
| `POST /tasks/{task_id}/cancel` | 申请停止 |

提交接口返回 HTTP 202，只表示后端接收了任务。任务可能还在请求模型，也可能等待确认，不能看到 202 就显示“执行成功”。

前端提交的关键代码在 `web/app.js`，下面只保留请求部分：

```javascript
const task = await api.request(`/sessions/${state.session.id}/tasks`, {
  prompt,
  stream: true,
  auto_read: ui["auto-read"].checked,
  max_model_calls: 8
});
```

在终端 B 输出本次任务文本，再复制到网页的新会话中发送。保持“自动允许读取”勾选；它只影响读取类工具，编辑和运行仍分别确认。

```powershell
$taskText = @'
网页实验08：读取 main.py，只用 edit_file 将 values = [6, 12, 18] 改为 values = [7, 14, 21]。
修改成功后，用 run_command 运行 python -m unittest -v test_stats.py，再用 run_python 运行 main.py。
不要修改其他文件，简要报告真实结果。
'@
Write-Output $taskText
```

**停在编辑确认处，先不要点击允许。** 页面应展示准备执行的真实差异：

```diff
-    values = [6, 12, 18]
+    values = [7, 14, 21]
```

这份 diff 在准备编辑时产生，等待确认事件把它带给网页。批准以后，执行使用同一份准备好的操作。如果文件在等待期间被其他程序修改，既有检查会阻止覆盖，避免网页确认的内容和实际写入脱节。

> 【截图 4】编辑审批区、真实代码差异、拒绝和允许按钮。此时文件还没有修改。

## 5. 刷新网页，恢复的是观察过程

前面已经能让模型逐段输出文字，现在又增加了 Python 后端到网页的事件流。这是两个不同方向的流：

```text
模型 → Python：模型回答片段
Python → 网页：模型片段、工具等待、执行结果、任务结束等事件
```

后者使用 SSE。每条事件有任务 ID 和序号，游标形如 `任务ID:序号`。首次连接可以拿到覆盖已有过程的快照，之后接收增量；重连时带上 `Last-Event-ID`，补收尚未看到的部分。

`web/client.js` 收到完整事件帧后，会检查任务归属和序号，再交给页面：

```javascript
if (frame.task_id !== taskId) throw new Error("收到其他任务的事件，已停止观察。");
const current = sequence(frame.id, taskId);
if (current <= last) continue;
if (frame.event !== "snapshot" && current !== last + 1) {
  throw new Error("事件序号不连续，请重新连接。");
}
await onEvent(frame);
cursor = frame.id;
last = current;
```

读取网络数据时还需要处理 UTF-8 字符和半截事件帧。一次网络读取未必包含完整 JSON，也可能把一个中文字符拆到两个数据块中；解析器累计完整帧以后才处理，半截帧不会推进游标。

下面记录正在等待编辑的真实任务，同时用终端观察一次快照。任务 ID 从后端动态取得，再保存到本地文件，后面不用手抄。

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$api = Get-Content -LiteralPath .\logs\.api-server.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($api.token)" }
$tasks = Invoke-RestMethod -Uri "$($api.url)/tasks" -Headers $headers
$running = @($tasks | Where-Object { -not $_.finished })
if ($running.Count -ne 1) { throw '请先在网页提交任务，并停在编辑审批处。' }
$task = Invoke-RestMethod -Uri "$($api.url)/tasks/$($running[0].id)" -Headers $headers
if ($task.pending_approval.tool -ne 'edit_file') { throw '当前不是编辑审批。' }
@{
    id = $task.id; session_id = $task.session_id; approval_id = $task.pending_approval.id
    cursor = $task.last_event_id; event_count = @($task.events).Count
    source_hash = (Get-FileHash -LiteralPath (Join-Path $article.project 'main.py') -Algorithm SHA256).Hash
} | ConvertTo-Json | Set-Content -LiteralPath .\logs\article-08-main.json -Encoding UTF8
& .\.venv\Scripts\python.exe -B -X utf8 watch_task.py $task.id --limit 1
if ($LASTEXITCODE -ne 0) { throw '快照观察失败。' }
```

终端应显示 `snapshot`、`waiting_approval` 和“已主动断开观察连接；任务继续运行”。这里的“断开”只关闭这个终端的观察连接，网页中的审批仍然有效。

> 【截图 5】终端的快照、等待审批信息和续收游标。无需展示 `.api-server.json` 的内容。

现在刷新网页，或者关闭这个网页后重新打开相同地址。编辑仍需确认。再执行：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$api = Get-Content -LiteralPath .\logs\.api-server.json -Raw -Encoding UTF8 | ConvertFrom-Json
$base = Get-Content -LiteralPath .\logs\article-08-main.json -Raw -Encoding UTF8 | ConvertFrom-Json
$task = Invoke-RestMethod -Uri "$($api.url)/tasks/$($base.id)" -Headers @{ Authorization = "Bearer $($api.token)" }
$hash = (Get-FileHash -LiteralPath (Join-Path $article.project 'main.py') -Algorithm SHA256).Hash
if ($task.pending_approval.id -ne $base.approval_id -or $task.last_event_id -ne $base.cursor -or
    @($task.events).Count -ne $base.event_count -or $hash -ne $base.source_hash) { throw '基准已变化，请检查是否操作了审批。' }
'同一任务、同一审批；事件和源码未变，刷新没有追加模型请求或执行工具。'
```

> 【截图 6】刷新后的原编辑审批，以及终端中的一致性核验结果。

这次实现里，网页刷新会先查找仍在运行的任务，取得快照，再继续观察。它不会因为页面重新加载，就重新发送用户任务。提交响应如果丢失，也会先查询后端是否已接收，避免直接重试 POST。

Windows 下还遇到过一个实际问题：终端观察器保存游标时，临时文件替换目标文件可能因为读取占用而被拒绝。现在游标先写入并关闭临时文件，遇到特定的短暂占用会有限重试；失败时保留旧游标，之后可以续收。这个修复针对观察器的本地文件保存，不会触发模型重试或工具重放。

## 6. 分别确认编辑、测试和运行

回到网页，允许编辑。随后，运行测试需要单独确认，入口运行也需要单独确认。即使模型在同一轮提出多个工具请求，每项需要人工批准的操作仍分别等待。

确认绑定的是具体的 `approval_id`。后端会检查它是否还是当前待确认项；上一项审批的 ID 不能用来批准下一项。等待回复时，网页也会禁用重复点击。

测试审批处可以再刷新一次：编辑卡应仍为完成，测试仍未执行。接着允许测试和入口，等待页面显示会话已保存。

完成后，在终端 B 补收刚才观察器断开以后的事件：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$base = Get-Content -LiteralPath .\logs\article-08-main.json -Raw -Encoding UTF8 | ConvertFrom-Json
& .\.venv\Scripts\python.exe -B -X utf8 watch_task.py $base.id --resume
if ($LASTEXITCODE -ne 0) { throw '事件续收失败。' }
```

展开网页中的两个运行工具，预期测试退出码为 0，`stderr` 中有四项测试和 `OK`；入口退出码为 0，`stdout` 为：

```text
{'count': 3, 'total': 42, 'average': 14.0, 'min': 7, 'max': 21}
```

`unittest` 把正常测试报告写到 `stderr`，因此不能只凭这个字段非空就判断失败；需要结合退出码和内容。

> 【截图 7】网页中的测试报告、入口 stdout、完成状态和会话已保存；终端补收应到达 `task_finished`。

这里还要区分两个时刻：`run_end` 表示 Agent 循环已经结束，`task_finished` 表示后端完成了收尾和保存尝试。网页在中间阶段显示“正在保存会话”，不会把模型回答结束直接当作保存成功。

本次已核验的一次真实 DeepSeek 修改任务，数据如下：

| 项目 | 真实记录 |
| --- | --- |
| 输入变化 | `[6, 12, 18]` → `[7, 14, 21]` |
| 工具过程 | 读取、局部编辑、运行四项测试、运行入口 |
| 测试 | 4 项通过 |
| 入口 | total=42，average=14.0，min=7，max=21 |
| 模型请求 | 5 次，均收到完整回复 |
| 输入 token | 13839 |
| 输出 token | 400 |
| 总 token | 14239 |
| 任务状态与原因 | completed / stop |
| 记录耗时 | 189.648 秒，包含等待人工确认的时间 |

这个耗时不能直接当作模型推理速度。复现时，请求次数和 token 也可能不同：模型可以把测试与入口放在同一轮请求，也可能分两轮；审批等待时间由人决定。需要核对的是实际过程和累计数据，而不是强求每次数字相同。

下面同时核对 API 快照、JSONL、逐轮用量和 SQLite 保存的工具结果，并保存完成后的日志基准：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$api = Get-Content -LiteralPath .\logs\.api-server.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($api.token)" }
$base = Get-Content -LiteralPath .\logs\article-08-main.json -Raw -Encoding UTF8 | ConvertFrom-Json
$task = Invoke-RestMethod -Uri "$($api.url)/tasks/$($base.id)" -Headers $headers
if (-not $task.finished -or $task.state -ne 'completed' -or $task.reason -ne 'stop' -or
    -not $task.session_saved -or $task.persistence_error) { throw '任务尚未正常完成并保存。' }
$events = @(Get-Content -LiteralPath $task.log_path -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json })
$end = $events | Where-Object { $_.event -eq 'run_end' } | Select-Object -Last 1
$responses = @($events | Where-Object { $_.event -eq 'model_response' })
$inputTokens = 0L; $outputTokens = 0L; $totalTokens = 0L
foreach ($response in $responses) {
    $usage = $response.response.usage
    if ($null -eq $usage.prompt_tokens -or $null -eq $usage.completion_tokens -or $null -eq $usage.total_tokens) { throw '用量不完整。' }
    $inputTokens += [long]$usage.prompt_tokens
    $outputTokens += [long]$usage.completion_tokens
    $totalTokens += [long]$usage.total_tokens
}
foreach ($summary in @($task.summary, $end.summary)) {
    if ($summary.model_calls -ne $responses.Count -or $summary.usage.prompt_tokens -ne $inputTokens -or
        $summary.usage.completion_tokens -ne $outputTokens -or $summary.usage.total_tokens -ne $totalTokens) { throw '逐轮用量与累计用量不一致。' }
}
if ($end.reason -ne 'stop' -or $end.task_state -ne 'completed') { throw '日志状态不一致。' }
$results = @($events | Where-Object { $_.event -eq 'tool_result' })
$test = $results | Where-Object { $_.tool -eq 'run_command' } | Select-Object -Last 1
$entry = $results | Where-Object { $_.tool -eq 'run_python' } | Select-Object -Last 1
if (-not $test.result.ok -or $test.result.exit_code -ne 0 -or $test.result.stderr -notmatch 'Ran 4 tests' -or
    $test.result.stderr -notmatch '(?m)^OK\s*$' -or -not $entry.result.ok -or $entry.result.exit_code -ne 0 -or
    $entry.result.stdout -notmatch "'total': 42" -or $entry.result.stdout -notmatch "'average': 14\.0") { throw '测试或入口输出不符合预期。' }
$session = Invoke-RestMethod -Uri "$($api.url)/sessions/$($base.session_id)" -Headers $headers
$saved = @($session.tool_runs | Where-Object { $_.run_log -eq $task.log_path -and $null -ne $_.result })
if ($saved.Count -ne $results.Count) { throw '保存的工具结果数不一致。' }
foreach ($result in $results) {
    $record = @($saved | Where-Object { $_.step -eq $result.step -and $_.call_id -eq $result.tool_call_id })
    if ($record.Count -ne 1 -or
        ($record[0].result | ConvertTo-Json -Depth 20 -Compress) -ne ($result.result | ConvertTo-Json -Depth 20 -Compress)) { throw 'SQLite 与 JSONL 的工具结果不一致。' }
}
$stamp = @{
    cursor = $task.last_event_id
    log_hash = (Get-FileHash -LiteralPath $task.log_path -Algorithm SHA256).Hash
    source_hash = (Get-FileHash -LiteralPath (Join-Path $article.project 'main.py') -Algorithm SHA256).Hash
}
if ($base.completed -and ($base.completed.cursor -ne $stamp.cursor -or $base.completed.log_hash -ne $stamp.log_hash -or
    $base.completed.source_hash -ne $stamp.source_hash)) { throw '完成后出现新的事件或文件变化。' }
$base | Add-Member -NotePropertyName completed -NotePropertyValue $stamp -Force
$base | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath .\logs\article-08-main.json -Encoding UTF8
$task | Select-Object state, reason, finished, session_saved, persistence_error | Format-List
$task.summary | ConvertTo-Json -Depth 5
'工具结果、逐轮用量、JSONL 和 SQLite 核对通过。'
```

> 【截图 8】完成与保存字段、这次复现自己的用量，以及核对通过提示。不要把表中的 14239 token 当作复现必须得到的数字。

同一后端进程内，刷新网页、重新打开已完成历史后，可以再执行一次这段核验。原任务的日志、游标、文件和用量都应不变。查看已有内容不需要再次请求模型；继续发送新问题则会产生新的用量。

## 7. 拒绝和停止，也需要留下准确的结果

接通页面以后，我发现了一个容易忽略的问题：实时执行时能看到“已拒绝”，但重新打开历史，工具卡却可能只显示“尚未执行”，顶部也回到“就绪”。

原因是之前只根据聊天消息还原历史。用户拒绝的工具没有实际执行结果，只看消息，无法还原明确的审批决定。

现在 `historyItems(messages, toolRuns)` 同时读取保存的消息和审批记录；相同工具调用 ID 按出现顺序匹配。没有结果时，根据真实审批状态显示：

```javascript
status: run?.approved === false ? "denied"
  : run?.approved === true ? "unknown"
  : "cancelled"
```

已拒绝就是已拒绝；批准过但没有结果，只能显示结果未确认；没有执行的请求显示未执行。后续出现真实工具结果，再据此显示完成或失败。顶部状态同样从保存的结束原因恢复。

先新建会话，复制下面的任务发送，在编辑审批处点击**拒绝**：

```powershell
$taskText = '网页实验08拒绝：先读取 main.py，只用 edit_file 将 values = [7, 14, 21] 改为 values = [99, 99, 99]，不要修改其他文件。'
Write-Output $taskText
```

等任务保存后，点击侧栏这条已拒绝历史。编辑卡应仍显示已拒绝，源码保持原样。以下从实际任务中找到对应记录，不使用“最后一个日志”猜测：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$api = Get-Content -LiteralPath .\logs\.api-server.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($api.token)" }
$base = Get-Content -LiteralPath .\logs\article-08-main.json -Raw -Encoding UTF8 | ConvertFrom-Json
$task = $null
$tasks = Invoke-RestMethod -Uri "$($api.url)/tasks" -Headers $headers
foreach ($item in $tasks) {
    $detail = Invoke-RestMethod -Uri "$($api.url)/tasks/$($item.id)" -Headers $headers
    $start = $detail.events | Where-Object { $_.event -eq 'run_start' } | Select-Object -First 1
    $user = $start.initial_messages | Where-Object { $_.role -eq 'user' } | Select-Object -Last 1
    if ($user.content -like '网页实验08拒绝：*') { $task = $detail }
}
if (-not $task.finished -or -not $task.session_saved -or $task.state -ne 'cancelled' -or $task.reason -ne 'user_denied') { throw '拒绝任务的结束状态不正确。' }
$executed = @($task.events | Where-Object { $_.event -in @('tool_started', 'tool_result') -and $_.tool -in @('edit_file', 'write_file', 'run_python', 'run_command') })
$hash = (Get-FileHash -LiteralPath (Join-Path $article.project 'main.py') -Algorithm SHA256).Hash
if ($executed.Count -ne 0 -or $hash -ne $base.completed.source_hash) { throw '拒绝后出现执行或文件变化。' }
@{ id = $task.id; session_id = $task.session_id } | ConvertTo-Json |
    Set-Content -LiteralPath .\logs\article-08-denied.json -Encoding UTF8
$task | Select-Object state, reason, finished, session_saved | Format-List
'编辑未执行，源码未变；允许发生的读取结果与被拒绝的编辑分开检查。'
```

> 【截图 9】重新打开的已拒绝历史，以及终端的 cancelled / user_denied、源码未变提示。

再新建会话，发送下面的任务，在运行审批处点击**停止**，不点允许：

```powershell
$taskText = '网页实验08停止：只用 run_python 运行 main.py，不读取、创建或修改文件。'
Write-Output $taskText
```

等结束并保存后，再打开该历史。运行卡应显示未执行，不应出现这次运行的 stdout。核对命令如下：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$api = Get-Content -LiteralPath .\logs\.api-server.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($api.token)" }
$task = $null
$tasks = Invoke-RestMethod -Uri "$($api.url)/tasks" -Headers $headers
foreach ($item in $tasks) {
    $detail = Invoke-RestMethod -Uri "$($api.url)/tasks/$($item.id)" -Headers $headers
    $start = $detail.events | Where-Object { $_.event -eq 'run_start' } | Select-Object -First 1
    $user = $start.initial_messages | Where-Object { $_.role -eq 'user' } | Select-Object -Last 1
    if ($user.content -like '网页实验08停止：*') { $task = $detail }
}
if (-not $task.finished -or -not $task.session_saved -or $task.state -ne 'cancelled' -or $task.reason -ne 'cancelled') { throw '停止任务的结束状态不正确。' }
if (@($task.events | Where-Object { $_.event -in @('tool_started', 'tool_result') }).Count -ne 0) { throw '出现了不应执行的工具。' }
@{ id = $task.id; session_id = $task.session_id } | ConvertTo-Json |
    Set-Content -LiteralPath .\logs\article-08-cancelled.json -Encoding UTF8
$task | Select-Object state, reason, finished, session_saved | Format-List
'等待运行时已停止，没有工具执行和运行输出。'
```

> 【截图 10】重新打开的已取消历史、未执行工具卡，以及终端的 cancelled / cancelled。

这里演示的是在等待审批时停止。若子进程已经开始运行，当前取消机制会等待正在执行的操作返回，再阻止后续操作；不能把停止按钮理解为任意时刻立刻终止整个进程树。

## 8. 后端重启后，历史与运行任务有什么不同

网页刷新只是观察连接变化，后端进程还在，内存任务也还在。后端重启则不同：本次进程中的任务和事件索引消失，但已经写入 SQLite 的会话仍然存在。

因此重启前先确保三个演示任务都已结束并保存。在终端 A 按 `Ctrl+C`，等退出后重新执行：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
& .\.venv\Scripts\python.exe -B -X utf8 api.py --workspace $article.project
```

这仍然使用终端 A 已设置的模型环境变量。刷新网页，取得本次进程的新令牌，再在终端 B 检查：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$api = Get-Content -LiteralPath .\logs\.api-server.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($api.token)" }
$tasks = Invoke-RestMethod -Uri "$($api.url)/tasks" -Headers $headers
if (@($tasks).Count -ne 0) { throw '新进程中已有任务，请在发送新问题前核对。' }
foreach ($name in @('main', 'denied', 'cancelled')) {
    $saved = Get-Content -LiteralPath ".\logs\article-08-$name.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $session = Invoke-RestMethod -Uri "$($api.url)/sessions/$($saved.session_id)" -Headers $headers
    [pscustomobject]@{ case = $name; session_id = $session.id; reason = $session.reason; messages = @($session.messages).Count } | Format-Table -AutoSize
}
'新进程任务索引为空，三个已保存的会话仍可读取。'
```

> 【截图 11】重启后的历史会话列表，以及终端中仍可读取的三条会话。切换已拒绝、已取消历史时，顶部与工具卡应保留相应状态。

选择正常完成的会话，发送一个只依赖已有结果的问题：

```powershell
$taskText = '根据已有结果，入口 total 和 average 分别是多少？不要调用工具。'
Write-Output $taskText
```

预期回答 42 和 14.0。保存后执行下面的检查，确认新问题确实接在原会话中，也没有重放工具：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
$api = Get-Content -LiteralPath .\logs\.api-server.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($api.token)" }
$base = Get-Content -LiteralPath .\logs\article-08-main.json -Raw -Encoding UTF8 | ConvertFrom-Json
$tasks = Invoke-RestMethod -Uri "$($api.url)/tasks" -Headers $headers
$selected = $tasks | Where-Object { $_.session_id -eq $base.session_id } | Select-Object -Last 1
if (-not $selected) { throw '请先在正常完成的原会话中发送新问题。' }
$task = Invoke-RestMethod -Uri "$($api.url)/tasks/$($selected.id)" -Headers $headers
if (-not $task.finished -or -not $task.session_saved -or $task.reason -ne 'stop') { throw '续聊尚未完成并保存。' }
if (@($task.events | Where-Object { $_.event -in @('tool_started', 'tool_result') }).Count -ne 0) { throw '续聊出现工具执行，请检查模型是否遵守要求。' }
$task | Select-Object session_id, state, reason, session_saved, final_answer | Format-List
$task.summary | ConvertTo-Json -Depth 5
'同一会话继续回答，本次没有工具执行；新问题会产生新的模型用量。'
```

> 【截图 12】正常历史中的追问与回答，以及同一 session_id、没有工具执行的核验结果。

当前还没有实现进程退出后自动恢复未完成的内存任务。网页历史可读、正常会话可继续，并不意味着后台执行过程已经可以跨进程无缝恢复。

## 9. 把验证范围写清楚

这次网页阶段的检查分成两类。

真实 DeepSeek 联合验证中，已经完成了读取、编辑与分别审批、四项测试、入口输出、拒绝、停止和历史续聊。上面的 5 次请求、14239 token 来自实际修改任务的 JSONL，用量逐轮相加，并与保存记录核对。

本地检查则使用固定模型回复，专门控制真实模型不容易稳定制造的情况：提交已被后端接收但响应正文丢失、事件接口返回 503、旧审批重复提交，以及重启后旧令牌失效。这些检查中的文件操作、命令运行和 SQLite 保存实际发生，但固定回复的请求数与 token 是测试数据，不能当作模型表现。

为核对本文的操作顺序，我又从 GitHub 固定提交克隆了新副本，创建虚拟环境并安装依赖，在 Windows PowerShell 5.1 中实跑了项目准备、快照、续收、用量核验、拒绝、停止和重启后的历史检查。网页使用独立本地端口与固定模型回复，避免占用原开发服务；文件编辑、四项业务测试和 SQLite 保存实际执行。下面的完整自动化测试也在这个副本中通过，复现检查没有额外调用付费模型 API。

| 检查对象 | 已验证的行为 |
| --- | --- |
| 重复提交 | 正在执行或等待审批时，额外任务返回 409，不创建第二个执行任务 |
| 旧审批 | 已使用的审批 ID 不能授权下一项工具 |
| 事件恢复 | 完整帧、游标、重复事件、断序和其他任务事件均有检查 |
| 历史还原 | 拒绝、取消、实际结果与重复调用 ID 按保存记录还原 |
| 后端重启 | 新进程无旧任务索引，旧令牌失效，已保存历史仍可读 |
| 页面内容 | 模型文字和文件内容用 `textContent` 展示，不作为 HTML 执行 |

在独立副本中运行完整检查：

```powershell
$article = Get-Content -LiteralPath (Join-Path $env:TEMP 'learn-agent-article-08.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $article.repro_dir
& .\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { throw 'Python 自动化测试未通过。' }
node --version
node --test .\tests\test_web_client.mjs
if ($LASTEXITCODE -ne 0) { throw '前端自动化测试未通过。' }
```

本文固定版本为 **481 项 Python 测试、26 项前端测试通过**。最后两条命令需要已安装 Node.js；单纯使用工作台不需要安装它。自动化测试中的模型响应使用模拟数据，不产生 DeepSeek API 用量。

> 【截图 13】481 项 Python 测试与 26 项前端测试通过。它们与演示项目的四项业务测试分别计数。

目前的工作台只面向本机：启动时固定一个项目目录，一次运行一个任务；没有多用户服务，也没有操作系统级沙箱。前端尚未提供模型配置、项目切换、MCP 和插件配置面板，已有核心能力不等于都已经做成网页入口。

做完这一阶段，我对 Agent 产品多了一层认识。模型负责提出下一步，Python 负责执行与约束，网页负责把当前状态和需要人决定的部分呈现出来。三者之间，需要用真实事件和持久记录连接起来。

现在，这个项目已经可以通过网页完成一次代码修改，并在同一个界面里检查差异、批准运行、查看结果和读取历史。后面继续做桌面应用时，可以复用这套页面与执行内核，而本次留下的日志、会话和核验过程，也成为检查后续变化的依据。
