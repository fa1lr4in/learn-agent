# 第三方组件与许可证

记录日期：2026-10-21。此清单根据固定依赖版本及其安装包元数据整理，用于追踪来源；各组件的原始许可证、版权和附加声明仍随组件保留。本项目源码的 MIT 许可证不替代第三方声明。

## Python 运行时与后端依赖

Windows 分发使用 CPython 3.13.9（PSF 及其附带第三方声明）。原文位于分发资源 backend/python/LICENSE.txt；下载来源与 SHA256 见 scripts/build_windows.py。

完整 Windows 环境固定在 [desktop/requirements-windows.txt](desktop/requirements-windows.txt)，共 35 个发行包。下表的名称、版本及许可证表达式来自这些发行包的 METADATA；多个许可证时保留上游表达式，不自行替上游选择。

| 发行包 | 固定版本 | 上游许可证元数据 |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.10.19 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.152.0 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.15.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx2 | 2.15.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| mcp | 2.3.0 | MIT |
| mcp-types | 2.3.0 | MIT |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| pycparser | 3.11 | BSD-3-Clause |
| pydantic | 2.14.1 | MIT |
| pydantic_core | 2.50.1 | MIT |
| PyJWT | 2.15.1 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| pywin32 | 312 | PSF |
| PyYAML | 6.0.3 | MIT |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.9.1 | MIT |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.9.0 | BSD-3-Clause |
| truststore | 0.10.5 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |

完整原文通常位于 `backend/python/Lib/site-packages/<发行包>.dist-info/licenses/`；pywin32 等发行包还在自身目录附带多份声明。cryptography 的表达式也不能替代其 wheel 中附带的其他声明；certifi、pywin32 等应保留各自原文。构建脚本安装完整 wheel，不移除许可证文件。

## Electron 与 Node 构建依赖

| 组件 | 固定版本 | 许可证及原文位置 |
| --- | --- | --- |
| Electron | 45.0.0 | Electron 为 MIT；运行时附带 LICENSE 与 LICENSES.chromium.html，后者记录 Chromium 及其他组件的声明 |
| electron-builder | 26.15.3 | MIT；开发依赖安装目录中的 LICENSE 文件 |

desktop/package-lock.json 记录 284 个 Node 包条目（包括嵌套、可选及构建依赖），不是 284 个全部进入安装包的运行时组件。按锁文件 license 字段统计如下；逐包的确切名称、版本、完整性校验及许可证表达式以 [锁文件](desktop/package-lock.json) 为准，原文随 npm 安装包保留。

| 锁文件中的许可证表达式 | 包条目数 |
| --- | --- |
| (MIT OR CC0-1.0) | 1 |
| (WTFPL OR MIT) | 1 |
| 0BSD | 1 |
| Apache-2.0 | 6 |
| BSD-2-Clause | 6 |
| BSD-3-Clause | 9 |
| BlueOak-1.0.0 | 8 |
| ISC | 36 |
| MIT | 213 |
| Python-2.0 | 1 |
| WTFPL | 1 |
| WTFPL OR ISC | 1 |

Electron 的两份声明文件由 electron-builder 随运行时保留；本清单随应用源码进入 app.asar。Node 构建依赖的许可证与 Electron 自带 Chromium 声明分别记录，不能用锁文件统计替代运行时的完整声明。NSIS 等构建工具与其来源由 electron-builder 的固定依赖提供，发布安装包前仍需核对实际产物。

## 更新方法

依赖更新时同步固定版本、安装到新的环境，核对实际发行包的 METADATA、LICENSE/COPYING/NOTICE 等原文，再更新本清单。添加新的打包资源时，确认构建产物保留所需的原始声明。此表是当前版本的来源记录，不是未来版本的自动合规结论。
