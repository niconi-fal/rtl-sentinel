"""把第三方回填的盲标结果并入金标准（bench/golden_adjudication.json）。

用法
----
    python bench/merge_blind_labels.py out/blind_labels.csv --rater "同学 A"
    python bench/merge_blind_labels.py out/blind_labels.csv --out /tmp/test.json  # 试跑

合并规则
--------
  · 已填 verdict 的判定单元：verdict/reason 换成人工结论，标记 source="human"
  · 未填的保持原样（source="self"），供对照
  · 打印"原自评 → 人工"的差异汇总，便于发现之前自评错在哪

改动请用 git commit 记录，以便追溯标注来源。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(BASE, "bench", "golden_adjudication.json")
OUT = os.path.join(BASE, "out")
VERDICTS = {"confirmed", "false_positive", "uncertain"}


def main() -> int:
    ap = argparse.ArgumentParser(description="合并盲标结果到金标准")
    ap.add_argument("csv_path", help="回填后的 blind_labels.csv")
    ap.add_argument("--rater", default="", help="标注者标识（写入金标准，便于审计）")
    ap.add_argument("--kind", choices=["human", "model"], default="human",
                    help="标注者类型：human=人工 / model=另一个 AI 模型（写入 source，供评测区分）")
    ap.add_argument("--out", default=GOLDEN, help="输出的金标准路径")
    ap.add_argument("--sheet", default=os.path.join(OUT, "blind_labels.csv"),
                    help="原始盲标清单；当输入 CSV 只含 id/verdict/reason（AI 标注常见）时，"
                         "用它按 id 回填 file/rule_id")
    args = ap.parse_args()

    with open(args.csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    # 输入可能只含 id/verdict/reason（例如另一个 AI 模型的输出）——按 id 从原始
    # 清单回填 file/rule_id/line，避免要求标注方复述这些字段（易抄错）。
    if any(not ((r.get("file") or "").strip() and (r.get("rule_id") or "").strip())
           for r in rows):
        if not os.path.isfile(args.sheet):
            print("!! 输入缺少 file/rule_id，且找不到原始清单 %s（用 --sheet 指定）"
                  % args.sheet)
            return 2
        with open(args.sheet, "r", encoding="utf-8-sig", newline="") as fh:
            sheet = {(s.get("id") or "").strip(): s for s in csv.DictReader(fh)}
        for r in rows:
            if not ((r.get("file") or "").strip() and (r.get("rule_id") or "").strip()):
                s = sheet.get((r.get("id") or "").strip())
                if s:
                    r["file"], r["rule_id"], r["line"] = (
                        s.get("file"), s.get("rule_id"), s.get("line"))

    filled, problems = [], []
    for r in rows:
        v = (r.get("verdict") or "").strip().lower()
        if not v:
            continue                                  # 未填 -> 跳过（允许部分标注）
        if v not in VERDICTS:
            problems.append("%s %s: 非法 verdict=%r" % (r.get("file"), r.get("rule_id"), v))
            continue
        filled.append((r, v))
    if problems:
        print("!! %d 行填写不合法，已跳过：" % len(problems))
        for p in problems[:10]:
            print("   " + p)
    if not filled:
        print("没有可合并的行（verdict 列为空？）。")
        return 2

    with open(GOLDEN, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    golden = {(g["file"], g["rule_id"]): g for g in doc["labels"]}

    changed = Counter()
    added = updated = 0
    for r, v in filled:
        key = ((r.get("file") or "").strip(), (r.get("rule_id") or "").strip())
        rec = golden.get(key)
        if rec is None:
            rec = {"file": key[0], "rule_id": key[1]}
            doc["labels"].append(rec)
            added += 1
        else:
            updated += 1
            changed[(rec.get("verdict", "(未标)"), v)] += 1
        rec["verdict"] = v
        rec["reason"] = (r.get("reason") or "").strip()
        rec["source"] = args.kind
        if args.rater:
            rec["rater"] = args.rater
        line = (r.get("line") or "").strip()
        if line.isdigit():
            rec["line"] = int(line)

    n_ext = sum(1 for g in doc["labels"] if g.get("source") not in (None, "self"))
    doc["source"] = ("混合来源：独立标注 %d 条（source=%s）+ 自评 %d 条（source=self）。"
                     % (n_ext, args.kind, len(doc["labels"]) - n_ext))
    if args.rater:
        doc["rater"] = args.rater

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)

    print("=" * 74)
    print("已合并 -> %s" % os.path.relpath(args.out, BASE).replace("\\", "/"))
    print("=" * 74)
    print("新增 %d 条 / 更新 %d 条；金标准现共 %d 条（其中独立标注 %d 条，来源=%s）"
          % (added, updated, len(doc["labels"]), n_ext, args.kind))
    if changed:
        print()
        diff = {k: c for k, c in changed.items() if k[0] != k[1]}
        if diff:
            print("与原自评的差异（原 → 人工）：")
            for (o, n), c in sorted(diff.items(), key=lambda kv: -kv[1]):
                print("  %-16s → %-16s %d 条" % (o, n, c))
        same = sum(c for (o, n), c in changed.items() if o == n)
        print("  （与原自评一致 %d 条）" % same)
    print()
    print("下一步：")
    print("  1) python bench/evaluate_adjudication.py     # 离线复评（免费）")
    print("  2) 再用真实 LLM 跑一次，得到「人工子集」的一致率")
    print("  3) git commit 记录本次标注（golden_adjudication.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
