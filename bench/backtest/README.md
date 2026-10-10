# 真实 bug 回溯验证

把开源 RTL 项目**真实修过的 bug** 拿来做回溯：用修复前的版本跑本工具，看它能否指出
作者当年修掉的那个问题；再用修复后的版本确认告警**消失**。

## 为什么这一类证据最有说服力

- 受控样例集（`samples/`）是**我们自己植入**的缺陷；
- 真实语料（`bench/corpus/`）是成熟代码，**没有真缺陷可供确认**；
- 只有"真实项目的历史 bug + 修复前/后对照"，才能回答"它到底能不能发现问题"。

「**修复前报、修复后不报**」是最强的形式：它把告警与真实修复因果地绑在一起。

## 目录约定

    <case>/
        META.json   来源 / commit / 许可 / 缺陷描述 / 期望规则与行号
        pre/        修复前的文件（取自 commit^）
        post/       修复后的文件（取自 commit）

`META.json.status`：

- `hit`        —— 断言「修复前报出期望规则、修复后不再报」，不满足即失败
- `known_miss` —— 已知漏报，只记录不判失败（诚实披露能力边界）

## 复现

```bash
python bench/evaluate_backtest.py
```

取回文件的方式（便于复核来源真实性）：

```bash
git clone https://github.com/alexforencich/verilog-axis
git -C verilog-axis show <commit>^:rtl/axis_pipeline_fifo.v > pre/axis_pipeline_fifo.v
git -C verilog-axis show <commit>:rtl/axis_pipeline_fifo.v  > post/axis_pipeline_fifo.v
```

## 许可

本目录下的 Verilog 代码来自 [alexforencich/verilog-axis](https://github.com/alexforencich/verilog-axis)
（**MIT**）。版权归原作者所有，此处仅用于验证工具的检出能力，来源与 commit 均已注明。

## 结果

见 `docs/项目说明.md` 第十二节。
