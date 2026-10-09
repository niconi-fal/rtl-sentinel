"""对照实验：让大模型"直接检测" Verilog 缺陷，与 RTL-Sentinel 的规则引擎对比。

实验设计
--------
同一套 LintLLM benchmark（90 个模块 / 11 类注入缺陷），两条路线各自独立作答：

  A. RTL-Sentinel 规则引擎 —— 确定性静态分析（bench/evaluate_lintllm.py）
  B. DeepSeek 直接检测   —— 把完整源码交给 LLM，让它自己指出缺陷行（本脚本）

对比的意义
----------
上游 LintLLM 论文报告: o1-mini+LintLLM 正确率 83.33% / 误报率 12.22%，
商业 EDA 64.44% / 27.78%，Verilator 62.22% / 32.22%。

本实验回答一个更具体的问题：**"可形式化的缺陷"是否真的更适合交给规则引擎？**
如果规则引擎与 LLM 的差距集中在语法/声明类缺陷上，就验证了双引擎分工的合理性。

公平性说明
----------
- 两条路线使用**同一份** ground truth 与同一个判定口径（模块级命中）。
- LLM 只拿到源码，不拿到答案；prompt 中不提示缺陷类型分布。
- 温度设为 0，保证可复现。

用法
----
    RTL_SENTINEL_API_KEY=sk-xxx python bench/evaluate_llm_direct.py \\
        --model deepseek-chat --base-url https://api.deepseek.com/v1
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.llm import OpenAICompatProvider      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BM = os.path.join(BASE, "bench", "thirdparty", "lintllm")
GT_PATH = os.path.join(BM, "GROUND_TRUTH.json")
LEVELS = ("simple", "medium", "complex")

SYSTEM_PROMPT = """你是一名资深数字 IC 设计验证工程师，负责 Verilog 代码的静态审查。

请检查给你的 Verilog 代码中是否存在**静态缺陷**，可能的类型包括但不限于:
  标点符号 / 模块结构 / 语句结尾 / 变量声明与使用 / 位宽使用 / 赋值方式
  敏感列表 / 竞争冒险 / case 结构 / 模块实例化 / 运算符使用

要求:
  - 逐行检查，指出**确实存在缺陷**的行号（1 起算）
  - 若无缺陷，defect_lines 为空数组
  - 只输出 JSON，不要输出其他内容

输出格式:
{"has_defect": true, "defect_lines": [12], "reason": "不超过 60 字的说明"}
"""


def _parse_json(text: str) -> Optional[dict]:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?|```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 直接检测 benchmark 对照实验")
    ap.add_argument("--model", default=os.environ.get("RTL_SENTINEL_MODEL", "deepseek-chat"))
    ap.add_argument("--base-url", default=os.environ.get("RTL_SENTINEL_BASE_URL",
                                                         "https://api.deepseek.com/v1"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个模块（调试用）")
    ap.add_argument("--out", default=os.path.join(BASE, "out", "llm_direct.json"))
    args = ap.parse_args()

    key = os.environ.get("RTL_SENTINEL_API_KEY", "")
    if not key:
        sys.stderr.write("错误: 需要环境变量 RTL_SENTINEL_API_KEY\n")
        return 2
    provider = OpenAICompatProvider(model=args.model, base_url=args.base_url,
                                    api_key=key, timeout=120)

    with open(GT_PATH, "r", encoding="utf-8") as fh:
        gt = {it["file"]: it for it in json.load(fh)["items"]}

    paths: List[str] = []
    for lv in LEVELS:
        folder = os.path.join(BM, lv)
        paths.extend(os.path.join(folder, f) for f in sorted(os.listdir(folder))
                     if f.endswith(".v"))
    if args.limit:
        paths = paths[: args.limit]

    print("=" * 84)
    print("对照实验: LLM 直接检测 vs RTL-Sentinel 规则引擎")
    print("=" * 84)
    print("后端: %s   模块数: %d" % (provider.name, len(paths)))
    print()

    hit = miss = err = 0
    per_type: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    per_level: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    records = []
    t0 = time.perf_counter()

    for idx, path in enumerate(paths, 1):
        fn = os.path.basename(path)
        item = gt.get(fn)
        if item is None:
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        numbered = "\n".join("%4d| %s" % (i, ln.rstrip())
                             for i, ln in enumerate(src.split("\n"), 1))
        try:
            raw = provider.complete(SYSTEM_PROMPT, numbered)
            data = _parse_json(raw) or {}
        except Exception as exc:                       # noqa: BLE001
            err += 1
            data = {}
            print("  [%2d/%d] %-14s 调用失败: %s" % (idx, len(paths), fn, type(exc).__name__))
            records.append({"file": fn, "hit": False, "error": str(exc)})
            continue

        gt_lines = set(item["lines"])
        got_lines = set()
        for v in data.get("defect_lines", []) or []:
            try:
                got_lines.add(int(v))
            except (TypeError, ValueError):
                pass
        ok = bool(gt_lines & got_lines)
        hit += ok
        miss += (not ok)
        per_type[item["type"]][1] += 1
        per_level[item["level"]][1] += 1
        if ok:
            per_type[item["type"]][0] += 1
            per_level[item["level"]][0] += 1
        records.append({"file": fn, "type": item["type"], "level": item["level"],
                        "gt_lines": sorted(gt_lines), "llm_lines": sorted(got_lines),
                        "hit": ok, "verdict": data.get("has_defect"),
                        "reason": str(data.get("reason", ""))[:120]})
        print("  [%2d/%d] %-14s GT=%-10s LLM=%-14s %s" %
              (idx, len(paths), fn, sorted(gt_lines), sorted(got_lines),
               "命中" if ok else "未中"))

    dt = time.perf_counter() - t0
    n = hit + miss
    print()
    print("-" * 84)
    print("模块级检出率 : %.1f%%  (%d/%d)" % (hit / n * 100 if n else 0, hit, n))
    if err:
        print("调用失败     : %d 个" % err)
    print("耗时         : %.1f s  (%.1f s/模块)" % (dt, dt / max(len(paths), 1)))
    print()
    print("按难度")
    for lv in LEVELS:
        h, t = per_level[lv]
        if t:
            print("  %-10s %2d/%2d  %.1f%%" % (lv, h, t, h / t * 100))
    print()
    print("按缺陷类型")
    for t, (h, tot) in sorted(per_type.items(), key=lambda kv: -kv[1][1]):
        print("  %-30s %2d/%2d  %s" % (t, h, tot, ("%.0f%%" % (h / tot * 100)) if tot else "-"))
    print()
    print("-" * 84)
    print("三路线对照（同一 benchmark / 同一口径 / 上游论文数据）")
    print("  %-28s %10s %10s" % ("方案", "正确率↑", "误报率↓"))
    print("  %-28s %10s %10s" % ("商业 EDA（论文）", "64.44%", "27.78%"))
    print("  %-28s %10s %10s" % ("Verilator（论文）", "62.22%", "32.22%"))
    print("  %-28s %10s %10s" % ("o1-mini + LintLLM（论文）", "83.33%", "12.22%"))
    print("=" * 84)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"backend": provider.name, "modules": len(paths),
                   "hit": hit, "miss": miss, "errors": err,
                   "hit_rate": round(hit / n, 4) if n else 0,
                   "seconds": round(dt, 1), "records": records},
                  fh, ensure_ascii=False, indent=2)
    print("结果已保存 -> %s" % os.path.relpath(args.out, BASE).replace("\\", "/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
