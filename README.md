# RTL-Sentinel

**面向 Verilog 的双引擎静态审查工具** —— 确定性规则引擎负责"发现问题"，
LLM 负责"裁定与解释"。**零第三方依赖**，一条命令即可运行。

[![RTL Sentinel Regression](https://github.com/niconi-fal/rtl-sentinel/actions/workflows/regression.yml/badge.svg?branch=main)](https://github.com/niconi-fal/rtl-sentinel/actions/workflows/regression.yml)

```bash
python run_demo.py        # 一键演示，生成 out/report.html
```

## 为什么做这个

RTL 代码审查依赖昂贵的商业 Lint 工具（SpyGlass 等），且只报"哪里错"不解释
"为什么、怎么改"；直接用大模型审代码，则在位宽算术与数据流追踪上系统性出错，
且输出不可复现。

RTL-Sentinel 的核心判断：**"发现问题"是符号计算，必须由确定性规则承担；
"解释问题"是语义理解，才交给大模型。**

| 环节 | 承担者 | 关键约束 |
| --- | --- | --- |
| 找出问题 | 规则引擎（13 条规则） | 零随机、可复现、行号精确、2.7 ms/文件 |
| 裁定/解释/补丁 | LLM（可插拔后端） | **只能裁定已有告警，禁止发明新告警** —— 幻觉被结构性排除 |

## 实测结果

**① 真实代码泛化验证**（3 个开源仓库，42 文件 / 15151 行，含 RISC-V 软核 picorv32）

| 指标 | 首轮 | 修复后 |
| --- | --- | --- |
| error 级告警 | 397 | **27（−93%）** |
| 告警总数 | 491 | **128（−74%）** |

首轮暴露 7 类系统性误报，逐一定位根因并修复（跨文件端口方向索引、`<=`
双重语义歧义、数组下标 vs 位选、generate 互斥分支等），受控回归全程无退化。

**② 真实 bug 回溯**：取开源项目真实修复提交做修复前/后对照，2 例满足
"修复前命中、修复后消失"；并对 6 个仓库 69 个修复提交量化规则射程（4.3%），
主动划定工具能力边界。复现：`python bench/evaluate_backtest.py`

**③ 外部交叉验证**：Verilator 独立复核缺陷样例（LATCH / MULTIDRIVEN /
BLKANDNBLK 佐证），已接入 CI 门禁。`python bench/check_verilator.py`

**④ LLM 语义裁定实测**（DeepSeek，判定单元口径）：一致率 **73.8%（31/42）**；
重要工程发现：**上下文供给质量决定 LLM 表现** —— 单行代码 40.9% → 补充
源码上下文后 **88.2%**。

> **口径与可复现性（重要）**：金标准目前是**自产自评**（见「局限」）；更关键的是实测发现
> 该一致率**强依赖标注者** —— 同一批 42 题换标注者（不同模型 / 提示词）后在 **45%~96%**
> 之间摆动。因此本项目**不主张单一一致率数字**。改用**二元协议**（改代码 / 标豁免）后，
> 标注者彼此一致率从 **47.6% 提升到 83.3%** —— 说明「要不要弃权」才是分歧之源。
> 复现：`python bench/compare_labels.py`

**⑤ 第三方基准**（GLSVLSI 2025 LintLLM benchmark，90 模块）：可综合设计缺陷类
检出 44%（敏感列表）/ 38%（实例化），语法声明类 0% —— 与纯 LLM 路线
（直接检测 72.2%）**互补而非竞争**：规则引擎快 4500 倍、零成本、可复现、可进 CI 门禁。

**⑥ 受控回归**：12 文件评测集（GROUND TRUTH 内嵌标注），规则级 24/24、
定位级（行号）35/35。该指标仅验证规则逻辑正确性，不代表真实工程表现。

**⑦ 解析器符号覆盖自检**：把「声明因**无分号记号**（属性 `(* *)`、编译器指令、
`end*`、`for` 头、`begin : label`）被并进下一条语句」导致的**静默漏报**缺口从
**3.8% 压到 0**（受影响模块 11 → 0）；过程中还暴露并修掉 8 条被掩盖的假多驱动，
既有告警行号零偏移。`python bench/check_symbol_coverage.py`（回归棘轮，基线 0/0）

> **证据链一览**（每条主张 ↔ 对应证据 ↔ 复现命令，按证据强度分三层）见
> [docs/项目说明.md](docs/项目说明.md) 开头第一节；技术细节见
> [docs/架构设计.md](docs/架构设计.md)。

## 规则清单

| ID | 规则 | 严重度 | 说明 |
| --- | --- | --- | --- |
| R001 | 推断锁存器 | warning | 组合块分支未覆盖全（无终结 else / case 无 default） |
| R002 | 位宽截断 | error | 两侧位宽不符；含常量值位宽推断，可抓 `reg[3:0] x; x<=20;` |
| R003 | 阻塞赋值误用 | error | 时序块内对寄存器使用 `=`（已排除 for 循环头） |
| R004 | 多驱动 | error | 多 always / 多 assign / 实例输出同时驱动 |
| R005 | 敏感列表不完整 | warning | 组合块漏列被读信号（`@*` 自动豁免） |
| R006 | 跨时钟域未同步 | warning | 信号跨时钟域被直接采样 |
| R007 | 不可综合结构 | error | `initial` / `#N` 延时 / `$display` / `fork-join` 等 |
| R008 | 未使用信号 | info | 只写不读；条件编译块内声明自动豁免 |
| R009 | 实例端口位宽不匹配 | error | 跨文件比对实例连接与端口定义 |
| R010 | 实例端口未连接 | warning | 实例化漏连输入端口 |
| R011 | 组合逻辑误用非阻塞 | warning | 组合块内 `<=`，仿真与综合行为不一致 |
| R012 | 赋值运算符误用 | error | `assign x == y;` 赋值误写成比较 |
| R013 | 实例化连接方式混用 | error | 同一实例混用按名与按位置连接（LRM 禁止） |

## 快速开始

```bash
# 审查单文件 / 整目录
python -m rtl_sentinel.cli samples/bad/uart_tx_buggy.v
python -m rtl_sentinel.cli rtl/ -f html -o report.html

# 只跑确定性规则（CI 场景，退出码 0/1/2 可直接做门禁）
python -m rtl_sentinel.cli rtl/ --no-llm --min-severity error

# 接入任意 OpenAI 兼容后端（云端 / vLLM / Ollama）
export RTL_SENTINEL_API_KEY=sk-xxx
python -m rtl_sentinel.cli rtl/ --base-url http://localhost:11434/v1 --model qwen2.5-coder

# 复现评测与一键回归
python bench/evaluate.py          # 受控评测（P/R/F1 + 行号级校验）
python bench/run_all.py           # 受控集 + 真实语料基线 + Verilator 交叉验证
```

环境：Python 3.8+，**无第三方依赖**（仅标准库）。

## 架构

```
 .v / .sv ─▶ 解析层 parser.py     注释剥离(保行号) → 模块边界 → 块配平 → 语句级提取
          ─▶ 规则层 rules.py     13 条确定性规则 + 跨文件模块索引(index.py)
          ─▶ 语义推理层 llm.py   输入=告警+源码片段；输出={裁定,根因,补丁,验证方法}
          ─▶ 报告层 report.py    Text / MD / HTML / JSON / SARIF 2.1.0
```

解析器采用启发式管线而非完整 IEEE 1364 语法分析：零依赖、对残缺代码不抛异常、
行号与原文件严格一致；代价是放弃完整语法树（`generate` 展开等，见局限）。

## 豁免机制与 CI 集成

真实工程中约 79% 的规则告警是"有意为之"而非缺陷。集中式豁免配置
`.rtlsentinel.json`：**强制带理由、豁免不隐藏**（被豁免告警仍出现在报告中，
SARIF 以 `suppressions` 保留）。真实语料应用 14 条豁免后健康分 17.0 → **79.9**。

```json
{ "waivers": [ { "file": "spiflash.v", "rule": "R004", "reason": "行为仿真模型，非综合目标" } ] }
```

SARIF 输出可被 GitHub Code Scanning 消费，审查结论以行内批注出现在 PR 上；
示例见 `.github/ci-examples/rtl-sentinel.yml`。

## 局限（主动披露）

- 支持 Verilog-2001/2005 常用子集；不支持 UDP、宏展开、完整 SystemVerilog；
  `generate` 仅用于抑制表观多驱动，未做分支语义展开。
- R006 仅同模块内 CDC，依赖 `_sync`/`_gray` 命名约定；R002 不做完整表达式位宽传播。
- LLM 裁定金标准目前为自产自评（已知方法论短板，独立盲标工具已随附）。

路线图：独立盲标 / `generate` 展开 / 跨模块 CDC / Yosys 联动 / VS Code 插件。

## 目录

```
rtl_sentinel/   核心包（10 文件，约 3500 行，零第三方依赖）
samples/        评测样例（8 个含缺陷设计 + 4 个干净基线，GROUND TRUTH 内嵌行号）
bench/          评测脚本（两级评测 / 裁定 / 交叉验证 / 真实 bug 回溯 / 符号覆盖棘轮）
docs/           项目说明 / 架构设计 / 相关工作调研
.github/        自身 CI（离线回归 + Verilator 交叉验证）+ CI 集成示例
```

## 许可

MIT，见 [LICENSE](LICENSE)。`bench/` 内第三方语料保留其原 license。
