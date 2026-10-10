"""生成"盲标"清单，供独立第三方（人或另一个 AI 模型）对告警做裁定。

为什么需要
----------
`bench/golden_adjudication.json` 的标注目前是"自产自评" —— 由本项目自己给出、
再用它评测本项目自己，属循环论证。要让"裁定一致率"成为可信数字，必须由
**没看过现有结论**的独立方重新标注。

两种标注协议（--protocol）
--------------------------
  three （默认）三态量表：confirmed / false_positive / uncertain
        实测问题：标注者对"要不要弃权"的自由度极大，导致跨标注者一致率在
        47.6%~95.2% 之间摆动（见 docs/项目说明.md 第十一节）。弃权是分歧之源。
  binary 可操作二元量表（推荐）：change / waive / abstain
        把问题改成"你会去改代码，还是标豁免？"，并给出 4 条**校准样例**锚定尺度。
        abstain 被明确要求"尽量少用"。目的是把分歧从"要不要弃权"拉回到"改不改"。

产物（内容一致，用途不同）
    out/blind_labels*.md       给**人**阅读（含源码上下文，>> 标记告警行）
    out/blind_labels*.csv      给**人**填写（Excel 可打开）
    out/label_prompt*.md       给**另一个 AI 模型**：可直接整段粘贴的完整提示词

关键：脚本**不读** `golden_adjudication.json`，输出里也不含任何现有判定结论。

诚实声明：用另一个 AI 模型标注属**跨模型独立标注**，比自产自评强，但**不等于
人工金标准**；对外表述时须如实说明标注者身份。

用法
----
    python bench/prepare_blind_labels.py --protocol binary          # 全部
    python bench/prepare_blind_labels.py --protocol binary --limit 30 --seed 0
    python bench/prepare_blind_labels.py                           # 三态（旧口径）
    python bench/prepare_blind_labels.py --neutral                 # 三态中性版（对照）
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtl_sentinel.engine import ReviewEngine       # noqa: E402
from rtl_sentinel.llm import _context_snippet      # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(BASE, "bench", "corpus")
OUT = os.path.join(BASE, "out")

INSTRUCTION = """\
# 盲标清单：RTL 告警裁定（三态量表）

请对下面每条告警独立判断：**它是否指向一个「需要修改的真实问题」？**

- `confirmed`      —— 是，这是真实缺陷，应当修复
- `false_positive` —— 否，这是设计者有意为之的合法写法（应豁免，不用改）
- `uncertain`      —— 证据不足，无法判断

注意：这些告警来自**成熟的开源工程代码**，作者都是有经验的工程师。
经验上这类代码里大部分告警并非缺陷，而是有意为之的写法 —— 请如实判断。
代码上下文见下，`>>` 标记的是告警行。
"""

PROMPT = """\
你是一名资深数字 IC 设计 / 验证工程师。下面给你一批由静态检查工具从**成熟开源
Verilog 工程**（RISC-V 软核 picoRV32、UART 收发器、AXI-Stream 基础设施库）中
报出的告警，请你独立判断每条告警**是否指向一个「需要修改的真实问题」**。

判断原则：
- 只依据给出的源码上下文与告警描述判断，不要臆测没有提供的信息。
- **不要假设"有告警就是问题"**。这些代码的作者都是有经验的工程师，经验上这类
  告警里相当一部分并非缺陷，而是有意为之的合法写法，例如：
  · 行为级仿真模型（不参与综合）
  · 仅用于过程块内顺序计算的临时变量
  · 有意的位宽截断 / 零扩展（如把 32 位结果取低 8 位驱动 LED）
  · 仿真调试代码与断言（$display / $error / $fatal）
  · 参数化设计中的互斥条件分支
- 证据不足以判断设计意图时，务必输出 uncertain，不要臆测。

判定取值（三选一，必须严格使用这三个英文词）：
  confirmed      —— 是真实缺陷，应当修复
  false_positive —— 否，这是设计者有意为之的合法写法，应豁免而非修复
  uncertain      —— 证据不足以判断

输出要求（严格遵守，否则无法自动汇总）：
- **只输出 CSV**，不要任何解释、标题、前后缀或 Markdown 代码围栏
- 表头固定为： id,verdict,reason
- 每个 id 恰好一行；verdict 用上面三个词之一；reason 用一句话说明理由
  （能引用具体信号名或行号最好），不要换行、不要包含逗号以外的分隔符问题
- 不要遗漏任何 id，也不要新增 id

待判定的告警如下：

"""

PROMPT_NEUTRAL = """\
你是一名资深数字 IC 设计 / 验证工程师。下面给你一批由静态检查工具从开源 Verilog
工程中报出的告警，请你独立判断每条告警**是否指向一个需要修改的真实问题**。

