# learn-agent

一个用 Python 从零搭起来的 **toy 编程 Agent**，带命令行、本地网页和 Windows 桌面界面。

这是我用来学习 Agent 开发的个人项目：尝试把模型 API、工具调用、执行循环、权限确认和图形界面串起来。**目前能力很薄弱，只在少量简单 Python 项目上做过验证。代码和交互都还很粗糙，不适合承担重要任务，也没有成熟编程 Agent 的通用性和可靠性。** 欢迎阅读源码、交流和提 Issue。

## 能做什么

- 在选定项目中浏览目录、搜索和读取文本文件。
- 预览修改差异，确认后编辑文件、执行 Python 和测试命令。
- 根据工具结果继续处理任务，展示流式回答和执行过程。
- 保存本地会话、继续聊天，支持拒绝操作和停止任务。
- 提供请求次数与 token 预算、上下文压缩等基础机制。
- CLI 中有项目指令、Skill、本地插件、只读子任务和实验性 MCP 示例。

这些都是学习性质的实现，功能存在不代表它能稳定完成复杂任务。

## Windows 安装版

从 [Releases](https://github.com/fa1lr4in/learn-agent/releases) 下载 `learn-agent-Setup-1.0.0-x64.exe`。安装包自带 Python 后端，不需要另外安装 Python 或 Node.js；运行用户项目所需的额外依赖仍需自行准备。

安装后打开应用，在设置里填写 **DeepSeek 官方 API** 配置，选择一个项目目录，再输入任务。建议先使用安装版附带的示例项目。模型调用会产生 API 费用。

安装器未签名，没有自动更新。目前只在开发者本机 Windows x64 上完成安装和真实模型验证，**Windows 沙盒、其他干净机器及其他系统均未验证**。校验文件 `SHA256SUMS.txt` 与安装器一起提供。

## 从源码开始

当前开发环境为 Windows x64、Python 3.13.9、Node.js 24.16.0（最低 22.12.0）和 Git。

```powershell
git clone https://github.com/fa1lr4in/learn-agent.git
Set-Location -LiteralPath .\learn-agent

python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r .\desktop\requirements-windows.txt
npm.cmd ci --prefix .\desktop

npm.cmd start --prefix .\desktop
```

首次启动在设置窗口配置模型与项目。桌面依赖由 `desktop/package-lock.json` 固定，Windows 后端依赖由 `desktop/requirements-windows.txt` 固定。

不调用模型，先查看请求格式：

```powershell
& .\.venv\Scripts\python.exe -B -X utf8 tool_call.py --preview "只预览请求，不执行工具。"
```

使用 CLI 检查示例项目：

```powershell
& .\.venv\Scripts\python.exe -B -X utf8 tool_call.py --workspace .\desktop\demo_project --auto-read --stream "读取 main.py，运行 python -m unittest -v test_stats.py，再运行 main.py，简要报告真实结果，不修改文件。"
```

CLI 从 `DEEPSEEK_API_KEY` 环境变量读取密钥，未设置时会在终端隐藏输入；桌面端通过设置窗口配置，并在 Windows 上加密保存在本机。不要把密钥写进代码或提交到 Git。更多 CLI 参数见 `tool_call.py --help`。

## 项目结构

| 位置 | 内容 |
| --- | --- |
| `learn_agent/` | 模型适配、Agent 循环、工具、权限、上下文和会话 |
| `chat.py`、`tool_call.py` | 模型聊天与 Agent 命令行入口 |
| `api.py`、`web/` | 本地后端与网页界面 |
| `desktop/` | Electron 桌面窗口、设置和示例项目 |
| `eval.py`、`evals/`、`tests/` | 固定任务评测与自动化检查 |
| `scripts/build_windows.py` | Windows 安装包构建 |

## 限制与安全边界

- **没有操作系统沙箱。** 批准的 Python、命令和可信插件使用当前账户的权限；项目目录检查不是执行隔离。
- 模型可能误读文件、修改错误、执行不合适的命令。先在临时项目或有版本控制的副本中试用，并仔细看审批内容。
- 文件内容会随模型请求发送给服务商；本地日志和历史也可能包含源码、文件内容与路径。
- MCP 仅实现了实验性 stdio 示例；Skill、插件和子任务主要从 CLI 启用，桌面端没有完整的扩展管理界面。
- 目前只评测了三类小型 Python 任务。三题各重复三次、9/9 通过，不能推导出对任意项目的成功率。

## 开发与反馈

[参与开发与测试](CONTRIBUTING.md) · [版本记录](CHANGELOG.md) · [安全说明](SECURITY.md) · [第三方组件](THIRD_PARTY_NOTICES.md) · [问题反馈](https://github.com/fa1lr4in/learn-agent/issues)

MIT License，见 [LICENSE](LICENSE)。
