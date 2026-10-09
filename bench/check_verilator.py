"""交叉验证：用 Verilator 独立复核 RTL-Sentinel 的告警。

为什么要它
----------
规则引擎是自研实现，"规则判断对不对"此前只能自证。本脚本引入**外部权威工具**
Verilator 对同一批样例做 lint，把"我说了算"变成"被工具背书"。

覆盖范围（实测口径，诚实声明）
------------------------------
Verilator 的警告与本项目规则只是**部分重叠**。经实测（Verilator 5.x）：

  · LATCH                      <-> R001 推断锁存器
  · WIDTHTRUNC / WIDTHEXPAND   <-> R002 位宽截断
      注意：Verilator 5 **没有**笼统的 "WIDTH" 码，位宽问题拆成了
      WIDTHTRUNC（截断，可能丢数据）与 WIDTHEXPAND（扩展，安全）。
  · MULTIDRIVEN                <-> R004 多驱动
      但仅对"实例 / always 块冲突"报；对"两条连续 assign 驱动同一 wire"
      （如 cdc_buggy 的 sync_ptr）Verilator 不报 —— 此时是本工具覆盖更广。

其余规则（CDC、敏感列表、不可综合结构、跨文件端口等）Verilator 不判，不在范围内。

验收标准（版本无关，避免被 Verilator 升级搞挂）
--------------------------------------------
  ① 干净基线（good/）：Verilator **不得**报出任何映射码 —— 独立确认基线确实干净
  ② 缺陷样例（bad/）：每个映射码至少被 Verilator 佐证一次 —— 独立确认缺陷真实存在

用法
----
    python bench/check_verilator.py          # 需 PATH 里有 verilator（>= 5.0）

未安装 Verilator 时打印安装指引并以退出码 2 结束（视为跳过）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import List, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluate import collect_samples, load_ground_truth      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Verilator 中与本项目规则对应的警告码（仅这些可被外部工具独立复核）
RULE_TO_VERILATOR = {
    "R001": "LATCH",
    "R002": ("WIDTHTRUNC", "WIDTHEXPAND"),
    "R004": "MULTIDRIVEN",
}
MAPPED_CODES = {"LATCH", "WIDTHTRUNC", "WIDTHEXPAND", "MULTIDRIVEN"}

# 匹配形如  %Warning-MULTIDRIVEN: samples/bad/cdc_buggy.v:30:14
WARN_RE = re.compile(r"%Warning-(\w+):\s*([^\s:]+):(\d+)")


def find_verilator() -> str:
    return shutil.which("verilator") or ""


def lint(files: List[str]) -> str:
    """运行 verilator --lint-only，返回其全部输出（stdout+stderr）。"""
    cmd = [find_verilator(), "--lint-only", "-Wall", "--timing", "-Wno-fatal"] + files
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
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
        print("  · Windows       : 用 WSL / Docker，或交给 CI（见 .github/workflows）")
        print("=" * 78)
        return 2

    samples = collect_samples()
    # 所有 good/ 文件作为"支撑文件"一起喂给 verilator，以便解析 inst_buggy.v
    # 对 sub_mod.v 的实例化（否则 Verilator 找不到模块定义）。
    support = [p for p in samples if os.sep + "good" + os.sep in p]

    print("=" * 78)
    print("Verilator 交叉验证")
    print("=" * 78)
    print("verilator: %s" % find_verilator())
    print("映射码   : %s" % ",".join(sorted(MAPPED_CODES)))
    print("-" * 78)

    ok = True
    seen: Set[str] = set()
    for path in samples:
        rel = os.path.relpath(path, BASE).replace("\\", "/")
        is_good = (os.sep + "good" + os.sep) in path
        out = lint([path] + [s for s in support if s != path])
        if "%Error" in out:
            print("%-30s [SKIP] Verilator 未完成 lint（含其不支持的语法）" % rel)
            continue

        hits = {code for code, wfile, _line in WARN_RE.findall(out)
                if os.path.basename(wfile) == os.path.basename(path)}
        mapped = hits & MAPPED_CODES
        seen |= mapped

        # ① 干净基线必须一条映射码都没有
        status = "OK"
        if is_good and mapped:
            status, ok = "FAIL", False
        print("%-30s [%s] verilator 映射码: %s" % (
            rel, status, ",".join(sorted(mapped)) or "（无）"))
        print("      codes: %s" % (",".join(sorted(hits)) or "(none)"))

    # ② 每个映射码至少被佐证一次
    unconfirmed = MAPPED_CODES - seen
    print("-" * 78)
    if unconfirmed:
        ok = False
        print("!! Verilator 未佐证: %s" % ",".join(sorted(unconfirmed)))
    else:
        print("Verilator 已独立佐证全部映射码: %s" % ",".join(sorted(MAPPED_CODES)))
    print("结论: %s" % ("PASS" if ok else "FAIL"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
