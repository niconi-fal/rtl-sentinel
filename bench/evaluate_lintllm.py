"""在 LintLLM 公开 benchmark 上评估 RTL-Sentinel。

benchmark
---------
来源: https://github.com/fangzhigang32/Static-Verilog-Analysis （GLSVLSI 2025, MIT）
规模: 90 个 Verilog 模块（simple / medium / complex 各 30），每模块注入 1 处已知缺陷。
类型: 11 类 —— Operators / Syntax Structure / Signal Usage / Sensitivity List /
      Logic Synthesis / Bit Width Usage / Race or Hazard / Module Instances /
      Port Type / Reserved words / Combinational or Sequential

上游论文结果（可对比）
---------------------
| 工具                | 正确率 ↑ | 误报率 ↓ |
| 商业 EDA            | 64.44%   | 27.78%   |
| Verilator           | 62.22%   | 32.22%   |
| o1-mini + LintLLM   | 83.33%   | 12.22%   |

本脚本的定位
------------
**不是刷分，而是定位分析。** 该 benchmark 侧重语法与声明类缺陷，而
RTL-Sentinel 专注可综合 RTL 的设计缺陷（位宽、CDC、多驱动、跨文件实例化）。
因此预期检出率有限 —— 这个结果本身说明了工具的适用边界，
以及哪些缺陷类型值得后续补充。

指标定义
--------
模块级检出率 = 至少命中一处 GT 行的模块数 / 90
        （用模块级而非行级，因为上游表格部分行号格式异常，行级数据不可靠）
告警精确率   = 落在 GT 行上的告警数 / 总告警数
"""
from __future__ import annotations

import collections
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine      # noqa: E402
from rtl_sentinel.index import ModuleIndex        # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BM = os.path.join(BASE, "bench", "thirdparty", "lintllm")
GT_PATH = os.path.join(BM, "GROUND_TRUTH.json")

LEVELS = ("simple", "medium", "complex")


def main() -> int:
    import json

    with open(GT_PATH, "r", encoding="utf-8") as fh:
        gt_doc = json.load(fh)
    gt: Dict[str, dict] = {it["file"]: it for it in gt_doc["items"]}

    paths: List[str] = []
    for lv in LEVELS:
        folder = os.path.join(BM, lv)
        paths.extend(os.path.join(folder, f) for f in sorted(os.listdir(folder))
                     if f.endswith(".v"))

    index = ModuleIndex()
    index.add_files(paths)

    eng = ReviewEngine()
    findings_by_file: Dict[str, list] = {}
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        res = eng.review_source(src, path, index=index)
        findings_by_file[os.path.basename(path)] = res.findings

    hit_modules = tp_alerts = fp_alerts = 0
    per_type: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    per_level: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    per_rule: collections.Counter = collections.Counter()
    missed: List[tuple] = []

    for fn, item in gt.items():
        findings = findings_by_file.get(fn, [])
        lines = set(item["lines"])
        hit = [f for f in findings if f.line in lines]
        tp_alerts += len(hit)
        fp_alerts += len(findings) - len(hit)
        for f in hit:
            per_rule[f.rule_id] += 1
        ok = bool(hit)
        hit_modules += ok
        per_level[item["level"]][1] += 1
        per_type[item["type"]][1] += 1
        if ok:
            per_level[item["level"]][0] += 1
            per_type[item["type"]][0] += 1
        else:
            missed.append((fn, item["type"], sorted(lines)))

    n = len(gt)
    alerts = tp_alerts + fp_alerts
    print("=" * 84)
    print("RTL-Sentinel 在 LintLLM 公开 benchmark 上的评估")
    print("=" * 84)
    print("benchmark  : %d 个模块 / 11 类注入缺陷（simple·medium·complex 各 30）"
          % n)
    print("本工具告警 : %d 条" % alerts)
    print()
    print("模块级检出率 : %.1f%%  (%d/%d)" % (hit_modules / n * 100, hit_modules, n))
    if alerts:
        print("告警精确率   : %.1f%%  (%d/%d 条告警落在 GT 行上)"
              % (tp_alerts / alerts * 100, tp_alerts, alerts))
    print()
    print("按难度")
    print("  %-10s %8s %8s %s" % ("等级", "检出", "总数", "检出率"))
    for lv in LEVELS:
        h, t = per_level[lv]
        if t:
            print("  %-10s %8d %8d %.1f%%" % (lv, h, t, h / t * 100))
    print()
    print("按缺陷类型（本工具覆盖情况）")
    print("  %-30s %6s %6s %s" % ("缺陷类型", "检出", "总数", "比例"))
    for t, (h, tot) in sorted(per_type.items(), key=lambda kv: -kv[1][1]):
        print("  %-30s %6d %6d   %s" % (t, h, tot, ("%.0f%%" % (h / tot * 100)) if tot else "-"))
    print()
    if per_rule:
        print("命中所用规则:", dict(sorted(per_rule.items())))
        print()

    print("-" * 84)
    print("与上游论文结果对比（注意口径不同，仅供参照）")
    print("  %-24s %10s %10s" % ("工具", "正确率↑", "误报率↓"))
    print("  %-24s %10s %10s" % ("商业 EDA", "64.44%", "27.78%"))
    print("  %-24s %10s %10s" % ("Verilator", "62.22%", "32.22%"))
    print("  %-24s %10s %10s" % ("o1-mini + LintLLM", "83.33%", "12.22%"))
    print("  %-24s %10s %10s" % ("RTL-Sentinel（本工具）",
                                 "%.2f%%" % (hit_modules / n * 100),
                                 "%.2f%%" % ((fp_alerts / alerts * 100) if alerts else 0)))
    print()
    print("口径说明: 上游的『正确率』指判定模块有无缺陷的正确率（二分类任务）；")
    print("          本工具不做二分类判定，此处以『模块级缺陷定位检出率』作为参照。")
    print("          RTL-Sentinel 的定位是可综合设计缺陷的深度检查（含跨文件），")
    print("          与本 benchmark 侧重的语法/声明类缺陷不完全重合，详见调研报告。")
    print("=" * 84)
    print("未检出模块（%d 个）的类型分布:" % len(missed))
    cnt = collections.Counter(t for _, t, _ in missed)
    for t, c in cnt.most_common():
        print("  %-30s %d" % (t, c))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
