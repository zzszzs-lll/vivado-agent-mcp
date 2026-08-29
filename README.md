# Vivado Agent MCP

[![CI](https://github.com/zzszzs-lll/vivado-agent-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/zzszzs-lll/vivado-agent-mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11--3.12-blue.svg)](pyproject.toml)

面向 AMD Vivado 的 MCP server。它让支持 MCP 的 Agent 通过结构化、受策略约束的工具执行无板卡 Vivado Project Mode PL 开发流程，并保留可诊断、可复现的工程证据。

## 项目状态

| 项目 | 当前口径 |
|---|---|
| 软件成熟度 | Alpha，适合研究、开发和受监督试用 |
| Python 包版本 | `0.10.0` |
| 受信任执行策略 | 精确 `2021.2`，其它版本 fail-closed |
| 公开 commit-bound Live qualification | 源提交 [`4bfd3db`](qualification/records/4bfd3dbba05dc5fab9cdb048776a70de6d75f731/qualification-record.json) 为 `qualified`；后续提交不自动继承 |
| 操作系统 | Windows |
| 主要范围 | 纯 RTL/XDC 的 Vivado Project Mode 无板卡软件闭环 |
| 真实 FPGA/JTAG 验证 | `NOT_VALIDATED` |

公开源码不代表当前版本已达到无人监督生产部署标准。当前支持范围、安全边界和已知限制以本 README 与 [SECURITY.md](SECURITY.md) 为准。

## 实现与验证状态

| 能力 | 实现状态 | 当前验证证据 |
|---|---|---|
| MCP stdio、工具发现、结构化响应 | 已实现 | 单元测试、stdio 契约测试、GitHub Actions |
| 本地 Vivado GUI 与认证 Tcl 通道 | 已实现 | fake-session；`4bfd3db` commit-bound Vivado 2021.2 qualification record |
| 工程、fileset、top、语言、compile order | 已实现 | 单元测试、stateful fake-session workflow；已有工程 per-file 语义重建通过本地 Vivado 2021.2 定向 smoke |
| XSIM behavioral simulation 与失败诊断 | 已实现 | 解析/安全回归；`4bfd3db` 的 S01 真实 XSIM 软件资格记录，公开普通 CI 不启动 Vivado |
| synthesis、implementation、bitstream | 已实现受管调用和结果解析 | fake-session、场景 runner；`4bfd3db` 的真实 Vivado 2021.2 S01 软件资格记录 |
| timing、utilization、DRC、QoR、CDC、power | 已实现报告生成和解析接口 | fixtures、单元测试；`4bfd3db` 的 report/signoff/audit evidence digest |
| artifact、signoff、audit、diagnostic handoff | 已实现 | 契约测试、场景 runner、bundle 完整性与新鲜度校验；`4bfd3db` qualification evidence |
| Hardware Manager、JTAG、烧录、ILA/VIO | 实验接口，默认关闭 | 仅负路径和门禁测试；真实硬件 `NOT_VALIDATED` |

GitHub Actions 的绿色状态证明 Python、MCP 契约和发行物安装链路。仓库公开保存源提交 `4bfd3dbba05dc5fab9cdb048776a70de6d75f731` 的 commit-bound Vivado 2021.2 qualification record；该记录来自维护者 self-hosted Windows 环境，不表示普通 GitHub-hosted Runner 安装了 Vivado。任何后续 commit 都必须生成自己的 record，不能继承 `4bfd3db` 的资格；软件 qualification 也不表示真实 FPGA 硬件通过。

## Commit-bound Live Vivado Qualification

仓库提供第一版正式 qualification 合约，用于把一个 Git commit、immutable source archive、exact wheel/sdist、真实 Vivado executable/build、确定性 fixture 和最终 evidence digest 绑定到同一机器可验证记录：

- [Qualification record JSON Schema](qualification/qualification-record.schema.json)
- [Vivado qualification matrix](qualification/matrix.json)
- [`4bfd3db` public qualification record](qualification/records/4bfd3dbba05dc5fab9cdb048776a70de6d75f731/qualification-record.json)、[脱敏 evidence snapshots](qualification/records/4bfd3dbba05dc5fab9cdb048776a70de6d75f731/public-evidence/) 与 [validator result](qualification/records/4bfd3dbba05dc5fab9cdb048776a70de6d75f731/qualification-validation.json)
- 确定性 fixture：`minimal-counter-v1`，随 wheel 分发的 SystemVerilog RTL、自检查 testbench 和 XDC
- 本地 runner：`tests/live_qualification_runner.py`
- 手动 self-hosted workflow：`.github/workflows/live-qualification.yml`

资格状态含义：

| 状态 | 含义 |
|---|---|
| `trusted` | 版本/可执行文件符合管理员配置的执行策略，但尚未完成本 commit 的完整 live qualification |
| `qualified` | exact source/package、真实 Vivado build、S01 live 软件流和完整新鲜 evidence 均通过校验 |
| `compatible` | 存在兼容性观察证据，但不会自动获得 trusted 执行资格 |
| `unvalidated` | 尚无可接受的 commit-bound live evidence |
| `rejected` | live 运行失败、被中断，或身份/证据契约不成立 |

`qualified` 只证明无板卡 Project Mode 软件流。它不会把 `hardware_validation.status` 从 `NOT_VALIDATED` 改为真实板卡通过。tracked matrix 只在审阅 qualification record 后更新；普通 Python CI、mock、fake-session、doctor 或维护者口头 smoke 都不能产生 `qualified`。

## 安全边界

- MCP `list_tools` 定义、core/advanced/all profile、risk、MCP annotations、Agent catalog 和 workflow tags 由同一 `CapabilitySpec` 投影生成；`selftest` 会检查公开 annotations 是否与该契约一致。
- 默认 `core` profile 只暴露标准 workflow、诊断和恢复工具。
- 公共 `run_tcl` / `safe_tcl` 不能执行任意 Tcl，只保留 dry-run 分类。
- 已有工程通过 Vivado 原生 `open_project -read_only` 与 MCP policy 双重进入 inspection-only 模式；需要执行时，Agent 必须先采集三个 fileset 的受支持逐文件语义，再在独立路径创建并核对 MCP 管理的工作工程。
- trusted XSIM 只适用于管理员已审阅并显式放入可信根的纯 RTL 工程；它不是操作系统沙箱。
- 删除、重置和清理操作默认 dry-run，并要求 identity、intent 和固定确认。
- Hardware Manager、烧录、JTAG、ILA/VIO 和真实板卡行为不属于当前验证范围。
- `READY` 只表示相应的软件证据就绪，绝不表示真实 FPGA 硬件通过。

详细威胁边界见 [SECURITY.md](SECURITY.md)。

## 环境要求

- Windows 10/11。
- Python `3.11` 或 `3.12`。
- AMD Vivado `2021.2`。版本证明保留完整补丁号；包括 `2021.2.1` 在内的其它版本当前不会继承 `2021.2` 的受信任资格，而是 fail-closed。
- 一个独立、可写的 runtime 目录。
- 使用 XSIM 时，显式配置经过审阅的可信工程根目录。

## 从源码安装

```powershell
git clone https://github.com/zzszzs-lll/vivado-agent-mcp.git
cd vivado-agent-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

确认入口：

```powershell
.\.venv\Scripts\vivado-agent-mcp.exe --help
.\.venv\Scripts\vivado-agent-mcp.exe --version
```

不带参数的 `vivado-agent-mcp` 会启动 stdio MCP server，不是人工交互式 CLI。

## 首次自检

先检查 Python、runtime、Vivado 路径、XSIM companion tools 和有界启动探针：

```powershell
$env:VIVADO_PATH = "C:\Xilinx\Vivado\2021.2\bin\vivado.bat"
.\.venv\Scripts\vivado-agent-mcp.exe doctor
```

再检查 MCP stdio、工具发现、结构化响应、安全门禁和硬件边界：

```powershell
.\.venv\Scripts\vivado-agent-mcp.exe selftest `
  --output-dir .\.vivado_agent_mcp\selftest
```

`doctor` 和 `selftest` 通过不等于已完成真实工程或真实板卡验证。

## MCP 客户端配置

将下列路径替换为本机实际路径：

```json
{
  "mcpServers": {
    "vivado-agent": {
      "command": "C:/path/to/vivado-agent-mcp/.venv/Scripts/vivado-agent-mcp.exe",
      "args": [],
      "env": {
        "VIVADO_PATH": "C:/Xilinx/Vivado/2021.2/bin/vivado.bat",
        "VIVADO_AGENT_MCP_RUNTIME_DIR": "C:/vivado-agent-mcp-runtime/runtime",
        "VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS": "C:/fpga-work",
        "VIVADO_AGENT_MCP_TOOL_PROFILE": "core",
        "SystemRoot": "C:/WINDOWS",
        "WINDIR": "C:/WINDOWS"
      }
    }
  }
}
```

可直接参考：

- [完整配置模板](examples/mcp-config-full.json)
- [最小配置模板](examples/mcp-config-minimal.json)

关键环境变量：

| 变量 | 作用 |
|---|---|
| `VIVADO_PATH` | 服务器启动前配置的可信 `vivado.bat` 绝对路径；MCP 工具参数不能覆盖它 |
| `VIVADO_AGENT_MCP_RUNTIME_DIR` | MCP bootstrap、会话日志和 Vivado 临时文件的统一 runtime 根目录 |
| `VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS` | 允许 trusted XSIM 执行的本地工程根；Windows 多根用 `;` 分隔 |
| `VIVADO_AGENT_MCP_TOOL_PROFILE` | 工具暴露策略，普通用户保持 `core` |
| `SystemRoot` / `WINDIR` | 避免部分 MCP 宿主子进程缺少 Windows 基础环境变量 |

## Agent 使用协议

Agent 开始 Vivado 工作时应遵循以下顺序：

1. 调用 `get_tool_catalog`，确认当前 profile、工具和硬件边界。
2. 调用 `get_agent_workflows`，选择新工程、已有工程审计、仿真修复或 handoff 流程。
3. 按 recipe 的 `steps` 执行，并直接消费结构化 `next_actions`。
4. 遇到 `BLOCK`、`stop_required=true` 或安全门禁时停止，不使用 Shell 绕过。
5. bitstream 生成只代表构建完成；完整 Agent handoff 还需要 artifact、report、signoff、audit 和 diagnostic evidence。
6. 任何无板卡结果都必须保持 `hardware_validation.status=NOT_VALIDATED`。

`get_tool_catalog` 默认返回适合 Agent 路由的紧凑 CapabilitySpec 投影；需要审计 profile、workflow tags、状态前置条件和 dispatch lane 时，传入 `{"detail":"full"}`。

推荐从 `get_agent_workflows` 选择标准路径：新工程先完成语法与有限时长仿真，再异步启动 synthesis/implementation/bitstream 并轮询；已有工程默认只读检查；bitstream 后继续收集 artifact、report、signoff、audit 和 diagnostic bundle 才算完整 handoff。

本 MCP 是 Vivado 控制与诊断平面，不负责创建或修改 RTL、SystemVerilog testbench 等源码字节。新工程和源码修复流程要求 Agent 同时具备经过用户授权的文件读取与代码编辑能力；Vivado、XSIM、run、报告和安全门禁仍应通过本 MCP 执行，不应使用 Shell 绕过。

## 验证范围

- `python -m pytest`：Python 单元测试、解析器、安全门禁、fake session 与 MCP 契约。
- `tests/agent_stdio_regression.py`：只通过 MCP stdio 消费 catalog、workflow、`next_actions` 和诊断结果。
- `tests/agent_scenario_runner.py`：S00-S07 Agent 场景；默认不启动 Vivado，显式 `--include-live-vivado` 才运行本地软件流程。
- `tests/live_qualification_runner.py`：使用 exact wheel、source provenance 和 S01 MCP stdio live flow 生成 commit-bound qualification record；未显式授权 live 或环境不可用时只能生成 `unvalidated` / `unavailable` 结果。
- `doctor`：检查本机 Python、runtime 和由实际 `vivado -version` 输出证明的 Vivado 版本。
- `selftest`：检查已安装入口、stdio、工具目录、结构化响应和安全边界。
- GitHub CI 使用 `requirements/` 中按 Python 版本区分的精确版本与 SHA256 lock；发行物从 immutable `git archive HEAD` snapshot 构建，并逐字节核对 wheel 中的 Python package members。
- CI 使用 `source-provenance.json` 将当前 clean Git identity、逐字节 package manifest 与 exact wheel SHA 交叉绑定，在 Python 3.11/3.12 安装同一个 wheel，并验证 sdist；wheel、sdist、`SHA256SUMS`、provenance 和 smoke reports 作为短期 Actions artifact 保留。它们不是 tag、Release 或 PyPI 发布。
- 真实 FPGA、JTAG、烧录、ILA/VIO 不在当前验证范围内。

## Runtime 与工程产物

- runtime 临时内容统一写入 `VIVADO_AGENT_MCP_RUNTIME_DIR`，可用 `get_runtime_cache_status` 查看，并通过 `clean_runtime_cache` 的 dry-run 计划安全清理。
- 工程交付物只写入工程目录内的 `vmcp_artifacts`、`vmcp_reports`、`vmcp_signoff`、`vmcp_diagnostics` 和 `vmcp_constraints`。
- MCP 不会把工程交付物混入统一 runtime；runtime 清理也不会删除工程内 `vmcp_*` 结果。
- 当前 diagnostic bundle 是绑定原工程路径和文件哈希的 project-local reference index，不是可复制到另一台机器后独立验证的 portable archive。

## 常见问题

- `doctor` 找不到 Vivado：在启动 MCP server 或 doctor 前设置 `VIVADO_PATH` 为可信 `vivado.bat` 的绝对路径。可选 `--vivado-path` 只能重复声明同一个 canonical 文件身份，不能选择其它 executable。
- 路径看似是 `2021.2` 但仍被阻断：目录名只是提示；只有实际 `vivado -version` 输出可以证明版本。
- 实际版本是 `2021.2.1`：当前验证矩阵只覆盖精确的 `2021.2`，补丁版本需要单独资格验证，默认会被阻断。
- Vivado GUI 无法显示：MCP 宿主必须运行在可交互的 Windows 桌面会话中。
- 仿真被 VCD 门禁阻断：使用有限 `run_time`，优先保留 WDB，避免 testbench 无限 dump。
- 已有工程无法直接执行：`open_project` 使用 Vivado `-read_only` 并施加 MCP inspection-only policy；按 `next_actions` 采集 `sources_1`、`constrs_1`、`sim_1` 的 `file_specs`，在独立路径完成语义等价重建后再执行。
- `core` profile 看不到硬件工具：这是默认安全策略，且真实硬件当前未验证。

## 开发与验证

```powershell
python -m pytest
python -m compileall src
git diff --check
```

安装态 smoke、Agent scenario runner 和 Vivado live 验证入口见 [贡献指南](CONTRIBUTING.md)。

## 已知限制

- 当前受信任执行基线固定为 Windows 与 Vivado `2021.2`；其它 Vivado 版本默认 fail-closed。
- 受信任版本不等于已完成公开 qualification；以 [tracked qualification matrix](qualification/matrix.json) 和对应 workflow artifact 为准。
- 当前主流程是 Project Mode，不覆盖 Non-Project Mode。
- 已有工程默认按 inspection-only 边界接手，避免 Agent 无意修改外部工程。
- working-copy 重建只复现当前 allowlist 中的逐文件语义，包括 file type、library、global include、used-in、XDC processing order 和 scope；发现未知、缺失或重建后不一致时会 fail-closed。
- RTL/Testbench 的创建与修改依赖 Agent 宿主提供的受控文件编辑能力；本 MCP 当前不提供 HDL 源码 patch 工具。
- diagnostic handoff 当前限于原工程可访问的 project-local reference 模式，不支持跨机器 portable bundle。
- trusted XSIM 只适用于管理员已审阅的纯 RTL/XDC 工程，不构成恶意 HDL 的操作系统级隔离。
- 真实板卡、JTAG、烧录、ILA/VIO 和 flash cfgmem 尚未验证，相关状态必须保持 `NOT_VALIDATED`。
- 当前版本定位为 Alpha，建议在受监督、可回滚的工程副本上使用。

## 参与贡献

提交 Issue 或 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中披露利用细节。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。

本项目不包含或分发 AMD Vivado。用户需要自行取得、安装 Vivado，并遵守适用的 AMD 软件许可条款。

AMD、Vivado、Xilinx 及相关名称可能是其各自权利人的商标。本项目是独立开源项目，不隶属于 AMD，也不代表其官方认可或支持。
