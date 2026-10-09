"""RTL-Sentinel 命令行入口。

用法示例
--------
    python -m rtl_sentinel.cli samples/bad/uart_tx_buggy.v
    python -m rtl_sentinel.cli rtl/ -f html -o report.html
    python -m rtl_sentinel.cli rtl/ --no-llm --min-severity warning
    python -m rtl_sentinel.cli rtl/ --base-url http://localhost:11434/v1 --model qwen2.5-coder
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Set

from . import report
from .engine import ReviewEngine
from .llm import LLMDetector, MockProvider, OpenAICompatProvider, Reasoner
from .model import SEVERITY_ORDER
from .waiver import WaiverSet

SEV_CHOICES = ["error", "warning", "info"]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rtl-sentinel",
        description="RTL-Sentinel —— 面向集成电路设计的 RTL 智能审查 Agent",
    )
    ap.add_argument("path", help="待审查的 .v/.sv 文件或目录")
    ap.add_argument("-f", "--format", choices=["text", "md", "html", "json", "sarif"],
                    default="text", help="输出格式（默认 text；sarif 可接入 CI）")
    ap.add_argument("-o", "--out", help="输出文件路径；缺省则打印到标准输出")
    ap.add_argument("--no-llm", action="store_true",
                    help="跳过语义推理层，仅输出规则引擎结果")
    ap.add_argument("--model",
                    default=os.environ.get("RTL_SENTINEL_MODEL", "gpt-4o-mini"),
                    help="LLM 模型名（也可用环境变量 RTL_SENTINEL_MODEL）")
    ap.add_argument("--base-url",
                    default=os.environ.get("RTL_SENTINEL_BASE_URL"),
                    help="OpenAI 兼容 API 地址（也可用环境变量 RTL_SENTINEL_BASE_URL）；"
                         "只设了 API Key 而未设此项时会退回 OpenAI 默认端点")
    ap.add_argument("--min-severity", choices=SEV_CHOICES, default="info",
                    help="只显示不低于该严重度的告警")
    ap.add_argument("-r", "--rules", help="只启用指定规则，逗号分隔，如 R001,R003")
    ap.add_argument("--config", help="豁免配置路径（缺省自动查找 .rtlsentinel.json）")
    ap.add_argument("--no-waiver", action="store_true",
                    help="禁用豁免，输出全部告警（用于对比与调试）")
    ap.add_argument("--llm-detect", action="store_true",
                    help="额外启用大模型检测通道（补充规则引擎的漏报；"
                         "结果标为待复核、不计入风险分、不进 CI 门禁）")
    ap.add_argument("--quiet", action="store_true", help="不输出进度信息")
    return ap


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.path.exists(args.path):
        sys.stderr.write("错误: 路径不存在 -> %s\n" % args.path)
        return 2

    only: Optional[Set[str]] = None
    if args.rules:
        only = {r.strip().upper() for r in args.rules.split(",") if r.strip()}

    eng = ReviewEngine(only=only)
    res = eng.review_path(args.path)

    if res.rule_errors and not args.quiet:
        for err in res.rule_errors:
            sys.stderr.write("[warn] 规则异常: %s\n" % err)
    if not res.files:
        sys.stderr.write("错误: 未找到可审查的 Verilog 文件 (.v/.sv/.vh/.svh)\n")
        return 2

    # 语义推理层
    if not args.no_llm:
        if args.base_url or os.environ.get("RTL_SENTINEL_API_KEY"):
            provider = OpenAICompatProvider(model=args.model, base_url=args.base_url)
        else:
            provider = MockProvider()

        # ① 可选：大模型检测通道 —— 补充规则引擎的漏报
        if args.llm_detect:
            if isinstance(provider, MockProvider):
                sys.stderr.write("[warn] --llm-detect 需要真实 LLM 后端，已跳过"
                                 "（离线启发式不具备检测能力）\n")
            else:
                extra = LLMDetector(provider).detect_files(res.sources)
                res.findings.extend(extra)
                if not args.quiet:
                    sys.stderr.write("[info] 大模型检测通道补充 %d 条待复核线索\n"
                                     % len(extra))

        # ② 只对**规则引擎产出**的告警做语义裁定。LLM 检测通道的线索
        #    （tag: llm_detect）本身就是“待复核”，不再送审 —— 否则等于让
        #    LLM 审自己，也与“只裁定规则告警”的裁定提示词口径不符。
        rule_findings = [f for f in res.findings if "llm_detect" not in f.tags]
        Reasoner(provider).adjudicate(rule_findings, sources=res.sources)
        if not args.quiet:
            sys.stderr.write("[info] 语义推理后端: %s\n" % provider.name)

    # 豁免层：集中式、带理由、可审计；被豁免的告警仍会出现在报告中
    waivers = WaiverSet()
    if not args.no_waiver:
        waivers = (WaiverSet.load(args.config) if args.config
                   else WaiverSet.autodiscover(args.path))
    if waivers:
        n_waived = waivers.apply(res.findings)
        if not args.quiet:
            sys.stderr.write("[info] 应用 %d 条豁免规则，共豁免 %d 条告警\n"
                             % (len(waivers), n_waived))

    # 严重度过滤（已豁免的告警不受过滤影响，保证透明可见）
    floor = SEVERITY_ORDER[args.min_severity]
    res.findings = [f for f in res.findings
                    if f.waived or SEVERITY_ORDER.get(f.severity, 9) <= floor]

    if args.format == "html":
        content = report.to_html(res)
    elif args.format == "md":
        content = report.to_markdown(res)
    elif args.format == "json":
        content = report.to_json(res)
    elif args.format == "sarif":
        content = report.to_sarif(res)
    else:
        content = report.to_text(res, color=(args.out is None and sys.stdout.isatty()))

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(content)
        if not args.quiet:
            sys.stderr.write("[info] 报告已写入 %s\n" % args.out)
    else:
        sys.stdout.write(content + "\n")

    return 1 if res.by_severity()["error"] else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
