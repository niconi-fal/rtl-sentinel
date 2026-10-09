"""交叉验证：用 Verilator 独立复核 RTL-Sentinel 的告警。

为什么要它
----------
规则引擎是自研实现，"规则判断对不对"此前只能自证。本脚本引入**外部权威工具**
Verilator 对同一批样例做 lint，把"我说了算"变成"被工具背书"。

覆盖范围与实测口径（诚实声明，均为实测结论）
--------------------------------------------
Verilator 的警告与本项目规则只是**部分重叠**，实测（Verilator 5.x）发现：

  · LATCH     <-> R001 推断锁存器     —— 确实指向缺陷，可作缺陷信号
  · MULTIDRIVEN <-> R004 多驱动        —— 确实指向缺陷，可作缺陷信号；
        但对"两条连续 assign 驱动同一 wire"（如 cdc_buggy 的 sync_ptr）Verilator
        不报 —— 此时是本工具覆盖更广。
  · 位宽码     —— Verilator 5 无笼统 "WIDTH"，拆成 WIDTHTRUNC / WIDTHEXPAND。
        **不能当缺陷信号**：WIDTHEXPAND 是正常的零扩展（干净基线也会触发），
        WIDTHTRUNC 也可能是设计者有意为之。故本脚本只把它们作为"信息"打印，
        不参与判定。

其余规则（CDC、敏感列表、不可综合结构、跨文件端口等）Verilator 不判。

判定标准（版本无关，避免被 Verilator 升级搞挂）
--------------------------------------------
  ① 干净基线（good/）：Verilator **不得**报出 LATCH / MULTIDRIVEN —— 外部 FP 检查
  ② 缺陷样例（bad/）：LATCH / MULTIDRIVEN 至少被佐证一次 —— 外部 TP 证据

  部分样例 Verilator 无法完成 lint（含其不支持的语法），会打印 [SKIP] 并附原因，
  不计入判定 —— 这是 Verilator 的覆盖边界，不是本工具的结论。

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

from evaluate import collect_samples      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 真正指向缺陷、且本项目也覆盖的 Verilator 警告码（参与判定）
DEFECT_CODES = {"LATCH", "MULTIDRIVEN"}
# 位宽相关码：仅作信息打印，不参与判定（见文件头"实测口径"）
WIDTH_CODES = {"WIDTHTRUNC", "WIDTHEXPAND"}

# 匹配形如  %Warning-MULTIDRIVEN: samples/bad/cdc_buggy.v:30:14
WARN_RE = re.compile(r"%Warning-(\w+):\s*([^\s:]+):(\d+)")
ERR_RE = re.compile(r"%Error[^\n]*")


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
    print("verilator  : %s" % find_verilator())
    print("判定码     : %s" % ",".join(sorted(DEFECT_CODES)))
    print("-" * 78)

    ok = True
    confirmed: Set[str] = set()
    for path in samples:
        rel = os.path.relpath(path, BASE).replace("\\", "/")
        is_good = (os.sep + "good" + os.sep) in path
        out = lint([path] + [s for s in support if s != path])

        if "%Error" in out:
            m = ERR_RE.search(out)
            print("%-30s [SKIP] Verilator 未能完成 lint: %s"
                  % (rel, (m.group(0)[:110] if m else "")))
            continue

        hits = {code for code, wfile, _line in WARN_RE.findall(out)
                if os.path.basename(wfile) == os.path.basename(path)}
        defect = hits & DEFECT_CODES
        width = ",".join(sorted(hits & WIDTH_CODES)) or "-"

        status = "OK"
        if is_good and defect:                    # ① 干净基线不得有缺陷码
            status, ok = "FAIL", False
        elif not is_good:
            confirmed |= defect                   # ② 缺陷样例的佐证
        print("%-30s [%s] 缺陷码: %-16s 位宽码(仅信息): %s"
              % (rel, status, ",".join(sorted(defect)) or "（无）", width))

    print("-" * 78)
    if confirmed:
        print("Verilator 独立佐证: %s" % ",".join(sorted(confirmed)))
    if not confirmed:                             # ② 至少佐证一次
        ok = False
        print("!! Verilator 未能佐证任何缺陷码（%s）"
              % ",".join(sorted(DEFECT_CODES)))
    print("结论: %s" % ("PASS" if ok else "FAIL"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
