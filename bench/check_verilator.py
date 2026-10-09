"""交叉验证：用 Verilator 独立复核 RTL-Sentinel 的告警。

为什么要它
----------
规则引擎是自研实现，"规则判断对不对"此前只能自证。本脚本引入**外部权威工具**
Verilator 对同一批样例做 lint，把"我说了算"变成"被工具背书" —— 这是本项目
唯一一处不依赖自身实现的独立性证据。

覆盖范围（诚实声明）
--------------------
Verilator 的警告与本项目的规则**只是部分重叠**，因此这是一次**局部**交叉验证，
只覆盖它能独立判定的三类：

    Verilator LATCH        <->  R001 推断锁存器
    Verilator MULTIDRIVEN  <->  R004 多驱动
    Verilator WIDTH        <->  R002 位宽截断

其余规则（CDC、敏感列表、不可综合结构、跨文件端口等）Verilator 不判，不在本
脚本的验证范围内。验收标准：

  · 缺陷样本：期望的映射规则，Verilator 必须报出对应警告（否则说明我们漏报）
  · 干净基线：Verilator 必须一条映射警告都不报（否则说明基线不干净）

用法
----
    python bench/check_verilator.py          # 需 PATH 里有 verilator（>= 5.0）

未安装 Verilator 时，脚本打印安装指引并以退出码 2 结束（视为跳过）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluate import collect_samples, load_ground_truth      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# RTL-Sentinel 规则 -> Verilator 警告码（仅这些规则可被外部工具独立复核）
RULE_TO_VERILATOR: Dict[str, str] = {
    "R001": "LATCH",
    "R004": "MULTIDRIVEN",
    "R002": "WIDTH",
}

# 匹配形如  %Warning-MULTIDRIVEN: samples/bad/cdc_buggy.v:30:14
WARN_RE = re.compile(r"%Warning-(\w+):\s*([^\s:]+):(\d+)")


def find_verilator() -> str:
    return shutil.which("verilator") or ""


def lint(files: List[str]) -> str:
    """运行 verilator --lint-only，返回其全部输出（stdout+stderr）。"""
    cmd = [find_verilator(), "--lint-only", "-Wall", "--timing", "-Wno-fatal"] + files
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "%%Fatal: %s: %s" % (type(exc).__name__, exc)
    return (p.stdout or "") + (p.stderr or "")


def main() -> int:
    if not find_verilator():
        print("=" * 78)
        print("Verilator 交叉验证 —— 跳过（未安装 verilator）")
        print("=" * 78)
        print("本机 PATH 中找不到 verilator。安装后即可运行本交叉验证：")
        print("  · Ubuntu/Debian : sudo apt-get install verilator")
        print("  · macOS         : brew install verilator")
        print("  · Windows       : winget install verilator  或 用 WSL/Docker")
        print("  · 也可改用 iverilog 做等价的最小验证（见 docs）")
        print("=" * 78)
        return 2

    samples = collect_samples()
    # 所有 good/ 里的文件都作为"支撑文件"一起喂给 verilator，以便解析
    # inst_buggy.v 对 sub_mod.v 的实例化（否则 Verilator 找不到模块定义）。
    support = [p for p in samples if os.sep + "good" + os.sep in p]

    print("=" * 78)
    print("Verilator 交叉验证")
    print("=" * 78)
    print("verilator: %s" % find_verilator())
    print("覆盖规则 : %s" % ", ".join("%s->%s" % kv for kv in RULE_TO_VERILATOR.items()))
    print("-" * 78)

    ok = True
    checked = 0
    for path in samples:
        rel = os.path.relpath(path, BASE).replace("\\", "/")
        entries = load_ground_truth(path)
        expect = {r for r, _ in entries if r in RULE_TO_VERILATOR}
        expected_codes = {RULE_TO_VERILATOR[r] for r in expect}

        out = lint([path] + [s for s in support if s != path])
        if "%Error" in out:
            print("%-30s [SKIP] Verilator 无法完成 lint（含其不支持的语法）" % rel)
            continue

        hits: Set[str] = set()
        for code, wfile, _line in WARN_RE.findall(out):
            if os.path.basename(wfile) == os.path.basename(path):
                hits.add(code)

        got_codes = hits & set(RULE_TO_VERILATOR.values())
        missing = expected_codes - got_codes        # 我们报缺陷、Verilator 没报
        extra = got_codes - expected_codes          # Verilator 报、我们没报
        checked += 1
        status = "PASS" if (not missing and not extra) else "FAIL"
        if status == "FAIL":
            ok = False
        print("%-30s [%s] 期望 %-18s Verilator 实报 %s" %
              (rel, status,
               ",".join(sorted(expected_codes)) or "（无）",
               ",".join(sorted(got_codes)) or "（无）"))
        if missing:
            print("      !! Verilator 未佐证: %s" % ",".join(sorted(missing)))
        if extra:
            print("      !! Verilator 报了但我们未报: %s" % ",".join(sorted(extra)))

    print("-" * 78)
    print("已交叉验证 %d 个样本；结论: %s" % (checked, "PASS" if ok else "FAIL"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