判断原则：
- 只依据给出的源码上下文与告警描述判断，不要臆测没有提供的信息。
- 若证据不足以判断设计意图，输出 uncertain，不要臆测。

判定取值（三选一，必须严格使用这三个英文词）：
  confirmed      —— 是真实缺陷，应当修复
  false_positive —— 否，这是设计者有意为之的合法写法，应豁免而非修复
  uncertain      —— 证据不足以判断

输出要求（严格遵守，否则无法自动汇总）：
- **只输出 CSV**，不要任何解释、标题、前后缀或 Markdown 代码围栏
- 表头固定为： id,verdict,reason
- 每个 id 恰好一行；verdict 用上面三个词之一；reason 用一句话说明理由
- 不要遗漏任何 id，也不要新增 id

待判定的告警如下：

"""

# 可操作二元量表 + 校准样例（推荐协议）：把分歧从"要不要弃权"拉回到"改不改"。
PROMPT_BINARY = """\
你是一名资深数字 IC 设计 / 验证工程师。下面是一批由静态检查工具从开源 Verilog
工程中报出的告警。请对**每一条**回答一个可操作的问题：

    如果这段代码出自你要交付的工程，你会去改源码，还是把它标为豁免？

判定取值（必须严格使用这三个英文词）：
  change  —— 去改代码。这是真实缺陷，必须修复。
  waive   —— 标豁免。这是设计者有意为之的写法、或不属于综合目标，不需要改。
  abstain —— 弃权。**仅当给出的上下文完全不足以判断时才用**，并在 reason 里写明
             你缺什么信息。请尽量少用；能判就判。

判断准则：
- 只依据给出的源码上下文与告警描述判断，不要臆测没有提供的信息。
- 既不要默认"有告警就要改"，也不要为省事一律标豁免 —— 两个方向都算错。
- 通常属 change：赋值方式误用（时序块里阻塞赋值 / 组合块里非阻塞）、位宽截断造成
  功能错误、多驱动冲突、缺分支导致推断 latch、不可综合结构进入综合路径。
- 通常属 waive：仿真专用代码（行为级仿真模型 / 断言 / $display）、有意的位宽取舍
  （如只取高位驱动少数输出）、以及需要工程外信息才能确认的保守告警。

【校准样例】先看这 4 条，确认你的判断尺度：

例 1 —— change（改代码）
```verilog
always @(posedge clk) begin
    if (!rst_n) q <= 1'b0;
    else        q = d;            // 时序块里用了阻塞赋值
end
```
理由：时序块内对寄存器用阻塞赋值，仿真与综合行为会不一致，是真实缺陷。

例 2 —— waive（标豁免）
```verilog
reg [7:0] led;
always @(posedge clk)
    led <= counter[31:24];        // 只取高 8 位驱动 LED
```
理由：设计者只想驱动 8 个 LED，截断是有意的，不是缺陷。

例 3 —— waive（标豁免）
```verilog
initial begin
    if (WIDTH % 8 != 0) $error("width not byte aligned");
end
```
理由：initial + $error 只在仿真期做参数体检，综合工具会忽略。

例 4 —— change（改代码）
```verilog
assign busy = ready;
always @(posedge clk) busy <= start;
```
理由：同一网络同时被连续赋值与时序块驱动，综合会报 multiple driver。

【输出要求】严格遵守，否则无法自动汇总：
- **只输出 CSV**，不要任何解释、标题、前后缀或 Markdown 代码围栏
- 表头固定为： id,decision,reason
- 每个 id 恰好一行；decision 用 change / waive / abstain 之一；reason 一句话说明理由
- 不要遗漏任何 id，也不要新增 id

待判定的告警如下：

