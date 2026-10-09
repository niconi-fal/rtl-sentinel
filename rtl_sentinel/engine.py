"""审查引擎：规则调度、告警聚合、风险评分。

引擎的职责边界:
    - 只调用规则函数产出确定性告警，不做语义推理
    - 单条规则异常不影响整体流程（面向真实工程代码的鲁棒性要求）
    - 输出带风险评分的结构化结果，供报告层与 LLM 层消费
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .index import ModuleIndex
from .model import SEVERITY_ORDER, Finding, Module
from .parser import parse_source
from .rules import RULES

SUPPORTED_EXT = (".v", ".sv", ".vh", ".svh")


@dataclass
class ReviewResult:
    findings: List[Finding] = field(default_factory=list)
    modules: List[Module] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    sources: Dict[str, str] = field(default_factory=dict)
    rule_errors: List[str] = field(default_factory=list)
    index_stats: Dict[str, int] = field(default_factory=dict)

    @property
    def loc(self) -> int:
        """被审查代码的总行数。"""
        return sum(len(s.split("\n")) for s in self.sources.values())

    @property
    def risk_score(self) -> float:
        """风险分 0-100，按**代码规模归一化**的加权告警密度。

        为什么必须归一化: 绝对加权和对大工程不公平 —— 15000 行代码里的
        8 个 error 与 200 行里的 8 个 error，严重程度完全不同。
        基准: 每千行 40 个加权缺陷视为满风险（100 分）。

        为什么排除 `llm_detect`: 大模型检测通道的产出是"待复核"线索而非
        确定性结论，把它计入风险分会让不可复现的因素影响 CI 门禁。
        """
        raw = sum(f.weight for f in self.findings
                  if not f.is_suppressed and "llm_detect" not in f.tags)
        loc = self.loc or 1
        density = raw / (loc / 1000.0)
        return min(100.0, round(density * 2.5, 1))

    @property
    def health(self) -> float:
        return round(max(0.0, 100.0 - self.risk_score), 1)

    def by_severity(self) -> Dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for f in self.findings:
            if not f.is_suppressed:
                counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    def by_rule(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for f in self.findings:
            if not f.is_suppressed:
                counts[f.rule_id] = counts.get(f.rule_id, 0) + 1
        return counts

    def rule_ids(self) -> Set[str]:
        return {f.rule_id for f in self.findings if not f.is_suppressed}

    def sorted_findings(self) -> List[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (f.file, f.line, SEVERITY_ORDER.get(f.severity, 9), f.rule_id),
        )


class ReviewEngine:
    """规则调度器。"""

    def __init__(self, only: Optional[Set[str]] = None):
        self.rules = [(rid, name, fn) for rid, name, fn in RULES
                      if only is None or rid in only]

    # -- 单源审查 ----------------------------------------------------------
    def review_source(self, src: str, path: str = "<memory>",
                      index: Optional[ModuleIndex] = None) -> ReviewResult:
        res = ReviewResult(files=[path], sources={path: src})
        modules, lines = parse_source(src, path)
        res.modules.extend(modules)
        if index is None:
            index = ModuleIndex()
            index.add_source(src, path)
        ctx = {"lines": lines, "path": path, "index": index}
        for mod in modules:
            for rid, name, fn in self.rules:
                try:
                    res.findings.extend(fn(mod, ctx))
                except Exception as exc:                      # noqa: BLE001
                    res.rule_errors.append(
                        "%s @ %s: %s: %s" % (rid, path, type(exc).__name__, exc))
        return res

    # -- 文件 / 目录审查 ----------------------------------------------------
    def review_path(self, target: str) -> ReviewResult:
        paths = self.collect(target)
        # 关键顺序: 先建立全工程模块索引（跨文件端口方向与位宽），
        # 再做逐文件审查 —— 否则实例化相关的判断会全面失真。
        index = ModuleIndex()
        index.add_files(paths)

        merged = ReviewResult(index_stats=index.stats())
        for path in paths:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            part = self.review_source(src, path, index=index)
            merged.findings.extend(part.findings)
            merged.modules.extend(part.modules)
            merged.files.extend(part.files)
            merged.sources.update(part.sources)
            merged.rule_errors.extend(part.rule_errors)
        return merged

    @staticmethod
    def collect(target: str) -> List[str]:
        if os.path.isfile(target):
            return [target]
        found: List[str] = []
        for root, _dirs, files in os.walk(target):
            for fn in sorted(files):
                if fn.lower().endswith(SUPPORTED_EXT):
                    found.append(os.path.join(root, fn))
        return found
