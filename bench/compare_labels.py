"""把多个标注集放在一起比较：与工具的一致率、选择性指标、两两一致率矩阵。

为什么需要
----------
做过几轮标注之后，"哪个标注方和工具差多少""两个标注方彼此差多少"靠手算容易错、
也不可复现。本脚本是**换标注方 / 改规则后的汇总工具**，一条命令出全套对照表。

口径
----
- 判定单元 = (文件, 规则)，与金标准粒度一致；同一单元内若工具给出不一致的裁定，
  按"该单元未一致"处理（严格口径）。
- 三态标注集：直接比对 confirmed / false_positive / uncertain。
- 二元标注集（decision 列）：额外给出**选择性预测**指标 ——
  弃权率 / 不弃权时准确率 / 对「需改代码」(change) 的召回。弃权≠判错，必须分开看。

用法
----
    python bench/compare_labels.py out/labelset_A_*.csv out/labelset_B_*.csv \
        --self bench/golden_adjudication.json --sheet out/blind_labels.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine       # noqa: E402
from rtl_sentinel.llm import (MockProvider, OpenAICompatProvider,  # noqa: E402
                              Reasoner)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(BASE, "bench", "corpus")
SHEET = os.path.join(BASE, "out", "blind_labels.csv")

BINARY_MAP = {"change": "confirmed", "waive": "false_positive", "abstain": "uncertain"}
VERDICTS = ["confirmed", "false_positive", "uncertain"]


def load_sheet(path: str) -> Dict[int, Tuple[str, str]]:
    rows = csv.DictReader(open(path, encoding="utf-8-sig").read().splitlines())
    return {int(r["id"]): (r["file"], r["rule_id"]) for r in rows if (r.get("id") or "").strip().isdigit()}


def load_set(path: str) -> Dict[int, Tuple[str, str]]:
    """返回 {id: (三态verdict, 原始取值)}。"""
    rows = csv.DictReader(open(path, encoding="utf-8-sig").read().splitlines())
    out = {}
    for r in rows:
        i = (r.get("id") or "").strip()
        if not i.isdigit():
            continue
        raw = (r.get("verdict") or r.get("decision") or "").strip().lower()
        if not raw:
            continue
        out[int(i)] = (BINARY_MAP.get(raw, raw), raw)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="多标注集对照")
    ap.add_argument("labels", nargs="*", help="标注 CSV（含 id + verdict/decision）")
    ap.add_argument("--self", dest="self_path", help="自评金标准 json（可选）")
    ap.add_argument("--sheet", default=SHEET, help="id -> (文件, 规则) 映射表")
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--backend", choices=["offline", "llm"], default="offline",
                    help="offline=离线启发式后端；llm=系统的真实后端（读 RTL_SENTINEL_* 配置）")
    args = ap.parse_args()

    sheet = load_sheet(args.sheet)
    id2key = {i: k for i, k in sheet.items()}

    # 工具（离线后端）在每个判定单元上的裁定集合
    eng = ReviewEngine()
    res = eng.review_path(args.corpus)
    if args.backend == "llm":
        provider = OpenAICompatProvider(
            model=os.environ.get("RTL_SENTINEL_MODEL", "gpt-4o-mini"),
            base_url=os.environ.get("RTL_SENTINEL_BASE_URL"))
    else:
        provider = MockProvider()
    Reasoner(provider).adjudicate(res.findings, sources=res.sources)
    failed = sum(1 for f in res.findings if "LLM 调用失败" in f.llm_reason)
    tool: Dict[Tuple[str, str], set] = {}
    for f in res.findings:
        tool.setdefault((os.path.basename(f.file), f.rule_id), set()).add(f.llm_verdict)

    sets: Dict[str, Dict[int, Tuple[str, str]]] = {}
    for p in args.labels:
        sets[os.path.splitext(os.path.basename(p))[0]] = load_set(p)
    if args.self_path:
        g = {(x["file"], x["rule_id"]): x["verdict"]
             for x in json.load(open(args.self_path, encoding="utf-8"))["labels"]}
        selfset = {}
        for i, key in id2key.items():
            if key in g:
                selfset[i] = (g[key], g[key])
        if selfset:
            sets["自评金标准"] = selfset

    def unit_ok(key: Tuple[str, str], v: str) -> bool:
        return tool.get(key) == {v}

    print("# 多标注集对照（判定单元 = (文件, 规则)，推理后端 = %s%s）\n"
          % (provider.name, ("，调用失败 %d 条！" % failed) if failed else ""))
    print("| 标注集 | 覆盖单元 | 与工具一致率 | 内部分布 | 弃权率 | 不弃权时准确率 | change 召回 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for name, ds in sets.items():
        keys = [id2key[i] for i in ds if i in id2key]
        n = len(keys)
        agree = sum(1 for i in ds if i in id2key and unit_ok(id2key[i], ds[i][0]))
        dist = {}
        for i in ds:
            dist[ds[i][0]] = dist.get(ds[i][0], 0) + 1
        abst = [k for k in keys if "uncertain" in tool.get(k, set())]
        dec = [k for k in keys if "uncertain" not in tool.get(k, set())]
        sel = sum(1 for k in dec
                  if tool.get(k) == {next(v[0] for i, v in ds.items() if id2key.get(i) == k)})
        chg = [(i, ds[i][0]) for i in ds if id2key.get(i) and ds[i][1] == "change"]
        chg_hit = sum(1 for i, v in chg if tool.get(id2key[i]) == {"confirmed"})
        print("| %s | %d | %.1f%% (%d/%d) | %s | %.1f%% | %s | %s |" % (
            name, n, (agree / n * 100 if n else 0), agree, n,
            " ".join("%s:%d" % (k, v) for k, v in sorted(dist.items())),
            (len(abst) / n * 100 if n else 0),
            ("%.1f%% (%d/%d)" % (sel / len(dec) * 100, sel, len(dec))) if dec else "—",
            ("%.1f%% (%d/%d)" % (chg_hit / len(chg) * 100, chg_hit, len(chg))) if chg else "—"))

    names = list(sets)
    if len(names) > 1:
        print("\n## 标注方两两一致率\n")
        print("| | " + " | ".join(names) + " |")
        print("| --- |" + " --- |" * len(names))
        for a in names:
            cells = []
            for b in names:
                common = [i for i in sets[a] if i in sets[b]]
                ag = sum(1 for i in common if sets[a][i][0] == sets[b][i][0])
                cells.append("%.1f%%" % (ag / len(common) * 100) if common else "—")
            print("| **%s** | %s |" % (a, " | ".join(cells)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
