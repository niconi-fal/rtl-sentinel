"""生成"盲标"清单，供独立第三方对告警做裁定（用于替换自产金标准）。

为什么需要
----------
`bench/golden_adjudication.json` 的标注目前是"自产自评" —— 由本项目自己给出、
再用它评测本项目自己，属循环论证。要让"裁定一致率"成为可信数字，必须由
**没看过现有结论**的独立人员重新标注。

产出两个文件（内容一致）：
    out/blind_labels.md     给人阅读（含源码上下文，>> 标记告警行）
    out/blind_labels.csv    给人填写（Excel 可打开，只需填 verdict / reason 两列）

关键：脚本**不读** `golden_adjudication.json`，输出里也不含任何现有判定结论，
因此标注者是"盲"的。

用法
----
    python bench/prepare_blind_labels.py                     # 全部判定单元
    python bench/prepare_blind_labels.py --limit 30 --seed 0 # 抽样 30 个（可复现）
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine       # noqa: E402
from rtl_sentinel.llm import _context_snippet      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(BASE, "bench", "corpus")
OUT = os.path.join(BASE, "out")

INSTRUCTION = """\
# 盲标清单：RTL 告警裁定

请对下面每条告警独立判断：**它是否指向一个「需要修改的真实问题」？**

- `confirmed`      —— 是，这是真实缺陷，应当修复
- `false_positive` —— 否，这是设计者有意为之的合法写法（应豁免，不用改）
- `uncertain`      —— 证据不足，无法判断

注意：这些告警来自**成熟的开源工程代码**，作者都是有经验的工程师。
经验上这类代码里大部分告警并非缺陷，而是有意为之的写法 —— 请如实判断，
不要默认"有告警就是问题"，也不必刻意迎合工具。

请在 `out/blind_labels.csv` 的 `verdict` / `reason` 两列填写：
`verdict` 填上面三个词之一；`reason` 用一句话说明理由（引用具体信号名/行号最好）。
代码上下文见下，`>>` 标记的是告警行。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成盲标清单")
    ap.add_argument("--corpus", default=CORPUS, help="待审查语料目录")
    ap.add_argument("--outdir", default=OUT, help="输出目录")
    ap.add_argument("--limit", type=int, default=0, help="抽样个数；0 = 全部")
    ap.add_argument("--seed", type=int, default=0, help="抽样随机种子（保证可复现）")
    args = ap.parse_args()

    eng = ReviewEngine()
    res = eng.review_path(args.corpus)

    # 判定单元 = (文件, 规则)，与金标准粒度一致
    groups = {}
    for f in res.sorted_findings():
        groups.setdefault((os.path.basename(f.file), f.rule_id), []).append(f)
    keys = sorted(groups)
    if args.limit and args.limit < len(keys):
        random.Random(args.seed).shuffle(keys)
        keys = sorted(keys[:args.limit])

    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.outdir, "blind_labels.csv")
    md_path = os.path.join(args.outdir, "blind_labels.md")

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "file", "rule_id", "rule_name", "line",
                    "message", "verdict", "reason"])
        for i, key in enumerate(keys, 1):
            fs = groups[key]
            f0 = fs[0]
            more = ("（该文件另有 %d 条同类告警，属同一判定单元）" % (len(fs) - 1)
                    if len(fs) > 1 else "")
            w.writerow([i, key[0], key[1], f0.rule_name, f0.line, f0.message + more, "", ""])

    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(INSTRUCTION)
        fh.write("\n共 **%d** 个判定单元。\n\n---\n\n" % len(keys))
        for i, key in enumerate(keys, 1):
            fs = groups[key]
            f0 = fs[0]
            fh.write("### %d. `%s` L%d —— %s（%s）\n\n"
                     % (i, key[0], f0.line, key[1], f0.rule_name))
            fh.write("- 工具告警：%s\n" % f0.message)
            if len(fs) > 1:
                fh.write("- 该文件另有 %d 条同类告警（同一判定单元，一并判定）\n"
                         % (len(fs) - 1))
            ctx = _context_snippet(res.sources.get(f0.file, ""), f0.line)
            fh.write("\n```verilog\n%s\n```\n\n" % ctx)

    print("=" * 74)
    print("盲标清单已生成")
    print("=" * 74)
    print("判定单元 : %d 个（(文件, 规则) 去重）" % len(keys))
    print("给人阅读 : %s" % os.path.relpath(md_path, BASE).replace("\\", "/"))
    print("给人填写 : %s" % os.path.relpath(csv_path, BASE).replace("\\", "/"))
    print()
    print("下一步：把这两个文件发给一位独立的数字设计同学，请其填 CSV 的")
    print("       verdict / reason 两列，再用 bench/merge_blind_labels.py 合并。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
