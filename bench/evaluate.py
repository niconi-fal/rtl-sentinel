"""评测脚本：在带人工标注的样例集上度量规则引擎的精确率 / 召回率 / F1，
并额外校验告警定位（行号）是否正确。

评测协议
--------
采用两级判定:

  ① 规则级 —— 判定单元 = (文件, 规则 ID)
       TP  期望命中且实际命中
       FP  未期望却命中           误报，审查工具最致命的缺陷
       FN  期望命中却未命中       漏报，可能直接导致流片失败

  ② 定位级 —— 判定单元 = (文件, 规则 ID, 行号) 的多重集
       为什么必须做: 规则级只回答"某规则在某文件上是否触发过"，发现不了
       「行号写错」「同一问题重复上报」「该报两处却只报一处」。而本项目把
       "行号精确"列为硬约束 —— 评测必须覆盖它，否则一行不改也能拿满分。
       标注格式: `GROUND TRUTH: R002@18 R002@19 R009@22 R009@22`
       （同一规则可重复出现，表示该文件上应有的多处命中；`@` 后为期望行号）。

样本集由含已知缺陷的设计 + 工业风格干净基线构成，全部标注以 GROUND TRUTH
注释内嵌在源码首部，保证评测可复现、可审计。干净基线的标注写作 `（无）`。
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine
from rtl_sentinel.index import ModuleIndex

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GT_RE = re.compile(r"GROUND\s+TRUTH\s*:\s*(.*)")
GT_ITEM_RE = re.compile(r"(R\d{3})(?:@(\d+))?")


def load_ground_truth(path: str) -> List[Tuple[str, Optional[int]]]:
    """读取源码首部的 GROUND TRUTH 标注。

    返回 [(规则 ID, 期望行号或 None), ...]。`R002@18` 带行号（参与定位级判定），
    `R004` 不带行号（该规则在本样本上只做规则级判定）。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(4000)
    m = GT_RE.search(head)
    if not m:
        return []
    return [(rid, int(ln) if ln else None)
            for rid, ln in GT_ITEM_RE.findall(m.group(1))]


def collect_samples() -> List[str]:
    paths: List[str] = []
    for kind in ("bad", "good"):
        folder = os.path.join(BASE, "samples", kind)
        if not os.path.isdir(folder):
            continue
        paths.extend(os.path.join(folder, f) for f in sorted(os.listdir(folder))
                     if f.lower().endswith((".v", ".sv")))
    return paths


def _ratio(hit: int, bad: int) -> float:
    return hit / (hit + bad) if (hit + bad) else 1.0


def main() -> int:
    eng = ReviewEngine()

    # 先对全样例集建立跨文件模块索引，使实例化相关规则（R009/R010）
    # 能在评测中被真正触发 —— 单文件审查无法看到被实例化模块的端口定义。
    all_paths = collect_samples()
    index = ModuleIndex()
    index.add_files(all_paths)

    rows = []
    tp = fp = fn = 0                  # 规则级计数
    l_tp = l_fp = l_fn = 0            # 定位级计数
    sev_total = {"error": 0, "warning": 0, "info": 0}

    for path in all_paths:
        rel = os.path.relpath(path, os.path.join(BASE, "samples")).replace("\\", "/")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        entries = load_ground_truth(path)
        expect_rules = {rid for rid, _ in entries}
        res = eng.review_source(src, path, index=index)
        got_rules = res.rule_ids()

        t, f_p, f_n = (len(expect_rules & got_rules),
                       len(got_rules - expect_rules),
                       len(expect_rules - got_rules))
        tp, fp, fn = tp + t, fp + f_p, fn + f_n

        # 定位级：只校验"带行号标注的规则"，用多重集比较（保留重复计数）。
        expected_loc = collections.Counter(
            (rid, ln) for rid, ln in entries if ln is not None)
        located = {rid for rid, ln in entries if ln is not None}
        reported_loc = collections.Counter(
            (f.rule_id, f.line) for f in res.findings if f.rule_id in located)
        l_tp += sum((expected_loc & reported_loc).values())
        missing = expected_loc - reported_loc
        extra = reported_loc - expected_loc
        l_fn += sum(missing.values())
        l_fp += sum(extra.values())

        for k, v in res.by_severity().items():
            sev_total[k] = sev_total.get(k, 0) + v
        rows.append((rel, entries, sorted(got_rules), t, f_p, f_n, missing, extra))

    prec, rec = _ratio(tp, fp), _ratio(tp, fn)
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    l_prec, l_rec = _ratio(l_tp, l_fp), _ratio(l_tp, l_fn)

    print("=" * 94)
    print("RTL-Sentinel 规则引擎评测报告")
    print("=" * 94)
    print("%-26s %-44s %4s %4s %4s" % ("样本", "期望(GROUND TRUTH)", "TP", "FP", "FN"))
    print("-" * 94)
    for name, entries, _got, t, f_p, f_n, missing, extra in rows:
        exp = " ".join("%s@%s" % (r, l) if l else r for r, l in entries) or "—"
        print("%-26s %-44s %4d %4d %4d" % (name, exp, t, f_p, f_n))
        for (rid, ln), n in sorted(missing.items()):
            print("      !! 定位缺失: %s@%s x%d" % (rid, ln, n))
        for (rid, ln), n in sorted(extra.items()):
            print("      !! 定位多余/错位: %s@%s x%d" % (rid, ln, n))
    print("-" * 94)
    print("① 规则级  TP=%-3d FP=%-3d FN=%-3d  Precision=%.1f%%  Recall=%.1f%%  F1=%.4f"
          % (tp, fp, fn, prec * 100, rec * 100, f1))
    print("② 定位级  TP=%-3d FP=%-3d FN=%-3d  Precision=%.1f%%  Recall=%.1f%%"
          % (l_tp, l_fp, l_fn, l_prec * 100, l_rec * 100))
    print("告警量分布: 错误 %d / 警告 %d / 提示 %d" %
          (sev_total["error"], sev_total["warning"], sev_total["info"]))
    print("=" * 94)

    out = {
        "unit": {"rule_level": "(file, rule_id)",
                 "location_level": "(file, rule_id, line)"},
        "samples": [
            {"sample": n,
             "expect": ["%s@%s" % (r, l) if l else r for r, l in e],
             "got_rules": g, "tp": t, "fp": f_p, "fn": f_n,
             "loc_missing": ["%s@%s x%d" % (r, l, c) for (r, l), c in sorted(m.items())],
             "loc_extra": ["%s@%s x%d" % (r, l, c) for (r, l), c in sorted(x.items())]}
            for n, e, g, t, f_p, f_n, m, x in rows
        ],
        "total": {"tp": tp, "fp": fp, "fn": fn,
                  "precision": round(prec, 4), "recall": round(rec, 4),
                  "f1": round(f1, 4)},
        "location": {"tp": l_tp, "fp": l_fp, "fn": l_fn,
                     "precision": round(l_prec, 4), "recall": round(l_rec, 4)},
        "severity": sev_total,
    }
    out_path = os.path.join(BASE, "bench", "eval_result.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("结果已保存 -> bench/eval_result.json")
    clean = (fp == 0 and fn == 0 and l_fp == 0 and l_fn == 0)
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
