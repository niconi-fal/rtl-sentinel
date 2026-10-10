"""一键离线回归：跑完所有不依赖网络/API 的检查，任一硬检查失败即非零退出。

为什么需要
----------
本项目的硬约束是"规则改动后必须回归"，但此前散落在多个脚本里、靠人肉记命令。
本脚本把它收成一条命令，作为**零依赖**（不引入 pytest，保持零第三方依赖）的
回归入口，避免"改了规则却忘了跑某个评测"。

只打印 ASCII 状态行，不转发子脚本的输出（子脚本的细节请单独运行查看）。

覆盖
----
    bench/evaluate.py         受控集：规则级 + 定位级（必须全绿）
    bench/smoke_parse.py      解析层冒烟
    bench/smoke_rules.py      规则层对照
    真实语料 error 基线        防"新规则引入系统性误报"
    bench/check_verilator.py  外部交叉验证（未装 verilator 时自动跳过）

用法
----
    python bench/run_all.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# 真实语料（42 文件 / 15151 行）基线。改动规则后若 error 数**上升**，说明引入了
# 系统性误报，须人工确认后再决定是否更新基线 —— 更新基线本身应是一次有意的
# 决策，而不是顺手把数字改大。
CORPUS_ERROR_BASELINE = 27
CORPUS_FINDING_BASELINE = 128      # 修好解析器符号覆盖后，多出 1 条**真实**新发现
                                   # （picorv32.v R008：dbg_ascii_state 从未被读取）


def run(args, timeout=180):
    p = subprocess.run([PY] + args, capture_output=True, text=True,
                       cwd=BASE, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return p.returncode


def main() -> int:
    results = []

    for rel in ("bench/evaluate.py", "bench/smoke_parse.py", "bench/smoke_rules.py",
                "bench/evaluate_backtest.py", "bench/check_symbol_coverage.py"):
        rc = run([rel])
        print("[%s] %-26s (exit=%d)" % ("PASS" if rc == 0 else "FAIL", rel, rc))
        results.append((rel, rc == 0))

    # 真实语料：error 数不得高于基线。注意 cli 在存在 error 级告警时按 CI 门禁
    # 约定返回 1，所以这里**不**要求 rc==0，只看 JSON 是否产出成功。
    out_json = os.path.join(BASE, "out", "_regress_corpus.json")
    run(["-m", "rtl_sentinel.cli", "bench/corpus", "--no-llm",
         "-f", "json", "-o", out_json, "--quiet"])
    ok, detail = False, "no output"
    if os.path.isfile(out_json):
        with open(out_json, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        errs = sum(1 for f in doc["findings"] if f["severity"] == "error")
        n = len(doc["findings"])
        ok = errs <= CORPUS_ERROR_BASELINE and n <= CORPUS_FINDING_BASELINE
        detail = "findings=%d(<=%d) errors=%d(<=%d)" % (
            n, CORPUS_FINDING_BASELINE, errs, CORPUS_ERROR_BASELINE)
    print("[%s] %-26s %s" % ("PASS" if ok else "FAIL", "corpus baseline", detail))
    results.append(("corpus baseline", ok))

    # 外部交叉验证：未装 verilator 视为 SKIP（不算失败）
    rc = run(["bench/check_verilator.py"])
    if rc == 2:
        print("[SKIP] %-26s (verilator not installed)" % "verilator cross-check")
    else:
        print("[%s] %-26s (exit=%d)" %
              ("PASS" if rc == 0 else "FAIL", "verilator cross-check", rc))
    results.append(("verilator cross-check", rc in (0, 2)))

    failed = [name for name, ok in results if not ok]
    print("=" * 70)
    print("REGRESSION: %s" % ("ALL PASS" if not failed else "FAIL -> " + ", ".join(failed)))
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
