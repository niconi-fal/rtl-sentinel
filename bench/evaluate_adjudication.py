"""语义推理层的裁定准确率评测。

背景
----
规则引擎负责"发现问题"，语义推理层负责"裁定真伪 + 归因 + 给补丁"。
后者的能力此前从未被量化。本脚本用人工复核得到的金标准
（`bench/golden_adjudication.json`）度量任一推理后端与人工裁定的一致率。

裁定标准
--------
该告警是否指向一个「需要修改的真实问题」。对成熟开源项目而言，
"技术上正确但设计者有意为之"（如行为仿真模型、块内临时变量、
有意位宽截断）应判为 `false_positive`（需豁免而非修复）。

用法
----
    python bench/evaluate_adjudication.py                      # 离线启发式后端
    RTL_SENTINEL_API_KEY=sk-... python bench/evaluate_adjudication.py \\
        --model gpt-4o-mini --base-url https://api.openai.com/v1

设计意图：把"接真实 LLM"这件事变成一行命令，且评测结果可横向对比不同模型。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from typing import Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine          # noqa: E402
from rtl_sentinel.llm import (MockProvider,           # noqa: E402
                              OpenAICompatProvider, Reasoner)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(BASE, "bench", "corpus")
GOLDEN = os.path.join(BASE, "bench", "golden_adjudication.json")

VERDICTS = ["confirmed", "false_positive", "uncertain"]


def main() -> int:
    ap = argparse.ArgumentParser(description="语义推理层裁定准确率评测")
    # 默认值必须与环境变量 / 本地配置文件保持一致，否则会静默退回 OpenAI 默认端点
    # （api.openai.com 在国内通常不通，表现为"全部标 uncertain"的假失败）。
    ap.add_argument("--model",
                    default=os.environ.get("RTL_SENTINEL_MODEL", "gpt-4o-mini"),
                    help="LLM 模型名（也可用环境变量 RTL_SENTINEL_MODEL）")
    ap.add_argument("--base-url",
                    default=os.environ.get("RTL_SENTINEL_BASE_URL"),
                    help="OpenAI 兼容 API 地址（也可用环境变量 RTL_SENTINEL_BASE_URL）")
    ap.add_argument("--corpus", default=CORPUS, help="待审查语料目录")
    ap.add_argument("--golden", default=GOLDEN, help="金标准路径（默认 bench/golden_adjudication.json）")
    args = ap.parse_args()

    with open(args.golden, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    golden: Dict[Tuple[str, str], dict] = {
        (g["file"], g["rule_id"]): g for g in doc["labels"]}

    eng = ReviewEngine()
    res = eng.review_path(args.corpus)

    if args.base_url or os.environ.get("RTL_SENTINEL_API_KEY"):
        provider = OpenAICompatProvider(model=args.model, base_url=args.base_url)
    else:
        provider = MockProvider()
    Reasoner(provider).adjudicate(res.findings, sources=res.sources)

    # ---- 计数 ----
    # 判定单元 = (文件, 规则) —— 与金标准的粒度一致。
    # 注意：同一判定单元可能对应多条告警；旧口径按“告警条数”计数会把同一个
    # 标注重复计入多次（43 个独立标注会被算成 127 条），虚增权重、误导读者。
    # 因此这里以“独立判定单元”为准，“告警条数”仅作对照保留。
    by_key: Dict[Tuple[str, str], list] = collections.defaultdict(list)
    for f in res.sorted_findings():
        key = (os.path.basename(f.file), f.rule_id)
        if key in golden:
            by_key[key].append(f)

    confused: collections.Counter = collections.Counter()
    mismatches = []
    key_ok = n_find = hit_find = 0
    human_keys = human_ok = 0
    for key in sorted(by_key):
        g = golden[key]
        fs = by_key[key]
        all_ok = all(f.llm_verdict == g["verdict"] for f in fs)
        key_ok += all_ok
        if g.get("source") == "human":
            human_keys += 1
            human_ok += all_ok
        for f in fs:
            n_find += 1
            ok = (f.llm_verdict == g["verdict"])
            hit_find += ok
            confused[(g["verdict"], f.llm_verdict)] += 1
            if not ok:
                mismatches.append((key, g["verdict"], f.llm_verdict, f.line, f.message))

    print("=" * 88)
    print("语义推理层裁定评测")
    print("=" * 88)
    print("推理后端   : %s" % provider.name)
    print("审查语料   : %s" % os.path.relpath(args.corpus, BASE).replace("\\", "/"))
    print("语料规模   : %d 文件 / %d 条告警" % (len(res.files), len(res.findings)))
    print("判定单元   : %d 个（(文件, 规则) 去重 —— 与金标准粒度一致）" % len(by_key))
    print("告警条数   : %d 条（同一判定单元可对应多条告警）" % n_find)

    # 调用失败的条数：若占比很高，多半是端点/密钥配置问题，而非裁定能力问题
    failed = sum(1 for f in res.findings if "LLM 调用失败" in f.llm_reason)
    if failed:
        print("⚠ 调用失败   : %d 条 —— 结果不可信，请检查 --base-url / API Key 配置"
              % failed)

    k_acc = (key_ok / len(by_key) * 100.0) if by_key else 0.0
    f_acc = (hit_find / n_find * 100.0) if n_find else 0.0
    print()
    print("一致率（以判定单元为准）: %.1f%%  (%d/%d)" % (k_acc, key_ok, len(by_key)))
    print("一致率（按告警条数，旧口径，仅对照）: %.1f%%  (%d/%d)" % (f_acc, hit_find, n_find))
    if human_keys:
        print("独立人工标注子集: %d 个单元，一致率 %.1f%%  (%d/%d)"
              % (human_keys, human_ok / human_keys * 100.0, human_ok, human_keys))
    else:
        print("独立人工标注子集: 尚无（金标准仍为自产自评；见 bench/prepare_blind_labels.py）")
    print()
    print("混淆矩阵（人工 ↓ / 模型 →，按告警条数）")
    print("  %-16s %10s %14s %11s" % ("", *VERDICTS))
    for gv in VERDICTS:
        row = [confused.get((gv, mv), 0) for mv in VERDICTS]
        if sum(row) == 0:
            continue
        print("  %-16s %10d %14d %11d" % (gv, *row))
    print()
    if mismatches:
        print("不一致明细（前 12 条）")
        for (fn, rid), gv, mv, line, msg in mismatches[:12]:
            print("  %-24s %-5s L%-5d 人工=%-14s 模型=%-14s" % (fn, rid, line, gv, mv))
        print("  ... 共 %d 条不一致" % len(mismatches))
    print("=" * 88)
    print("说明: 离线启发式后端用规则化先验模拟语义判断，不依赖源码语义理解；")
    print("      接入真实 LLM 后以同一命令复现，即可量化语义推理层的实际增益。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
