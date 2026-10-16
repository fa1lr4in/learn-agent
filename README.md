# learn-agent

一个正在逐步开发的本地编程 Agent，使用 Python 实现核心能力。

## 已实现功能

- 调用 DeepSeek 官方 API，发送问题并查看完整响应。
- 单独展示助手回答、停止原因和 token 用量，提示输出被截断的情况。
- 预览请求内容，无需密钥或网络调用。
- 从环境变量或终端读取 API Key，通过环境变量选择模型。
- 根据模型的工具请求，经用户确认后读取指定练习文件，再将结果交回模型。
- 检查工具名称、参数和允许读取的文件；拒绝确认时停止执行。

单次模型调用已由使用者真实运行成功；文件工具流程通过离线检查，真实模型工具调用仍待验证。

## 运行

需要 Python 3.11 或更新版本，无第三方依赖。

```powershell
# 预览请求
python chat.py --preview "你好"

# 调用模型；按本地终端提示输入密钥
python chat.py "请用一句话介绍你自己。"

# 运行文件工具演示
python tool_call.py
```

可以通过 `DEEPSEEK_API_KEY` 环境变量提供密钥，通过 `DEEPSEEK_MODEL` 指定模型；默认模型为 `deepseek-flash`。程序不会将密钥保存到文件。

文件工具演示只允许读取 `workspace/project.txt`。输入 `y` 后读取文件并将内容发送给 DeepSeek；其他输入会停止。当前最多两次模型请求。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试模拟模型响应，不调用真实 API，检查工具结果对应关系、拒绝执行和非法请求。

## 许可证

[MIT](LICENSE)。
