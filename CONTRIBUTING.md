# 贡献指南

感谢你改进 Vivado Agent MCP。本项目当前是面向 Windows、Vivado 2021.2 和无板卡 Project Mode 的 Alpha 软件；贡献应保持范围清晰、证据可复现，并遵守既有安全边界。

## 开始之前

1. 阅读 [README.md](README.md) 和 [安全策略](SECURITY.md)。
2. 对缺陷或较大功能先创建 Issue，说明使用场景、预期行为、实际结果和最小复现。
3. 不要在 Issue、测试夹具或提交中加入许可证密钥、访问令牌、私钥、客户工程或其它敏感材料。

## 本地开发

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

运行基础验证：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall src
git diff --check
```

涉及 MCP schema、stdio、Vivado Tcl、simulation、run、report、signoff、audit 或 diagnostic evidence 时，应同时运行相应的定向测试。真实 Vivado 场景需要独立工作目录，并且输出必须留在 `test_use` 或 `.vivado_agent_mcp`。

## 变更要求

- 保持改动小、可审查，优先复用现有 registry、result contract 和 Vivado helper。
- 新增工具必须提供 schema、handler、统一响应字段、风险等级、profile 和契约测试。
- 任何危险行为都必须 fail-closed；不能通过 Shell 或任意 Tcl 执行面绕过门禁。
- 不把无板卡软件结果描述为真实硬件验证；硬件状态保持 `NOT_VALIDATED`。
- 不提交 `test_use`、`.vivado_agent_mcp`、Vivado/XSIM 运行产物、bitstream、日志、波形、构建目录或本机配置。
- 中文文档使用 UTF-8；用户可见行为、安装方式或安全边界变化同步更新 `README.md` 或 `SECURITY.md`。

## Pull Request

PR 描述至少包含：

- 要解决的问题和范围。
- 主要设计取舍及安全影响。
- 已运行的测试和结果。
- 未运行或无法验证的内容。
- 对文档、兼容性和硬件验证口径的影响。

维护者会优先检查行为回归、安全边界、证据新鲜度、错误恢复和测试充分性。

## Live Vivado qualification

普通 PR CI 不启动 Vivado。commit-bound live qualification 只能通过手动 self-hosted workflow 或等价本地入口运行，并且必须使用 immutable Git snapshot 构建出的 exact wheel/sdist 与 clean-install release manifest：

```powershell
python tests/live_qualification_runner.py --help
```

Qualification PR 还必须满足：

- runner 使用现有 S01 MCP stdio flow，不建立旁路 Tcl/Shell 执行面；
- record 通过 `qualification/qualification-record.schema.json` 对应的 fail-closed validator；
- commit、source archive、wheel、sdist、Vivado executable/build、fixture 和 evidence digest 全部匹配；
- 无 live 环境只能记录 `SKIPPED`、`UNAVAILABLE` 或 `unvalidated`，不得写成 `qualified`；
- `hardware_validation.status=NOT_VALIDATED` 且 `validated=false`；
- workflow artifact 不得包含用户目录、机器名、许可证路径、客户工程或原始大日志。

公开 matrix 位于 `qualification/matrix.json`。不要只凭本地运行口头说明修改为 `qualified`；应先保存并审阅对应 commit 的 qualification record。
