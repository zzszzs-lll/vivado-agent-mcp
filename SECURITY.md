# 安全策略

## 支持范围

当前 `main` 和 Python 包 `0.10.x` 属于 Alpha 开发线。安全修复只保证合入当前开发主线；历史快照不单独维护。

本项目当前只信任 Windows 上、管理员明确审阅并纳入可信根的纯 RTL/XDC Project Mode 工作流。它不是操作系统沙箱，也不承诺抵御与 MCP/Vivado 处于同一 Windows 安全主体的恶意进程。

真实 FPGA、JTAG、Hardware Manager、烧录、ILA/VIO 和 flash cfgmem 未完成硬件验证，所有相关状态均为 `NOT_VALIDATED`。

## 报告漏洞

仓库公开并启用 GitHub Private vulnerability reporting 后，请使用 Security 页面中的 **Report a vulnerability** 私下提交报告。在该入口可用前，只能创建一个不包含利用代码、密钥、客户数据或敏感路径的最小公开 Issue，请求维护者建立私下沟通渠道。

报告建议包含：

- 受影响 commit 或版本。
- 前置条件和攻击者能力。
- 最小复现步骤。
- 实际影响和预期安全边界。
- 可行的缓解或修复建议。

请不要在公开 Issue、Discussion 或 Pull Request 中披露可直接利用的细节。维护者确认问题前不会要求你运行真实硬件或提供私有 FPGA 工程。

## 不属于安全承诺的范围

- 未经审阅的 HDL、Tcl、IP、BD、XCI、DCP 或第三方生成物。
- 非 Vivado 2021.2 的受信任执行。
- 同一 Windows 用户权限已被攻陷后的强隔离。
- 真实板卡、电气、引脚、电源、JTAG 或烧录正确性。
- 用户通过 `advanced` / `all` profile、Shell 或外部 Vivado 进程绕过 MCP 策略后的行为。