"""


def render_items(res, groups, keys, facts=None):
    """把待判定条目渲染成文本（人读的 MD 与 AI 的提示词共用）。

    facts: {文件名: 事实字符串}。给出后每条附一行**文件级事实**，用于消除
    \u201c标注方看不到整份文件、只能看告警附近几行\u201d这一信息不对称 ——
    实测中它造成了约一半的标注分歧（如把行为级仿真模型误判为需修改的缺陷）。
    """
    out = []
    for i, key in enumerate(keys, 1):
        fs = groups[key]
        f0 = fs[0]
        out.append("### %d. `%s` L%d —— %s（%s）\n" % (i, key[0], f0.line, key[1], f0.rule_name))
        out.append("- 工具告警：%s\n" % f0.message)
        if facts and key[0] in facts:
            out.append("- 文件事实：%s\n" % facts[key[0]])
        if len(fs) > 1:
            out.append("- 该文件另有 %d 条同类告警（属同一判定单元，一并判定）\n" % (len(fs) - 1))
        out.append("\n```verilog\n%s\n```\n\n"
                   % _context_snippet(res.sources.get(f0.file, ""), f0.line))
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成盲标清单（给人 / 给 AI）")
    ap.add_argument("--corpus", default=CORPUS, help="待审查语料目录")
    ap.add_argument("--outdir", default=OUT, help="输出目录")
    ap.add_argument("--limit", type=int, default=0, help="抽样个数；0 = 全部")
    ap.add_argument("--seed", type=int, default=0, help="抽样随机种子（保证可复现）")
    ap.add_argument("--protocol", choices=["three", "binary"], default="three",
                    help="three=三态量表（旧） / binary=可操作二元+校准样例（推荐）")
    ap.add_argument("--neutral", action="store_true",
                    help="三态协议下生成中性版提示词（不暗示\u201c大部分告警不是问题\u201d）")
    ap.add_argument("--file-facts", action="store_true",
                    help="每条附一行文件级事实（行数 / 是否含 #N 延时 / 是否含 task 定义），"
                         "消除\u201c只能看告警附近几行\u201d的信息不对称")
    args = ap.parse_args()

    binary = (args.protocol == "binary")
    col = "decision" if binary else "verdict"
    suffix = "_binary" if binary else ("_neutral" if args.neutral else "")
    if args.file_facts:
        suffix += "_facts"

    eng = ReviewEngine()
    res = eng.review_path(args.corpus)

    groups = {}
    for f in res.sorted_findings():
        groups.setdefault((os.path.basename(f.file), f.rule_id), []).append(f)
    keys = sorted(groups)
    if args.limit and args.limit < len(keys):
        random.Random(args.seed).shuffle(keys)
        keys = sorted(keys[:args.limit])

    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.outdir, "blind_labels%s.csv" % suffix)
    md_path = os.path.join(args.outdir, "blind_labels%s.md" % suffix)
    prompt_path = os.path.join(args.outdir, "label_prompt%s.md" % suffix)
    facts = None
    if args.file_facts:
        facts = {}
        for path, src in res.sources.items():
            name = os.path.basename(path)
            facts[name] = "%s，共 %d 行；含延时 `#N`：%s；含 task 定义：%s" % (
                name, len(src.split("\n")),
                "是" if re.search(r"#\s*\d+", src) else "否",
                "是" if re.search(r"(?<![\w$])task(?![\w$])", src) else "否")
    items = render_items(res, groups, keys, facts)

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "file", "rule_id", "rule_name", "line", "message", col, "reason"])
        for i, key in enumerate(keys, 1):
            fs = groups[key]
            f0 = fs[0]
            more = ("（该文件另有 %d 条同类告警，属同一判定单元）" % (len(fs) - 1)
                    if len(fs) > 1 else "")
            w.writerow([i, key[0], key[1], f0.rule_name, f0.line, f0.message + more, "", ""])

    head = (INSTRUCTION if not binary else
            "# 盲标清单：RTL 告警裁定（可操作二元量表）\n\n"
            "请对每条告警回答：**如果这段代码出自你要交付的工程，你会去改代码，还是标豁免？**\n\n"
            "- `change`  —— 去改代码，这是真实缺陷\n"
            "- `waive`   —— 标豁免，有意为之或不属于综合目标\n"
            "- `abstain` —— 弃权（仅当上下文完全不足以判断，尽量少用）\n\n"
            "请在对应 CSV 的 `decision` / `reason` 两列填写。\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(head)
        fh.write("\n共 **%d** 个判定单元。\n\n---\n\n" % len(keys))
        fh.write(items)

    prompt = PROMPT_BINARY if binary else (PROMPT_NEUTRAL if args.neutral else PROMPT)
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
        fh.write(items)

    print("=" * 74)
    print("盲标清单已生成（协议=%s，判定单元 %d 个）" % (args.protocol, len(keys)))
    print("=" * 74)
    print("给人阅读 : %s" % os.path.relpath(md_path, BASE).replace("\\", "/"))
    print("给人填写 : %s" % os.path.relpath(csv_path, BASE).replace("\\", "/"))
    print("给 AI 粘贴: %s   ← 整段复制到另一个模型即可" % os.path.relpath(prompt_path, BASE).replace("\\", "/"))
    print()
    print("回填后合并（脚本自动识别 verdict / decision 列）：")
    print("  python bench/merge_blind_labels.py %s --kind model --rater \"模型名\""
          % os.path.relpath(csv_path, BASE).replace("\\", "/"))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
