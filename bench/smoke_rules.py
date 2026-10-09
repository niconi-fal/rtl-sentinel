"""规则引擎冒烟测试：核对命中规则集合与 GROUND TRUTH 是否一致。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CASES = [
    ("samples/bad/uart_tx_buggy.v", {"R001", "R002", "R003", "R004",
                                     "R005", "R006", "R007", "R008"}),
    ("samples/good/uart_tx_clean.v", set()),
]

if __name__ == "__main__":
    eng = ReviewEngine()
    ok = True
    for rel, expect in CASES:
        res = eng.review_path(os.path.join(BASE, rel))
        got = res.rule_ids()
        print("=" * 72)
        print("%-32s risk=%-5s health=%s" % (rel, res.risk_score, res.health))
        print("  expect: %s" % (sorted(expect) or "（无告警）"))
        print("  got   : %s" % sorted(got))
        miss, extra = expect - got, got - expect
        if miss:
            print("  !! MISS : %s" % sorted(miss))
            ok = False
        if extra:
            print("  !! EXTRA(false positive): %s" % sorted(extra))
            ok = False
        for f in res.sorted_findings():
            print("     %-5s %-8s L%-3d %s" % (f.rule_id, f.severity, f.line,
                                               f.message[:76]))
        if res.rule_errors:
            print("  rule errors: %s" % res.rule_errors)
    print("=" * 72)
    print("RESULT:", "PASS" if ok else "FAIL")
