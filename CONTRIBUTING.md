# 参与开发

learn-agent 当前主要验证 Windows x64。开发和 CI 使用 Python 3.13.9、Node.js 24.16.0；桌面依赖由 desktop/package-lock.json 固定。其他系统尚未承诺完整支持。

## 安装环境

从 [README 的源码启动步骤](README.md#从源码开始) 建立独立 .venv。Windows 完整开发与 CI 复用 desktop/requirements-windows.txt 的 35 项固定版本；根目录 requirements.txt 只固定 6 项直接依赖，用于可选功能的最小安装，不是完整环境锁。

`npm.cmd ci --prefix .\desktop` 会安装固定的 Node 依赖并下载 Electron。CI 只运行不需要 Electron 窗口的检查，使用 `--ignore-scripts` 跳过下载；这种安装之后需要再次执行正常的 npm ci 才能启动桌面窗口。

## 修改与检查

先明确要修复的行为，保持修改集中。修复缺陷时优先使用最小复现和必要的回归测试；不要为纯文案变动增加重复检查。

在仓库根目录运行以下命令，每一步失败都会停止脚本。检查不需要真实 API Key，模型回复使用测试替身；部分测试仍会启动本地后端、MCP 服务与临时 Python 子进程。

```powershell
& .\.venv\Scripts\python.exe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖存在冲突。' }

& .\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Python 检查失败。' }

npm.cmd test --prefix .\desktop
if ($LASTEXITCODE -ne 0) { throw '桌面检查失败。' }

node --test .\tests\test_web_client.mjs
if ($LASTEXITCODE -ne 0) { throw '前端检查失败。' }

& .\.venv\Scripts\python.exe -B -X utf8 tool_call.py --preview '只预览请求，不运行工具。'
if ($LASTEXITCODE -ne 0) { throw '请求预览失败。' }
```

GitHub 的 Windows checks 工作流在 main 的 push、Pull Request 和手动触发时执行上述流程。它不传入模型密钥，不运行付费评测，不构建或发布安装包。测试通过说明被覆盖的逻辑符合预期，真实模型与安装版仍需各自验收。

涉及界面交互、工具审批或生命周期的修改，还应按 README 在临时项目手动验证。不要让测试任务修改自己的业务项目。涉及 Windows 分发时，按 README 重新构建，再运行 `npm.cmd run test:package --prefix .\desktop` 和安装版实际验收；现有安装包不会自动随源码更新。

## 依赖与版本

- 修改直接 Python 依赖时，同步 requirements.txt 与 desktop/requirements-windows.txt，并在新的 .venv 中安装和运行 pip check；传递依赖也记录确切版本。
- 修改 Node 依赖时，同步 desktop/package.json 与 package-lock.json，使用 npm ci 验证。
- 核对发行包自身的许可证、版权与附加声明，更新 THIRD_PARTY_NOTICES.md；不要把本项目的 MIT 声明套用到第三方代码上。
- 用户可见变更写入 CHANGELOG.md 的 Unreleased 部分。实际发布时才增加版本条目、更新 desktop/package.json 及锁文件版本，并重新构建验收。

## 提交问题与改动

通过 [Issue](https://github.com/fa1lr4in/learn-agent/issues/new/choose) 提供可公开的最小复现、版本、预期和实际结果。日志中可能含源码、文件内容和本地路径，提交前只保留相关片段并脱敏。安全问题参考 [SECURITY.md](SECURITY.md)。

提交 Pull Request 时说明解决的问题、实际验证结果和剩余限制。不要提交 .env、密钥、logs、会话数据库、node_modules、.venv、build、dist 或个人练习与截图；被 .gitignore 忽略不代表文件从未进入历史，暂存前仍需检查实际内容。
