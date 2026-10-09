"""RTL-Sentinel 数据模型。

三层结构:
    Signal / Assign / AlwaysBlock  -> 解析层产出的静态事实
    Module                         -> 一个 Verilog 模块的完整事实集合
    Finding                        -> 规则层产出的告警，携带 LLM 裁定结果
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
SEVERITY_LABEL = {"error": "错误", "warning": "警告", "info": "提示"}
SEVERITY_WEIGHT = {"error": 10, "warning": 4, "info": 1}

VERDICT_LABEL = {
    "confirmed": "确认为缺陷",
    "false_positive": "判定为误报",
    "uncertain": "需人工确认",
    "": "未送审",
}


@dataclass
class Signal:
    """一个信号/端口的静态属性。"""

    name: str
    direction: str = ""          # input / output / inout / ""
    storage: str = "wire"        # wire / reg / logic / integer / parameter / localparam
    msb: Optional[int] = None
    lsb: Optional[int] = None
    line: int = 0
    signed: bool = False
    const_value: Optional[int] = None
    width_unknown: bool = False   # 位宽表达式含未求值参数（如 [regindex_bits-1:0]）
    width_param_dependent: bool = False  # 位宽由 parameter 决定（实例可覆写）
    is_array: bool = False        # 数组（reg [7:0] mem [0:15]），[N] 是下标而非位选
    in_ifdef: bool = False        # 声明位于 `ifdef 条件编译块内

    @property
    def width(self) -> int:
        if self.msb is None:
            return 1
        return abs(self.msb - self.lsb) + 1

    @property
    def range_text(self) -> str:
        if self.msb is None:
            return ""
        return "[%s:%s]" % (self.msb, self.lsb)

    @property
    def is_port(self) -> bool:
        return self.direction in ("input", "output", "inout")

    @property
    def is_variable(self) -> bool:
        """能否在过程块中作为赋值目标（reg/logic/integer）。"""
        return self.storage in ("reg", "logic", "integer")

    @property
    def is_net(self) -> bool:
        """是否为连线类型（只能由 assign / 实例端口驱动）。"""
        return self.storage in ("wire",) and self.direction != "input"


@dataclass
class Assign:
    """一条赋值语句（过程赋值或连续赋值）。"""

    lhs: str
    op: str                      # '=' 阻塞  /  '<=' 非阻塞
    expr: str
    line: int
    bit_select: str = ""         # 位选/片选，如 [3:0]


@dataclass
class AlwaysBlock:
    """一个过程块（always / always_ff / always_comb）。"""

    line_start: int
    line_end: int
    sens: List[str] = field(default_factory=list)
    is_seq: bool = False
    clock: Optional[str] = None
    edge: str = "comb"           # posedge / negedge / comb
    source: str = ""
    assigns: List[Assign] = field(default_factory=list)
    has_if: bool = False
    has_else: bool = False
    has_case: bool = False
    has_default: bool = False
    has_reset: bool = False
    has_full_case: bool = False
    default_assigned: List[str] = field(default_factory=list)

    @property
    def assigned_signals(self) -> set:
        return {a.lhs for a in self.assigns}

    @property
    def is_comb(self) -> bool:
        return not self.is_seq

    @property
    def is_full_sens(self) -> bool:
        return self.sens == ["*"]


@dataclass
class Instance:
    """一次模块实例化。"""

    module_type: str
    instance_name: str
    connections: List[str] = field(default_factory=list)
    line: int = 0
    overrides: bool = False       # 实例带 #(...) 参数覆写，端口位宽将随之改变


@dataclass
class Module:
    """一个 Verilog 模块的解析结果。"""

    name: str
    line_start: int = 1
    line_end: int = 1
    file: str = ""
    params: Dict[str, int] = field(default_factory=dict)
    signals: Dict[str, Signal] = field(default_factory=dict)
    always_blocks: List[AlwaysBlock] = field(default_factory=list)
    assigns: List[Assign] = field(default_factory=list)
    instances: List[Instance] = field(default_factory=list)
    body: str = ""
    body_offset: int = 0
    body_line: int = 1
    ifdef_lines: Set[int] = field(default_factory=set)
    generate_lines: Set[int] = field(default_factory=set)

    def signal(self, name: str) -> Optional[Signal]:
        return self.signals.get(name)

    def clocks(self) -> List[str]:
        seen: List[str] = []
        for blk in self.always_blocks:
            if blk.is_seq and blk.clock and blk.clock not in seen:
                seen.append(blk.clock)
        return seen


@dataclass
class Finding:
    """一条审查告警。"""

    rule_id: str
    rule_name: str
    severity: str
    line: int
    message: str
    hint: str = ""
    snippet: str = ""
    confidence: float = 1.0
    module: str = ""
    file: str = ""
    tags: List[str] = field(default_factory=list)
    waived: bool = False
    waive_reason: str = ""
    llm_verdict: str = ""
    llm_reason: str = ""
    llm_patch: str = ""
    llm_verify: str = ""

    @property
    def weight(self) -> float:
        return SEVERITY_WEIGHT.get(self.severity, 0) * self.confidence

    @property
    def is_suppressed(self) -> bool:
        return self.llm_verdict == "false_positive" or self.waived

    @property
    def status(self) -> str:
        """三态之外的第四种状态: 已豁免（人工/LLM 判定无需处理）。"""
        if self.waived:
            return "waived"
        return self.llm_verdict or "pending"

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "line": self.line,
            "module": self.module,
            "file": self.file,
            "message": self.message,
            "hint": self.hint,
            "snippet": self.snippet,
            "confidence": round(self.confidence, 3),
            "weight": round(self.weight, 2),
            "llm": {
                "verdict": self.llm_verdict,
                "reason": self.llm_reason,
                "patch": self.llm_patch,
                "verify": self.llm_verify,
            },
        }
