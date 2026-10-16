# learn-agent

通过逐步开发一个编程 Agent，学习模型调用、工具执行、上下文、会话、权限和桌面应用工程。项目当前包含两份教学示例：单次模型调用，以及一次经过确认的文件读取工具调用。

正式课程已重新规划为 **36 节主线＋4 节进阶**，从第 1 节重新开始。完整目标、逐课产物、验收标准和公众号系列见 [课程总纲](CURRICULUM.md)。现有两个脚本是已准备的教学样例，不代表学习者已掌握对应内容。

每次迭代都需解释文件与关键函数的变化、执行路径和验证结果；每个产品里程碑交付一篇完整公众号正文草稿。协作规则记录在 [AGENTS.md](AGENTS.md)。

长期目标：输入一个代码任务，由程序组织模型与工具完成读取文件、修改代码、执行测试，并展示和保存任务过程。

第二课能够根据模型请求读取公开练习文件，并把结果交回模型。自主循环将在后续课程中实现。

## 运行第一课

需要 Python 3.11 或更新版本，仅使用 Python 标准库，无需安装第三方依赖。

先进入项目目录，查看请求结构：

```powershell
python chat.py --preview
```

连接 DeepSeek 官方 API：

```powershell
python chat.py "请用两句话解释什么是 Agent。"
```

程序会在当前本地终端中提示输入 API Key，输入时不显示字符。已有 `DEEPSEEK_API_KEY` 环境变量时，程序会使用该变量。密钥只用于本次请求的请求头，不写入源文件，不打印，也不保存到磁盘。

程序会把输入消息发送给 DeepSeek；默认使用 `deepseek-flash`，关闭思考模式，单次输出最多 512 token。每次真实运行发送一次请求，实际用量由响应中的 `usage` 返回。预览模式不连接 API。

如果你的账号需要使用其他模型标识，可以在 PowerShell 中设置后再运行：

```powershell
$env:DEEPSEEK_MODEL = "你的模型标识"
```

第一课完整讲解与练习见 [lessons/01-model-call.md](lessons/01-model-call.md)。

## 运行第二课

```powershell
python tool_call.py --preview
python tool_call.py
```

第二课复用第一课的模型请求和密钥输入函数，先显示模型提出的工具请求。输入 `y` 后，程序读取 `workspace/project.txt` 并把内容发送给 DeepSeek，再显示回答。输入其他内容则停止。本课只允许这个练习文件，最多发出两次模型请求。

讲解与实验见 [lessons/02-tool-call.md](lessons/02-tool-call.md)。验证工具结果对应关系、拒绝执行和参数检查：

```powershell
python -m unittest discover -s tests -v
```

这些自动检查模拟模型响应，不连接真实 API；读取使用本地公开练习文件。

## 学习过程

1. 观察一次模型调用，理解消息与响应。
2. 手动执行模型提出的一个工具调用。
3. 实现自动工具循环与停止条件。
4. 加入文件搜索、编辑、测试执行和权限控制。
5. 保存会话，控制上下文，处理错误与恢复。
6. 实现网页界面和 Windows 桌面端。
7. 用固定任务评测，并按需要接入 MCP 等扩展。

Python 实现是学习主线的起点。后续对照 DeepSeek Harness 的选定模块理解工程设计，再参考 OpenCode 的桌面产品实现。

每一课完成一个可运行版本、一次独立修改和一份实验记录。项目从第一课开始在 GitHub 公开开发，逐步形成可用的 Agent。

## 当前验证范围

第一课已通过语法、离线请求预览和请求/响应处理检查；学习者于 2026-10-16 反馈真实模型调用成功。第二课提供离线自动检查，真实工具调用仍需学习者在本地终端提供自己的密钥验证。预览模式不联网、不提示输入密钥。

## 参考资料

- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)
- [DeepSeek 多轮对话](https://api-docs.deepseek.com/zh-cn/guides/multi_round_chat/)
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [OpenCode](https://github.com/anomalyco/opencode)

## 许可证

[MIT](LICENSE)。
