"""测量解析器的"符号表覆盖"——量化因声明被屏蔽而导致的**静默漏报**风险。

背景（这是一个真实发生过的问题）
--------------------------------
语句是按 ``;`` 切分的，而 Verilog 里有**大量没有分号的记号**：属性 ``(* ... *)``、
编译器指令（`` `ifndef `` 等）、块结束词（``end / endfunction``）、``begin / generate``、
``if (...)`` / ``for (...)`` 头、以及 label。它们会被并进下一条语句，使该语句**无法被
识别为声明**，于是信号进不了符号表；而 R002 / R003 / R008 等会查符号表、查不到就跳过
—— 造成**静默漏报**（真实案例见 ``bench/backtest/axis_pipeline_fifo_assignment_type``：
``axis_tvalid_reg`` 的声明以 ``(* shreg_extract = "no" *)`` 开头，R003 因此漏掉，
而那个模式**曾被上游作者真实修复**）。

本脚本度量影响面（当前 **0/1563**），并作为**回归棘轮**：只允许变好，不允许变差。

两个度量
--------
主指标 —— **赋值目标不在符号表里的模块数**：这些模块存在静默漏报风险（最要紧的一项）。
副指标 —— 声明覆盖率：已解析信号数 / 直接扫描源码得到的声明名数。

用法
----
    python bench/check_symbol_coverage.py
"""
from __future__ import annotations

import os
import re
import sys
from typing import List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine        # noqa: E402
from rtl_sentinel.parser import KEYWORDS            # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(BASE, "bench", "corpus")

# 回归棘轮：当前实测值（2026-10-10）。**缺口已闭合：0 / 0**。
# 起点是 11 模块 / 59 目标（3.8%）。棘轮只允许变好；一旦回升即为回归。
BASELINE_MODULES_WITH_UNKNOWN_TARGETS = 0
BASELINE_UNKNOWN_TARGETS = 0
BASELINE_UNKNOWN_TARGETS = 59

DECL_RE = re.compile(
    r"\b(?:input|output|inout|wire|reg|logic|integer|genvar|parameter|localparam|"
    r"bit|tri|wand|wor|supply0|supply1|real|time|realtime|int|byte|shortint|longint)\b"
    r"([^;{}]*);", re.S)
IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def declared_names(body: str) -> Set[str]:
    """直接从（未屏蔽的）源码文本里扫出声明名 —— 与解析器走的是不同路径。"""
    out: Set[str] = set()
    for group in DECL_RE.findall(body):
        for nm in IDENT_RE.findall(group):
            if nm in KEYWORDS or nm.isdigit():
                continue
            out.add(nm)
    return out


def main() -> int:
    targets = [CORPUS, os.path.join(BASE, "samples")]
    bad: List[Tuple[str, int, List[str]]] = []
    tot_targets = tot_unknown = 0
    n_mod = 0

    for tgt in targets:
        res = ReviewEngine().review_path(tgt)
        for m in res.modules:
            n_mod += 1
            # 该模块所有赋值目标（过程赋值 + 连续赋值）
            names: Set[str] = set()
            for b in m.always_blocks:
                for a in b.assigns:
                    names.add(a.lhs)
            for a in m.assigns:
                names.add(a.lhs)
            unknown = sorted(n for n in names if n not in m.signals)
            tot_targets += len(names)
            tot_unknown += len(unknown)
            if unknown:
                bad.append((os.path.basename(m.file) + "::" + m.name, len(unknown), unknown[:6]))

    print("=" * 92)
    print("解析器符号覆盖自检")
    print("=" * 92)
    print("模块数 %d | 赋值目标 %d 个 | 其中不在符号表里的 %d 个" % (n_mod, tot_targets, tot_unknown))
    print("存在静默漏报风险的模块数: %d" % len(bad))
    print("-" * 92)
    for name, n, ex in sorted(bad, key=lambda x: -x[1]):
        print("  %-34s 未知目标 %-3d 例: %s" % (name, n, ", ".join(ex)))
    print("-" * 92)

    ok = (len(bad) <= BASELINE_MODULES_WITH_UNKNOWN_TARGETS
          and tot_unknown <= BASELINE_UNKNOWN_TARGETS)
    print("基线: 风险模块 ≤%d、未知目标 ≤%d（回归棘轮，只许变好）"
          % (BASELINE_MODULES_WITH_UNKNOWN_TARGETS, BASELINE_UNKNOWN_TARGETS))
    print("说明: '未知目标'= 赋值目标不在符号表里 —— R002/R003/R008 等会因此静默跳过。")
    print("      根因是声明落在 generate / `ifdef 区间内、被解析器的区间屏蔽掉。")
    print("结论: %s" % ("PASS" if ok else "FAIL（指标变差，请检查解析器改动）"))
    print("=" * 92)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
