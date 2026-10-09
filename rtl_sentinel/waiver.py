"""豁免（waiver）机制 —— 让工具在真实工程中可用。

为什么必需
----------
工业 Lint 工具都有豁免机制。真实项目必然存在"技术上命中规则、但设计者
有意为之"的代码（行为仿真模型、块内临时变量、有意位宽截断 —— 实测占
成熟开源代码告警的 79%）。若没有官方豁免通道，工程师只能：

    ① 关掉整条规则        -> 丢失该规则的检出能力
    ② 报告里人工忽略      -> 噪音累积，最终弃用工具
    ③ 散落的 disable 注释 -> 不可审计、无人知道为什么豁免

本模块提供**集中式、强制带理由、可审计**的豁免配置。

配置格式（`.rtlsentinel.json`）
------------------------------
{
  "waivers": [
    {"file": "spiflash.v",  "rule": "R004", "reason": "行为仿真模型，非综合目标"},
    {"file": "picorv32.v",  "rule": "R003", "lines": [1406, 1407],
     "reason": "块内顺序计算风格"},
    {"file": "axis_*.v",    "rule": "R007", "reason": "AXI 库的仿真断言"}
  ]
}

匹配字段均为可选，缺省表示匹配全部：
    file  —— 文件名 glob（对 basename 与完整路径分别匹配）
    rule  —— 规则 ID，如 "R003"
    lines —— 行号列表

设计原则：**豁免不隐藏**。被豁免的告警仍会出现在报告中，只是单独归入
"已豁免"区并注明理由 —— 保持透明度，避免豁免变成问题藏匿手段。
"""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

CONFIG_NAME = ".rtlsentinel.json"


@dataclass
class Waiver:
    """单条豁免规则。"""

    reason: str
    file: str = ""
    rule: str = ""
    lines: List[int] = field(default_factory=list)

    def matches(self, finding) -> bool:
        if self.rule and self.rule != finding.rule_id:
            return False
        if self.lines and finding.line not in self.lines:
            return False
        if self.file:
            base = os.path.basename(finding.file)
            norm = finding.file.replace("\\", "/")
            if not (fnmatch.fnmatch(base, self.file)
                    or fnmatch.fnmatch(norm, self.file)
                    or fnmatch.fnmatch(base, "*" + self.file)):
                return False
        return True

    def describe(self) -> str:
        parts = []
        if self.file:
            parts.append("文件 %s" % self.file)
        if self.rule:
            parts.append("规则 %s" % self.rule)
        if self.lines:
            parts.append("行 %s" % ",".join(str(n) for n in self.lines))
        return " ".join(parts) or "全部告警"


class WaiverSet:
    """一组豁免规则的集合。"""

    def __init__(self, waivers: Optional[List[Waiver]] = None):
        self.waivers: List[Waiver] = waivers or []

    def __len__(self) -> int:
        return len(self.waivers)

    def __bool__(self) -> bool:
        return bool(self.waivers)

    # -- 构造 --------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "WaiverSet":
        items: List[Waiver] = []
        for raw in data.get("waivers", []) or []:
            if not isinstance(raw, dict):
                continue
            # reason 必填: 保证每一条豁免都能被审计
            if not str(raw.get("reason", "")).strip():
                continue
            lines_raw = raw.get("lines", []) or []
            items.append(Waiver(
                reason=str(raw["reason"]).strip(),
                file=str(raw.get("file", "")).strip(),
                rule=str(raw.get("rule", "")).strip().upper(),
                lines=[int(x) for x in lines_raw if str(x).strip().isdigit()],
            ))
        return cls(items)

    @classmethod
    def load(cls, path: str) -> "WaiverSet":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def autodiscover(cls, target: str) -> "WaiverSet":
        """依次在目标目录、其父目录、当前目录查找配置文件。"""
        cands: List[str] = []
        if os.path.isfile(target):
            cands.append(os.path.join(os.path.dirname(os.path.abspath(target)),
                                      CONFIG_NAME))
        else:
            cands.append(os.path.join(os.path.abspath(target), CONFIG_NAME))
        cands.append(os.path.join(os.getcwd(), CONFIG_NAME))
        for cand in cands:
            if os.path.isfile(cand):
                try:
                    return cls.load(cand)
                except (OSError, ValueError):
                    continue
        return cls()

    # -- 匹配 --------------------------------------------------------------
    def match(self, finding) -> Optional[str]:
        """命中则返回豁免理由，否则返回 None。"""
        for w in self.waivers:
            if w.matches(finding):
                return w.reason
        return None

    def apply(self, findings) -> int:
        """对告警列表应用豁免，返回被豁免的条数。"""
        n = 0
        for f in findings:
            reason = self.match(f)
            if reason:
                f.waived = True
                f.waive_reason = reason
                n += 1
        return n
