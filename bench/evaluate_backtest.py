"""真实 bug 回溯验证：拿开源项目"真实修过的 bug"，回到修复前跑本工具。

为什么这是最强的一类证据
------------------------
受控样例集是"我们植入的缺陷"，语料是"成熟代码（没有真缺陷可确认）"。
真正能回答"它到底能不能发现问题"的，是**真实项目的历史 bug**：
把某个开源 RTL 项目的 bug 修复提交（fix commit）拿来做回溯 ——
用修复前的版本跑本工具，看它能否指出**作者当年确实修掉的那个问题**；
再用修复后的版本跑一遍，确认告警**消失**。

"修复前报、修复后不报"是最强的形式：它把告警与真实修复因果地绑在一起。

目录约定
--------
    bench/backtest/<case>/
        META.json     来源、commit、许可、缺陷描述、期望规则与行号
        pre/          修复前的文件（来自 commit^）
        post/         修复后的文件（来自 commit）

META.json 的 status 字段：
    "hit"        —— 断言：pre 报出期望规则，且 post 不再报（不满足即失败）
    "known_miss" —— 已知漏报，仅记录不判失败（诚实披露能力边界）

用法
----
    python bench/evaluate_backtest.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BT = os.path.join(BASE, "bench", "backtest")


def scan(path_dir: str):
    """返回 (规则集合, {规则: [行号]})。"""
    res = ReviewEngine().review_path(path_dir)
    rules = {}
    for f in res.findings:
        rules.setdefault(f.rule_id, []).append(f.line)
    return set(rules), rules


def main() -> int:
    cases = sorted(d for d in os.listdir(BT) if os.path.isdir(os.path.join(BT, d)))
    if not cases:
        print("bench/backtest 下没有案例。")
        return 2

    print("=" * 100)
    print("真实 bug 回溯验证（修复前 vs 修复后）")
    print("=" * 100)
    print("%-42s %-6s %-8s %-8s %s" % ("案例", "期望", "修复前", "修复后", "判定"))
    print("-" * 100)

    ok = True
    for c in cases:
        cdir = os.path.join(BT, c)
        meta = json.load(open(os.path.join(cdir, "META.json"), encoding="utf-8"))
        rule = meta.get("expected_rule", "")
        status = meta.get("status", "")
        pre, pre_map = scan(os.path.join(cdir, "pre"))
        post, post_map = scan(os.path.join(cdir, "post"))
        pre_hit, post_hit = rule in pre, rule in post

        if status == "hit":
            good = pre_hit and not post_hit
            if not good:
                ok = False
            verdict = "PASS" if good else "FAIL"
        else:
            good = None
            verdict = "known_miss"

        print("%-42s %-6s %-8s %-8s %s" % (
            c, rule, "命中" if pre_hit else "未命中",
            "命中" if post_hit else "未命中", verdict))

        # 细节：命中时给出具体行号与信息
        if status == "hit" and pre_hit:
            for ln in sorted(pre_map.get(rule, []))[:8]:
                print("      · 修复前 %s @ L%d" % (rule, ln))

    print("-" * 100)
    print("说明：'known_miss' 是**已诚实披露**的漏报，不计失败；'hit' 类必须满足"
          "「修复前报、修复后不报」。")
    print("结论: %s" % ("PASS" if ok else "FAIL"))
    print("=" * 100)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
