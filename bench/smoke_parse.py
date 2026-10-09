"""解析器冒烟测试：确认解析层产出的静态事实与人工预期一致。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.parser import parse_file


def dump(path):
    mods, raw = parse_file(path)
    print("=" * 72)
    print("FILE:", os.path.basename(path), "| modules:", [m.name for m in mods])
    for m in mods:
        print("  module %s  lines %d-%d  params=%s" % (m.name, m.line_start, m.line_end, m.params))
        print("  -- signals --")
        for s in m.signals.values():
            print("     %-12s dir=%-7s storage=%-9s width=%s" %
                  (s.name, s.direction or "-", s.storage, s.width))
        print("  -- always blocks --")
        for b in m.always_blocks:
            print("     L%-3d-L%-3d seq=%-5s edge=%-8s clk=%-9s sens=%s" %
                  (b.line_start, b.line_end, b.is_seq, b.edge, b.clock, b.sens))
            print("        if=%s else=%s case=%s default=%s default_assigned=%s" %
                  (b.has_if, b.has_else, b.has_case, b.has_default, b.default_assigned))
            for a in b.assigns:
                print("        L%-3d %s %s %s" % (a.line, a.lhs, a.op, a.expr[:48].replace("\n", " ")))
        print("  -- continuous assigns --")
        for a in m.assigns:
            print("     L%-3d %s = %s" % (a.line, a.lhs, a.expr[:48].replace("\n", " ")))
        print("  -- instances --")
        for i in m.instances:
            print("     %s %s %s" % (i.module_type, i.instance_name, i.connections))


if __name__ == "__main__":
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
    for f in ("bad/uart_tx_buggy.v", "good/uart_tx_clean.v"):
        dump(os.path.join(base, f))
