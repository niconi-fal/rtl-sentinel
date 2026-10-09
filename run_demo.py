"""RTL-Sentinel 一键演示：扫描 -> 规则分析 -> 语义裁定 -> 报告生成。

用法:
    python run_demo.py
"""
from __future__ import annotations

import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from rtl_sentinel import report                      # noqa: E402
from rtl_sentinel.engine import ReviewEngine         # noqa: E402
from rtl_sentinel.llm import MockProvider, Reasoner  # noqa: E402

SAMPLES = os.path.join(BASE, "samples")
OUT = os.path.join(BASE, "out")


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(" " + title)
    print("=" * 78)


def main() -> int:
    banner("RTL-Sentinel 一键演示")
    eng = ReviewEngine()

    t0 = time.perf_counter()
    res = eng.review_path(SAMPLES)
    dt = (time.perf_counter() - t0) * 1000.0
    reasoner = Reasoner(MockProvider())
    reasoner.adjudicate(res.findings, sources=res.sources)

    total_lines = sum(len(s.split("\n")) for s in res.sources.values())
    print("扫描文件 %d 个 / 解析模块 %d 个 / RTL 源码 %d 行" %
          (len(res.files), len(res.modules), total_lines))
    print("静态分析耗时 %.1f ms   (%.2f ms/文件)" %
          (dt, dt / max(len(res.files), 1)))
    sev = res.by_severity()
    print("告警: 错误 %d / 警告 %d / 提示 %d      健康分 %.1f / 100" %
          (sev["error"], sev["warning"], sev["info"], res.health))

    banner("逐文件摘要")
    print("  %-30s %-40s %s" % ("文件", "命中规则", "严重度"))
    for f in sorted(res.files):
        fs = [x for x in res.sorted_findings() if x.file == f]
        ids = sorted({x.rule_id for x in fs})
        worst = fs[0].severity if fs else "-"
        print("  %-30s %-40s %s" %
              (os.path.relpath(f, BASE).replace("\\", "/"),
               ",".join(ids) or "无告警", worst))

    banner("语义推理裁定示例（前 3 条）")
    for f in res.sorted_findings()[:3]:
        print("  [%s] %s  L%d" % (f.rule_id, f.rule_name, f.line))
        print("    规则结论: %s" % f.message)
        print("    推理裁定: %s" % f.llm_reason)
        if f.llm_patch:
            print("    修复建议: %s" % f.llm_patch.replace("\n", "\n              "))
        print()

    banner("报告输出")
    os.makedirs(OUT, exist_ok=True)
    writers = {"html": report.to_html, "md": report.to_markdown,
               "json": report.to_json}
    for name, fn in writers.items():
        path = os.path.join(OUT, "report." + name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(fn(res))
        print("  已生成 %s" % os.path.relpath(path, BASE).replace("\\", "/"))

    banner("下一步")
    print("  评测指标   python bench/evaluate.py")
    print("  审查代码   python -m rtl_sentinel.cli <file.v|dir> [-f html -o out.html]")
    print("  离线模式   python -m rtl_sentinel.cli <dir> --no-llm")
    print("  接入真 LLM 设置 RTL_SENTINEL_API_KEY 后加 --base-url/--model 即可")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
