# 第三方资源来源说明

本目录下的内容全部来自公开开源项目，**仅用于评测与对标验证**，不作商业用途。
版权与许可归原项目所有。

## lintllm/ —— 学术公开 benchmark

| 项 | 内容 |
| --- | --- |
| 项目 | [fangzhigang32/Static-Verilog-Analysis](https://github.com/fangzhigang32/Static-Verilog-Analysis) |
| 论文 | LintLLM: An Open-Source Verilog Linting Framework Based on Large Language Models |
| 会议 | GLSVLSI 2025（ACM）｜arXiv:2502.10815 |
| License | MIT |

**导入内容**

- `simple/` `medium/` `complex/` —— 各 30 个 Verilog 模块，每个注入 1 处已知缺陷
- `scripts/` —— 上游的 Prompt of Logic-Tree / Defect Tracker 实现（供参考其方法）
- `1_Benchmark_Simple.png` 等 —— 上游 README 附带的缺陷标注表（GT 原始来源）
- `GROUND_TRUTH.json` —— **本项目人工转录**的机器可读标注

**关于 GROUND_TRUTH.json 的说明**

上游标注以图片表格形式给出。转录时发现原表存在若干行号格式异常
（如 `6-5`、`10-9`、`2~>7`、`12~11~10`），已按区间规范化处理。
这部分数据存在不确定性，因此评估脚本以**模块级检出率**为主指标，
避免因 GT 行号不精确而误判工具能力。

**评估方式**

```bash
python bench/evaluate_lintllm.py
```

**上游论文报告的对比基线**

| 工具 | 正确率 ↑ | 误报率 ↓ |
| --- | --- | --- |
| 商业 EDA | 64.44% | 27.78% |
| Verilator | 62.22% | 32.22% |
| o1-mini + LintLLM | 83.33% | 12.22% |

> 注意口径差异：上游的"正确率"指**判定模块有无缺陷**的正确率（二分类任务）；
> 本工具不做二分类判定，输出的是具体缺陷位置与解释，
> 因此以"模块级缺陷定位检出率"作为参照指标。

## 引用

若需引用 LintLLM 及其 benchmark：

```bibtex
@inproceedings{Fang2025lintllm,
  author={Zhigang Fang and Renzhi Chen and Zhijie Yang and Yang Guo and Huadong Dai and Lei Wang},
  booktitle={Proceedings of the Great Lakes Symposium on VLSI 2025},
  title={{LintLLM}: An Open-Source Verilog Linting Framework Based on Large Language Models},
  year={2025},
  publisher={ACM},
  url={https://doi.org/10.1145/3716368.3735198}
}
```
