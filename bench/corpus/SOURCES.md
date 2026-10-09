# 真实代码验证语料来源

`bench/corpus/` 下的 Verilog 代码全部来自公开开源项目，**仅用于验证 RTL-Sentinel
在真实工程代码上的泛化表现**，不作任何商业用途。版权与许可归原项目所有。

| 目录 | 来源 | License | 内容 |
| --- | --- | --- | --- |
| `picorv32/` | [YosysHQ/picorv32](https://github.com/YosysHQ/picorv32) | ISC | 32 位 RISC-V 软核，含 picosoc 外设（simpleuart / spimemio / spiflash） |
| `verilog-uart/` | [alexforencich/verilog-uart](https://github.com/alexforencich/verilog-uart) | MIT | 工业风格 UART 收发器 |
| `verilog-axis/` | [alexforencich/verilog-axis](https://github.com/alexforencich/verilog-axis) | MIT | AXI-Stream 基础设施（FIFO / 仲裁 / 位宽转换 / 帧处理 / COBS 编解码） |

**导入筛选**：仅保留可综合的 RTL 源文件（`rtl/` 目录与核心模块），
剔除了各开发板的 example 目录与 `*_tb.v` testbench，以聚焦综合目标代码。

**规模**：42 个文件 / 15151 行 Verilog。

**验证结论**（详见 README 第 4.2 节）

| 指标 | 首轮 | 修复后 |
| --- | --- | --- |
| 告警总数 | 491 | 127 |
| error 级 | 397 | 27 |
| R004 多驱动（误报重灾区） | 215 | 8 |

剩余 8 条 R004 全部来自 `spiflash.v` —— 该文件是 SPI Flash 的**行为仿真模型**
（用多个 `always` 块驱动同一组变量模拟器件行为），并非综合目标代码，告警本身正确。

**复现方式**

```bash
python -m rtl_sentinel.cli bench/corpus -f html -o out/corpus_report.html
python -m rtl_sentinel.cli bench/corpus --no-llm --min-severity warning
```
