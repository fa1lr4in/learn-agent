# 第二课：模型提出请求，程序执行工具

第一课的真实 API 调用已由学习者于 2026-10-16 反馈成功。

本课目标：观察一次完整过程——模型提出读文件请求，你确认后 Python 读取文件，程序把结果交回模型，模型据此回答。

## 先校准第一课的理解

第一课的两条消息由不同的代码位置提供：`system` 的内容写在程序中，`user` 的内容来自你的命令行输入。两条都是我们的程序发出的请求的一部分；它们的 role 表示用途，不能一概视为 user 消息。

当前第一课程序没有实现并接入文件读取工具，因此模型无法通过它取得本地文件内容。能力是否存在，和模型本次是否选择使用该能力，是两个问题。

模型 API 不会因为你在消息里写“读取文件”，就获得操作系统的文件访问能力。声明一个工具也不会自动执行 Python 函数。

## 三部分分别由谁负责

| 部分 | 本课的位置 | 责任 |
| --- | --- | --- |
| 工具说明 | `tool_call.py` 中的 `TOOLS` | 程序告诉模型工具名称、用途和参数结构，不包含文件内容 |
| 工具请求 | 第一次响应里的 `message.tool_calls` | 模型选择工具，并生成参数 |
| 执行代码 | `read_file` 和 `run_demo` | 程序检查参数，等待确认，执行读取，回传结果 |

本课固定演示一次工具调用，最多两次模型请求。第二次设置 `tool_choice: none`，让模型整理结果。自动循环和让模型自行决定更多步骤留给下一课。

## 实验一：先看声明，不联网

```powershell
python tool_call.py --preview
```

在输出中找到 `tools[0].function`，观察 `name`、`description`、`parameters`。

这时模型能够知道有一个 `read_file` 工具，只允许参数 `path` 为 `project.txt`。请求里没有文件内容。

本课为缩小范围，只允许读取项目中的 `workspace/project.txt`。这是一个固定练习文件示例，通用文件工具的路径与进程隔离后续再学习。

## 实验二：完成一次真实工具调用

```powershell
python tool_call.py
```

输入自己的 DeepSeek API Key。第一次模型返回后，先停下来观察 `tool_calls`。它通常具有以下形状；下面的 ID 只是示意，实际运行由服务返回：

```json
{
  "id": "call_example",
  "type": "function",
  "function": {
    "name": "read_file",
    "arguments": "{\"path\":\"project.txt\"}"
  }
}
```

`arguments` 是包含 JSON 的字符串，程序使用 `json.loads` 把它转换为 Python 字典。这里没有执行字符串中的 Python 代码。

模型默认可以自行决定是否调用工具，`tool_choice` 是 `auto`。如果没有生成工具调用，程序会显示停止原因并结束；这不是读取成功。观察提示和模型响应，再分析原因。

输入 `y` 后，程序读取练习文件，并向 DeepSeek 发送包含文件内容的第二次请求。

在第二次请求中确认这四条消息的角色顺序：

```text
system → user → assistant（包含工具请求）→ tool（包含工具结果）
```

`tool` 消息中的 `tool_call_id` 要与模型刚才发出的调用 ID 对应。它说明这个结果是在回答哪次调用。

本课已经把第一课的“第二次请求要携带历史”落实到工具场景里：原始要求、模型请求和工具结果一起进入下一次模型调用。

## 实验三：修改真实数据，验证信息来源

在 `workspace/project.txt` 中把实验编号 `7391` 改为你自己选择的另一个编号，再次运行程序。

预测：程序打印的工具结果和最终回答是否都会变化？如果模型回答没有变化，先检查实际读取内容和第二次请求中的 `tool.content`，不要只看最终文字。

## 实验四：拒绝一次读取

再次运行，在确认处输入 `n`。

预期：不调用 `read_file`，不发第二次模型请求。程序在确认之前已经把问题和工具说明发给 DeepSeek，但尚未通过工具读取文件。

这个简单确认是本课的执行检查点；它不是操作系统沙箱。

## 本课练习

在打印工具请求后，增加一行更容易读的输出，例如：

```text
模型想调用 read_file，参数是 {'path': 'project.txt'}
```

尝试自己从 `function` 和解析后的 `arguments` 中取值。暂时不要扩展到其他文件或增加自动循环。

## 完成后回答

1. 哪一行 Python 真正读取了文件？模型返回 `tool_calls` 时文件是否已经被读取？
2. 把实验编号改掉后，最终回答是否变化？你通过哪两处输出核对了它？
3. 第二次请求为什么要同时保留 assistant 工具请求和 tool 工具结果？

## 参考资料

- [DeepSeek Tool Calls](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)

真实工具调用需要学习者在本地提供密钥验证。自动检查仅使用模拟模型响应，并实际读取公开练习文件，不会调用付费 API。
