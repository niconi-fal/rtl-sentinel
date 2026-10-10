"""轻量 Verilog / SystemVerilog 子集解析器（零第三方依赖）。

设计取舍
--------
不追求 IEEE 1364 完整语法覆盖，而是采用
    「注释剥离 -> 模块边界扫描 -> 块结构配平 -> 语句级正则」
的启发式管线，精确提取静态分析真正需要的四类事实:

    1. 模块边界与参数表   -> 用于位宽常量求值
    2. 信号声明           -> 方向 / 存储类 / 位宽
    3. 过程块             -> 时序或组合、敏感列表、块内赋值、控制结构
    4. 连续赋值与实例化   -> 数据流与驱动关系

工程收益:
    - 零依赖: 任意环境可运行，不依赖商业 EDA license
    - 不抛异常: 残缺/半成品代码也能给出部分结果（面向前端实时审查场景）
    - 行号可追溯: 剥离注释时保留换行符，行号与原文件严格一致
"""
from __future__ import annotations

import bisect
import re
from typing import Dict, List, Optional, Tuple

from .model import AlwaysBlock, Assign, Instance, Module, Signal

# --------------------------------------------------------------------------
# 词法常量
# --------------------------------------------------------------------------

KEYWORDS = {
    "always", "always_ff", "always_comb", "always_latch", "assign", "begin", "end",
    "case", "casex", "casez", "default", "else", "endcase", "endmodule", "for", "forever",
    "function", "endfunction", "if", "initial", "inout", "input", "integer", "localparam",
    "module", "negedge", "output", "parameter", "posedge", "reg", "repeat", "signed",
    "unsigned", "wire", "while", "logic", "bit", "genvar", "generate", "endgenerate",
    "task", "endtask", "wait", "fork", "join", "or", "and", "not", "xor", "nand", "nor",
    "xnor", "buf", "assign", "specify", "endspecify", "defparam", "primitive", "table",
    "endtable", "endprimitive", "typedef", "struct", "enum", "packed", "automatic",
    "static", "interface", "endinterface", "modport", "package", "endpackage", "import",
    "export", "return", "break", "continue", "do", "foreach", "real", "realtime", "time",
    "supply0", "supply1", "tri", "triand", "trior", "wand", "wor", "event", "int",
    "byte", "shortint", "longint", "void", "const", "ref", "output_", "unique", "unique0",
    "priority", "inside", "with", "iff", "disable", "deassign", "force", "release",
    "highz0", "highz1", "scalared", "vectored", "small", "medium", "large", "macromodule",
    "config", "endconfig", "design", "liblist", "cell", "use", "instance", "library",
    "incdir", "include", "timescale", "celldefine", "endcelldefine", "default_nettype",
    "unconnected_drive", "nounconnected_drive", "resetall", "pulldown", "pullup",
    "pull0", "pull1", "strong0", "strong1", "weak0", "weak1", "cmos", "nmos", "pmos",
    "rcmos", "rnmos", "rpmos", "tran", "rtran", "tranif0", "tranif1", "rtranif0",
    "rtranif1", "xor_", "bufif0", "bufif1", "notif0", "notif1", "signed_", "new",
}

IDENT_RE = re.compile(r"[A-Za-z_]\w*")
NUM_RE = re.compile(r"^(?:(\d[\d_]*)\s*)?'\s*([sS]?)\s*([bodhBODH])\s*([0-9a-fA-FxXzZ?_]+)$")
DEC_RE = re.compile(r"^(\d[\d_]*)$")
DECL_KEYWORD_RE = re.compile(
    r"\b(input|output|inout|wire|reg|logic|integer|parameter|localparam|genvar|"
    r"bit|tri|wand|wor|supply0|supply1|real|time|realtime|int|byte|shortint|longint)\b"
)
SAFE_EXPR_RE = re.compile(r"^[0-9A-Za-z_+\-*/()\s]+$")
#: Verilog/SystemVerilog 属性 (* ... *)。它**没有分号终止**，会被"按 ; 切分"的
#: 语句扫描并入下一条语句，使该语句以 `(*` 开头、声明/实例分类失败
#: （实测因此漏掉 axis_pipeline_fifo 的 8 个寄存器声明，并造成 R003 静默漏报）。
ATTR_RE = re.compile(r"\(\s*\*.*?\*\s*\)", re.S)
STRUCT_RE = re.compile(
    r"(?<![\w$])(?:begin|else|generate|case|casex|casez|default)(?![\w$])")
COND_RE = re.compile(r"(?<![\w$])(?:if|for|while|repeat|foreach)\s*\(")
LABEL_RE = re.compile(r"[A-Za-z_]\w*\s*:[^=:]")
LABEL2_RE = re.compile(r":\s*[A-Za-z_]\w*")      # begin : name 的写法（冒号在前）
#: 块结束关键字。它们**没有分号**，会被并入下一条语句，使该语句以 `end...` 开头、
#: 分类失败（实测因此漏掉 axis_async_fifo / axis_demux 的寄存器声明）。
BLOCK_END_RE = re.compile(
    r"(?<![\w$])(?:end|endcase|endfunction|endtask|endmodule|endgenerate|"
    r"endprimitive|endspecify|endtable|endinterface|endpackage|endclass|endprogram|"
    r"join|join_any|join_none)(?![\w$])")
#: 编译器指令（整条占一行）。它们的**参数也不是代码**，必须连参数一起抹掉，
#: 否则参数会粘到下一条语句前（如 `ifndef PICORV32_REGS 后的宏名）。
#: 注意与"用户宏当属性用"的情形区分（如 `FORMAL_KEEP reg x;）—— 那种只抹记号。
DIRECTIVE_RE = re.compile(
    r"(?<![\w`])`(?:ifdef|ifndef|elsif|else|endif|define|undef|include|timescale|"
    r"default_nettype|resetall|celldefine|endcelldefine|unconnected_drive|"
    r"nounconnected_drive|line|pragma|begin_keywords|end_keywords)\b")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def strip_comments(text: str) -> str:
    """剥离注释但保留换行符，保证行号与原文件一一对应。"""
    out: List[str] = []
    i, n = 0, len(text)
    in_line = in_block = in_str = False
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line:
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                in_line = False
            i += 1
        elif in_block:
            if c == "*" and nxt == "/":
                out.append("  ")
                in_block = False
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
        elif in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                if c == '"':
                    in_str = False
                i += 1
        elif c == "/" and nxt == "/":
            out.append("  ")
            in_line = True
            i += 2
        elif c == "/" and nxt == "*":
            out.append("  ")
            in_block = True
            i += 2
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def split_top_level(text: str, sep: str = ",") -> List[str]:
    """按顶层分隔符切分，忽略括号内的分隔符。"""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    in_str = False
    for c in text:
        if in_str:
            if c == '"':
                in_str = False
            buf.append(c)
            continue
        if c == '"':
            in_str = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def match_block_end(text: str, start: int) -> int:
    r"""从 start 起做 begin/end 配平，返回块尾偏移（含 end）。

    正则中的 (?<![\w$]) / (?![\w$]) 可自动排除 endcase / endmodule /
    endfunction 等复合关键字。
    """
    depth = 0
    for m in re.finditer(r"(?<![\w$])(begin|end)(?![\w$])", text[start:]):
        if m.group(1) == "begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return start + m.end()
    return len(text)


def take_block(text: str, start: int) -> str:
    """从 start 起取一个完整语句块：有 begin 则配平到 end，否则取到分号。"""
    j = start
    while j < len(text) and text[j].isspace():
        j += 1
    if re.match(r"(?<![\w$])begin(?![\w$])", text[j:]):
        return text[start:match_block_end(text, j)]
    semi = text.find(";", start)
    return text[start: semi + 1] if semi >= 0 else text[start:]


def top_level_groups(text: str) -> List[Tuple[int, int]]:
    """返回文本中所有顶层括号组的 (开括号下标, 闭括号下标)。"""
    groups: List[Tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "(":
            depth, j = 1, i + 1
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            groups.append((i, j - 1))
            i = j
        else:
            i += 1
    return groups


def strip_control_prefix(stmt: str) -> str:
    """剥掉 if (...) / else / begin / end / case 标签等前缀，露出真正的赋值语句。"""
    s = stmt.strip()
    for _ in range(8):
        before = s
        m = re.match(r"^if\s*\(", s)
        if m:
            depth, k = 0, s.index("(")
            for idx in range(k, len(s)):
                if s[idx] == "(":
                    depth += 1
                elif s[idx] == ")":
                    depth -= 1
                    if depth == 0:
                        s = s[idx + 1:].strip()
                        break
        elif re.match(r"^else\b", s):
            s = re.sub(r"^else\b", "", s).strip()
        elif re.match(r"^(case|casex|casez)\b", s):
            s = re.sub(r"^(case|casex|casez)\b\s*\(?[^)]*\)?\s*", "", s).strip()
        elif re.match(r"^(begin|end)\b", s):
            s = re.sub(r"^(begin|end)\b", "", s).strip()
        elif re.match(r"^default\s*:", s):
            s = re.sub(r"^default\s*:", "", s).strip()
        else:
            m2 = re.match(r"^([^=<>;]*?):\s*", s)
            if m2 and len(m2.group(1)) <= 48 and "(" not in m2.group(1):
                s = s[m2.end():].strip()
        if s == before:
            break
    return s


def eval_const(expr: str, params: Dict[str, int]) -> Optional[int]:
    """在受限沙箱中求值常量表达式，如 'WIDTH-1' / '8' / 'ADDR_W*2'。"""
    expr = expr.strip()
    if not expr or not SAFE_EXPR_RE.match(expr):
        return None
    try:
        return int(eval(expr, {"__builtins__": {}}, dict(params)))  # noqa: S307
    except Exception:
        return None


def parse_literal(expr: str) -> Tuple[Optional[int], Optional[int]]:
    """解析 Verilog 字面量，返回 (值, 显式位宽)。无显式位宽时位宽为 None。"""
    expr = expr.strip()
    m = NUM_RE.match(expr)
    if m:
        size, _, base, digits = m.groups()
        base = base.lower()
        digits = digits.replace("_", "")
        if re.search(r"[xXzZ?]", digits):
            return None, int(size) if size else None
        try:
            val = int(digits, {"b": 2, "o": 8, "d": 10, "h": 16}[base])
        except ValueError:
            return None, None
        return val, (int(size) if size else None)
    m = DEC_RE.match(expr)
    if m:
        return int(m.group(1).replace("_", "")), None
    return None, None


def min_width(value: int) -> int:
    """某无符号整数所需的最小位宽。"""
    if value <= 0:
        return 1
    return value.bit_length()


def read_identifiers(text: str) -> List[str]:
    """提取文本中的标识符，过滤关键字。"""
    return [t for t in IDENT_RE.findall(text) if t not in KEYWORDS]


# --------------------------------------------------------------------------
# 声明解析
# --------------------------------------------------------------------------

def strip_directives(text: str) -> str:
    """清掉文本里的编译器指令记号（连同紧跟的宏名参数），长度保持不变。

    端口表 / 声明文本里也可能夹带指令，例如 picorv32 的 ANSI 端口表：
        `ifdef RISCV_FORMAL
            output rvfi_valid,
        `endif
    按逗号切分后，条目会以反引号开头 —— 若不清理，方向/存储类匹配不上，
    宏名反而会被当成信号名（实测因此漏掉 rvfi_valid / trace_valid）。
    只吃"宏名"这一种参数、且要求它不是 Verilog 关键字 —— 这样
    `FORMAL_KEEP reg [63:0] x; 里的 reg 不会被吃掉。
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "`":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            k = j
            while k < n and text[k].isspace():
                k += 1
            m = re.match(r"[A-Za-z_]\w*", text[k:])
            if m and m.group(0) not in KEYWORDS:
                k += m.end()
            out.append(" " * (k - i))
            i = k
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def parse_decl_text(
    text: str,
    line: int,
    params: Dict[str, int],
    signals: Dict[str, Signal],
    inherit: Optional[Dict[str, object]] = None,
) -> None:
    """解析一条声明语句，结果写入 signals。

    inherit 用于 ANSI 风格端口列表的「类型继承」:
        module m(input clk, rst_n, output reg [7:0] q)
        其中 rst_n 继承 input 方向。
    """
    text = text.strip().rstrip(",").strip()
    # 声明文本里可能夹带属性（如 `input (* foo *) wire x`），先清掉再分类
    text = ATTR_RE.sub(" ", text).strip()
    text = strip_directives(text).strip()
    if not text:
        return

    direction = ""
    storage = ""
    signed = False
    msb = lsb = None
    width_unknown = False
    width_param_dependent = False
    const_value: Optional[int] = None

    m = re.match(r"^(input|output|inout)\b(.*)$", text, re.S)
    body = text
    if m:
        direction = m.group(1)
        body = m.group(2).strip()

    m = re.match(r"^(wire|reg|logic|integer|genvar|bit|tri|wand|wor|real|time|"
                 r"realtime|int|byte|shortint|longint|parameter|localparam|supply0|supply1)\b(.*)$",
                 body, re.S)
    if m:
        storage = m.group(1)
        body = m.group(2).strip()

    if re.match(r"^signed\b", body):
        signed = True
        body = re.sub(r"^signed\b", "", body).strip()
    elif re.match(r"^unsigned\b", body):
        body = re.sub(r"^unsigned\b", "", body).strip()

    rm = re.match(r"^\[\s*([^:\]]+?)\s*(?::\s*([^\]]+?)\s*)?\](.*)$", body, re.S)
    if rm:
        msb = eval_const(rm.group(1), params)
        if msb is None:
            # 位宽表达式依赖未求值的参数（如 [regindex_bits-1:0]）——
            # 此时必须标记为"未知"而不是退化成 1 位，否则 R002 会把
            # 所有赋值都误判为位宽截断。
            width_unknown = True
        # 位宽由 parameter 决定时，实例可通过 #(...) 覆写其值，
        # 用定义处的默认值做跨模块位宽比对会产生误报（典型如 axis_adapter）。
        width_param_dependent = bool(re.search(r"[A-Za-z_]", rm.group(1)))
        lsb = eval_const(rm.group(2), params) if rm.group(2) else 0
        body = rm.group(3).strip()

    if not direction and not storage:
        # 纯标识符列表（ANSI 端口继承场景）
        if inherit is None:
            return
        direction = str(inherit.get("direction", ""))
        storage = str(inherit.get("storage", "wire"))
        msb = inherit.get("msb")          # type: ignore[assignment]
        lsb = inherit.get("lsb")          # type: ignore[assignment]
        signed = bool(inherit.get("signed", False))
    elif storage and not direction and inherit is not None:
        direction = str(inherit.get("direction", ""))
    if not storage:
        storage = "wire"

    for part in split_top_level(body, ","):
        part = part.strip()
        if not part or part.startswith("@"):
            continue
        nm = re.match(r"^([A-Za-z_]\w*)", part)
        if not nm:
            continue
        name = nm.group(1)
        # 名字之后若还有一对方括号，说明这是数组维度声明
        # （如 reg [7:0] mem [0:15]）—— 此时 mem[i] 是元素下标而非位选。
        is_array = bool(re.match(r"^\s*\[", part[nm.end():]))
        val: Optional[int] = None
        if "=" in part:
            rhs = part.split("=", 1)[1].strip()
            val = eval_const(rhs, params)
            if val is None:
                lit, _w = parse_literal(rhs)
                val = lit
        sig = signals.get(name)
        if sig is None:
            sig = Signal(name=name, line=line)
            signals[name] = sig
        if direction:
            sig.direction = direction
        if storage:
            sig.storage = storage
        if msb is not None:
            sig.msb, sig.lsb = msb, lsb if lsb is not None else 0
            sig.width_unknown = False
            sig.width_param_dependent = width_param_dependent
        elif width_unknown:
            sig.msb, sig.lsb = None, None
            sig.width_unknown = True
        if is_array:
            sig.is_array = True
        sig.signed = sig.signed or signed
        if val is not None:
            sig.const_value = val
            params[name] = val

    if inherit is not None:
        inherit["direction"] = direction
        inherit["storage"] = storage
        inherit["msb"] = msb
        inherit["lsb"] = lsb
        inherit["signed"] = signed


# --------------------------------------------------------------------------
# 过程块解析
# --------------------------------------------------------------------------

ASSIGN_STMT_RE = re.compile(
    r"^([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(<=|=)(?![=<>])\s*(.+)$", re.S
)
# 在语句内部搜索赋值目标（不要求行首），用于取得精确行号
ASSIGN_IN_STMT_RE = re.compile(
    r"([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(<=|=)(?![=<>])"
)


def _for_head_spans(body: str) -> List[Tuple[int, int]]:
    """返回所有 for 循环头的 (起始偏移, 结束偏移)。

    for 头形如 `for (i = 0; i < 4; i = i + 1)` —— 其中的 `i = 0` 是循环控制，
    不是时序逻辑赋值。若不排除，R003 会把循环变量误判为"时序块阻塞赋值"，
    产生高频误报（每个含 for 的时序块都会中招）。
    """
    spans: List[Tuple[int, int]] = []
    n = len(body)
    for m in re.finditer(r"(?<![\w$])for\s*\(", body):
        depth, j = 1, m.end()
        while j < n and depth:
            if body[j] == "(":
                depth += 1
            elif body[j] == ")":
                depth -= 1
            j += 1
        spans.append((m.start(), j))
    return spans


def parse_always_body(body: str, base_line: int) -> Tuple[List[Assign], Dict[str, bool]]:
    """扫描过程块体，提取赋值语句与控制结构特征。

    行号策略: 在「未做前缀剥离」的原始语句内直接正则定位赋值目标，
    以匹配位置换算绝对行号 —— 避免跨行语句（如 case 项内的赋值）
    因语句起始行与实际赋值行不同而产生 1~2 行偏移。
    """
    assigns: List[Assign] = []
    flags = {
        "has_if": bool(re.search(r"(?<![\w$])if(?![\w$])", body)),
        "has_else": bool(re.search(r"(?<![\w$])else(?![\w$])", body)),
        "has_case": bool(re.search(r"(?<![\w$])(case|casex|casez)(?![\w$])", body)),
        "has_default": bool(re.search(r"(?<![\w$])default(?![\w$])\s*:", body)),
        # `(* full_case *)` / `(* parallel_case *)` 是综合属性，声明综合器可假定
        # 分支已完全覆盖 —— 此时缺 default 不会推断 latch。
        "has_full_case": bool(re.search(r"full_case|parallel_case", body)),
        "has_reset": bool(re.search(r"(?<![\w$])(rst|reset|areset|rst_n|reset_n|n_rst|"
                                    r"srst|arst)(?![\w$])", body, re.I)),
    }
    for_spans = _for_head_spans(body)
    # 预计算括号深度表: depth_arr[k] = body[:k] 的括号嵌套深度。
    # 用途: Verilog 的 "<=" 有两种语义 —— 非阻塞赋值 与 小于等于比较。
    #   语句顶层        x <= y;            非阻塞赋值
    #   括号内部   if (a <= b) begin ...   比较运算
    # 用括号深度可以无歧义地区分二者，避免把条件表达式中的比较
    # 误判为对信号赋值（曾在真实语料上污染 assigned 集合，制造假多驱动）。
    depth_arr = [0] * (len(body) + 1)
    _d = 0
    for _i, _ch in enumerate(body):
        if _ch == "(":
            _d += 1
        elif _ch == ")":
            _d -= 1
        depth_arr[_i + 1] = _d

    def _in_for_head(off: int) -> bool:
        return any(s <= off < e for s, e in for_spans)

    stmt_start = 0
    i = 0
    n = len(body)
    while i < n:
        if body[i] == ";":
            if _in_for_head(i):
                i += 1                     # for 头内的分号不作为语句边界
                continue
            stmt = body[stmt_start:i]
            m = ASSIGN_IN_STMT_RE.search(stmt)
            while m is not None:
                abs_off = stmt_start + m.start()
                if _in_for_head(abs_off) or depth_arr[abs_off] > 0:
                    m = ASSIGN_IN_STMT_RE.search(stmt, m.end())
                    continue
                break
            if m and m.group(1) not in KEYWORDS:
                abs_off = stmt_start + m.start()
                rhs = stmt[m.end():].strip()
                if ";" not in rhs:
                    assigns.append(
                        Assign(
                            lhs=m.group(1),
                            op=m.group(3),
                            expr=rhs,
                            line=base_line + body.count("\n", 0, abs_off),
                            bit_select=(m.group(2) or ""),
                        )
                    )
            stmt_start = i + 1
        i += 1
    # 去重: 同一声明位置的重复记录（如复位分支与正常分支）保留首次出现
    seen = set()
    unique: List[Assign] = []
    for a in assigns:
        key = (a.lhs, a.op, a.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)
    return unique, flags


def parse_sensitivity(sens_text: str) -> Tuple[List[str], bool, Optional[str], str]:
    """解析敏感列表，返回 (信号名列表, 是否时序, 时钟名, 边沿类型)。"""
    s = sens_text.strip()
    if s in ("", "*"):
        return ["*"], False, None, "comb"
    parts = [p.strip() for p in re.split(r"\bor\b|,", s) if p.strip()]
    names: List[str] = []
    edges: List[str] = []
    clock: Optional[str] = None
    for p in parts:
        em = re.match(r"^(posedge|negedge)\s+([A-Za-z_]\w*)", p)
        if em:
            edges.append(em.group(1))
            names.append(em.group(2))
            if clock is None:
                clock = em.group(2)
            continue
        nm = re.match(r"^\(?\s*([A-Za-z_]\w*)", p)
        if nm:
            names.append(nm.group(1))
    is_seq = bool(edges)
    if not is_seq:
        return names or ["*"], False, None, "comb"
    if len(set(edges)) == 1:
        return names, True, clock, edges[0]
    return names, True, clock, "async"


ALWAYS_RE = re.compile(r"(?<![\w$])(always(?:_ff|_comb|_latch)?)(?![\w$])")


def extract_always_blocks(body: str, offset: int, line_starts: List[int]) -> List[AlwaysBlock]:
    """从模块体中提取全部过程块。"""
    blocks: List[AlwaysBlock] = []
    for m in ALWAYS_RE.finditer(body):
        kw = m.group(1)
        i = m.end()
        while i < len(body) and body[i].isspace():
            i += 1
        sens_text = "*" if kw in ("always_comb",) else ""
        if i < len(body) and body[i] == "@":
            i += 1
            while i < len(body) and body[i].isspace():
                i += 1
            if i < len(body) and body[i] == "(":
                depth, j = 1, i + 1
                while j < len(body) and depth:
                    if body[j] == "(":
                        depth += 1
                    elif body[j] == ")":
                        depth -= 1
                    j += 1
                sens_text = body[i + 1: j - 1]
                i = j
            elif i < len(body) and body[i] == "*":
                sens_text = "*"
                i += 1
        elif kw in ("always_comb", "always_latch"):
            sens_text = "*"

        blk_text = take_block(body, i)
        start_off = offset + m.start()
        end_off = offset + i + len(blk_text)
        line_start = bisect.bisect_right(line_starts, start_off)
        line_end = bisect.bisect_right(line_starts, end_off)

        inner = body[i: i + len(blk_text)]
        # begin 后的基准行 = 块体首行的下一行
        base_line = line_start + inner.count("\n", 0, inner.find("begin") + 1)
        assigns, flags = parse_always_body(inner, base_line)

        names, is_seq, clock, edge = parse_sensitivity(sens_text)
        if kw == "always_ff" and not is_seq:
            is_seq, clock, edge = False, None, "comb"

        # 找出「块首无条件赋值」的信号 —— 这类信号有默认值，不会推断 latch。
        # 关键: 必须匹配未剥前缀的原始首句, 否则 case 标签内的首条赋值会被
        # 误判为默认赋值, 造成 latch 漏检。
        default_assigned: List[str] = []
        bpos = inner.find("begin")
        if bpos >= 0:
            seg = inner[bpos + len("begin"):]
            semi = seg.find(";")
            first_raw = (seg[:semi] if semi >= 0 else seg).strip()
            fm = ASSIGN_STMT_RE.match(first_raw)
            if fm and fm.group(1) not in KEYWORDS:
                default_assigned.append(fm.group(1))

        blocks.append(
            AlwaysBlock(
                line_start=line_start,
                line_end=line_end,
                sens=names,
                is_seq=is_seq,
                clock=clock,
                edge=edge,
                source=inner,
                assigns=assigns,
                default_assigned=default_assigned,
                **flags,
            )
        )
    return blocks


# --------------------------------------------------------------------------
# 模块解析
# --------------------------------------------------------------------------

MODULE_RE = re.compile(r"(?<![\w$])module(?:\s+(?:automatic|static))?\s+([A-Za-z_]\w*)")
ASSIGN_CONT_RE = re.compile(r"(?<![\w$])assign(?![\w$])\s+([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*=")


def _generate_line_numbers(clean: str) -> set:
    """返回处于 generate ... endgenerate 区间内的行号集合。

    参数化 generate 的 if/else 分支是**互斥**的：同一信号在不同分支里被赋值
    并不构成多驱动。典型如 picorv32 的 TWO_CYCLE_ALU —— 单周期走组合块、
    多周期走时序块，两者不会同时存在，但静态看是"两个 always 驱动同一信号"。
    """
    inside: set = set()
    depth = 0
    for idx, line in enumerate(clean.split("\n"), start=1):
        s = line.strip()
        if re.search(r"(?<![\w$])generate(?![\w$])", s):
            depth += 1
            inside.add(idx)
            continue
        if re.search(r"(?<![\w$])endgenerate(?![\w$])", s):
            depth = max(0, depth - 1)
            inside.add(idx)
            continue
        if depth > 0:
            inside.add(idx)
    return inside


def _ifdef_line_numbers(clean: str) -> set:
    """返回处于 `ifdef / `ifndef ... `endif 条件编译块内的行号集合。

    用途: 条件编译块内的声明是否被使用取决于宏定义，静态分析无法判定，
    R008（未使用信号）对这类信号必须沉默，否则会大量误报
    （典型如 picorv32 的 `ifdef DEBUGREGS 调试寄存器）。
    """
    inside: set = set()
    depth = 0
    for idx, line in enumerate(clean.split("\n"), start=1):
        s = line.strip()
        if re.match(r"`\s*(ifdef|ifndef)\b", s):
            depth += 1
            continue
        if re.match(r"`\s*endif\b", s):
            depth = max(0, depth - 1)
            continue
        if depth > 0:
            inside.add(idx)
    return inside


def parse_source(raw: str, path: str = "<memory>") -> Tuple[List[Module], List[str]]:
    """解析一个 Verilog 源文件，返回 (模块列表, 原始行列表)。"""
    clean = strip_comments(raw)
    raw_lines = raw.split("\n")
    line_starts = [0]
    for m in re.finditer("\n", clean):
        line_starts.append(m.end())
    ifdef_lines = _ifdef_line_numbers(clean)
    generate_lines = _generate_line_numbers(clean)

    modules: List[Module] = []
    for mm in MODULE_RE.finditer(clean):
        name = mm.group(1)
        start = mm.start()
        em = re.search(r"(?<![\w$])endmodule(?![\w$])", clean[start:])
        end = start + (em.end() if em else len(clean) - start)
        header_end = clean.find(";", mm.end())
        if header_end < 0 or header_end > end:
            header_end = mm.end()
        header = clean[mm.end(): header_end]
        body_offset = header_end + 1
        body = clean[body_offset:end]

        line_start = bisect.bisect_right(line_starts, start)
        line_end = bisect.bisect_right(line_starts, end)

        mod = Module(
            name=name,
            line_start=line_start,
            line_end=line_end,
            file=path,
            body=body,
            body_offset=body_offset,
            body_line=bisect.bisect_right(line_starts, body_offset),
        )

        # --- 头部: 参数列表 (#(...)) 与 ANSI 端口列表 ((...)) ---
        param_text: Optional[str] = None
        param_off = 0
        port_text: Optional[str] = None
        port_off = 0
        for gs, ge in top_level_groups(header):
            inner_g = header[gs + 1: ge]
            if header[:gs].rstrip().endswith("#"):
                param_text, param_off = inner_g, mm.end() + gs + 1
            else:
                port_text, port_off = inner_g, mm.end() + gs + 1

        def _locate(block: str, part: str, base_off: int, cursor: List[int]) -> int:
            """在块内定位片段偏移并换算绝对行号（游标递进，避免重复片段错位）。"""
            pos = block.find(part, cursor[0])
            if pos < 0:
                pos = cursor[0]
            cursor[0] = pos + max(len(part), 1)
            return bisect.bisect_right(line_starts, base_off + pos)

        if param_text:
            ph: Dict[str, object] = {"direction": "", "storage": "parameter",
                                     "msb": None, "lsb": None, "signed": False}
            cur = [0]
            for part in split_top_level(param_text, ","):
                if part.strip():
                    pline = _locate(param_text, part, param_off, cur)
                    parse_decl_text(part, pline, mod.params, mod.signals, ph)
        if port_text:
            inherit: Dict[str, object] = {"direction": "", "storage": "wire",
                                          "msb": None, "lsb": None, "signed": False}
            cur = [0]
            for part in split_top_level(port_text, ","):
                if not part.strip():
                    continue
                pline = _locate(port_text, part, port_off, cur)
                parse_decl_text(part, pline, mod.params, mod.signals, inherit)

        # --- 模块体: 声明 / 过程块 / 连续赋值 / 实例化 ---
        _parse_body(body, body_offset, line_starts, mod, line_start)

        # 标记条件编译块内的声明（其使用与否取决于宏定义）
        mod.ifdef_lines = ifdef_lines
        mod.generate_lines = generate_lines
        for sig in mod.signals.values():
            if sig.line in ifdef_lines:
                sig.in_ifdef = True

        modules.append(mod)
    return modules, raw_lines


def _skip_preamble(text: str, k: int, limit: int) -> int:
    """跳过语句开头的"结构噪声"，让被并进来的声明重新可分类。

    Verilog 里有一批**没有分号**的记号：属性 (* ... *)、块结束词
    end / endfunction / join ...、begin / else / generate / case、
    if (...) / for (...) 头、以及 label:。按 ; 切分语句时它们会被并进下一条
    语句，使该语句无法被识别（实测因此在 picorv32 上漏掉过寄存器声明）。

    这里**只跳这些结构关键字**，不跳普通标识符 —— 因此不会把 generate 里的实例
    误当成模块级实例（那类语句仍以普通标识符开头，行为与改动前一致）。
    """
    while k < limit:
        while k < limit and text[k].isspace():
            k += 1
        m = ATTR_RE.match(text, k)
        if m and m.end() > k:
            k = m.end()
            continue
        m = BLOCK_END_RE.match(text, k) or STRUCT_RE.match(text, k)
        if m and m.end() > k:
            k = m.end()
            continue
        m = COND_RE.match(text, k)
        if m:
            depth, j = 1, m.end()
            while j < limit and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            k = j
            continue
        m = LABEL_RE.match(text, k)
        if not (m and m.end() > k):
            m = LABEL2_RE.match(text, k)
        if m and m.end() > k:
            k = m.end()
            continue
        break
    return k



def _parse_body(
    body: str,
    offset: int,
    line_starts: List[int],
    mod: Module,
    default_line: int,
) -> None:
    """扫描模块体，填充 signals / always_blocks / assigns / instances。"""
    # 1) 先移除过程块文本，避免其内部语句被误判为声明或实例
    masked = list(body)
    for m in ALWAYS_RE.finditer(body):
        i = m.end()
        while i < len(body) and body[i].isspace():
            i += 1
        if i < len(body) and body[i] == "@":
            i += 1
            while i < len(body) and body[i].isspace():
                i += 1
            if i < len(body) and body[i] == "(":
                depth, j = 1, i + 1
                while j < len(body) and depth:
                    if body[j] == "(":
                        depth += 1
                    elif body[j] == ")":
                        depth -= 1
                    j += 1
                i = j
            elif i < len(body) and body[i] == "*":
                i += 1
        blk = take_block(body, i)
        for k in range(m.start(), min(i + len(blk), len(masked))):
            if masked[k] != "\n":
                masked[k] = " "
    scan = "".join(masked)

    # 1b) 再把"预处理指令行"整行抹白（`ifdef / `define / `timescale ...）：
    #     它们**没有分号**，否则会被并入下一条语句，使该语句以反引号开头、
    #     分类失败 —— 实测因此漏掉 picorv32 里 `ifndef PICORV32_REGS 包住的
    #     17 个寄存器声明。抹白保留换行与字符偏移，行号不受影响。
    #     只抹掉**指令记号本身**（`ifndef / `FORMAL_KEEP ...），保留同一行里
    #     跟在后面的真实代码（如 `FORMAL_KEEP reg [63:0] dbg_ascii_instr;）。
    masked2 = list(scan)
    for m in re.finditer(r"(?<![\w`])`\w+", scan):
        end = m.end()
        if DIRECTIVE_RE.match(scan, m.start()):
            # 只把紧随其后的**宏名参数**一并抹掉；**不能抹到行尾** ——
            # 同一行后面可能还有真实代码（如 "`endif  end  reg x;"）。
            mm2 = re.compile(r"\s+([A-Za-z_]\w*)").match(scan, end)
            if mm2 and mm2.group(1) not in KEYWORDS:
                end = mm2.end()
        for k in range(m.start(), end):
            if masked2[k] != "\n":
                masked2[k] = " "
    scan = "".join(masked2)

    # 1c) 抹白 `for (...)` 头：头里的 `;`（`for (i = 0; i < 4; i = i + 1)`）不是
    #     语句结束符。always 体内已有 _for_head_spans 处理，但函数体 / generate-for
    #     的 for 头同样会切坏语句 —— 实测因此漏掉 picorv32 与 axis_ram_switch 的声明。
    masked3 = list(scan)
    # 注意：区间必须在**未抹白的原文 body** 上计算 —— 抹白会把括号也变成空格，
    # 使括号失配、for 头区间被算成一大段、连带把中间的声明与分号一起抹掉
    # （实测在 picorv32 上造成过这种自伤）。掩码都保持长度不变，偏移可直接用。
    # 另加长度保护：真正的 for 头很短，超长区间一律视为失配、不抹。
    for s, e in _for_head_spans(body):
        if e - s > 500:
            continue
        for k in range(s, min(e, len(masked3))):
            if masked3[k] != "\n":
                masked3[k] = " "
    scan = "".join(masked3)

    # 1d) 抹白字符串字面量内容：串里的 `;`（$display("...; ...")）会被误当作语句结束。
    masked4 = list(scan)
    k, total = 0, len(scan)
    while k < total:
        if scan[k] == '"':
            j = k + 1
            while j < total and scan[j] != '"' and scan[j] != "\n":
                j += 2 if scan[j] == "\\" else 1
            for q in range(k + 1, min(j, total)):
                if masked4[q] != "\n":
                    masked4[q] = " "
            k = j + 1
        else:
            k += 1
    scan = "".join(masked4)

    # 2) 语句级扫描
    stmt_start = 0
    i = 0
    n = len(scan)
    while i < n:
        c = scan[i]
        if c == ";":
            stmt = scan[stmt_start:i]
            # 跳过语句前导空白（上一行的换行 + 缩进），否则行号会偏小一行
            k = stmt_start
            while k < i and scan[k].isspace():
                k += 1
            # 跳过语句开头的结构噪声（属性 / end* / begin / if(...) / 标签…），
            # 把行号推进到真正的语句起始处 —— 这些记号都没有分号，必然被并进来。
            k = _skip_preamble(scan, k, i)
            line = bisect.bisect_right(line_starts, offset + k)
            text = scan[k:i]
            if text.strip() and not text.lstrip().startswith(("module", "endmodule")):
                _classify_statement(text.strip(), line, mod)
            stmt_start = i + 1
        i += 1

    # 3) 过程块
    mod.always_blocks = extract_always_blocks(body, offset, line_starts)

    # 4) 连续赋值
    for m in ASSIGN_CONT_RE.finditer(body):
        line = bisect.bisect_right(line_starts, offset + m.start())
        tail = body[m.end():]
        semi = tail.find(";")
        expr = tail[:semi].strip() if semi >= 0 else tail.strip()
        # 跳过形如 "assign a = 1'b0;" 之外的多重赋值
        mod.assigns.append(
            Assign(lhs=m.group(1), op="=", expr=expr, line=line,
                   bit_select=(m.group(2) or ""))
        )


def _match_paren(text: str, start: int) -> int:
    """text[start] 必须是 '('，返回与之配对的 ')' 的下一个位置。"""
    depth = 0
    i, n = start, len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _parse_instance(stmt: str, line: int, mod: Module) -> bool:
    """解析模块实例化语句，成功返回 True。

    为什么不用单一大正则: 实例化含两层嵌套括号 ——
        picorv32 #(.A(a), .B(b)) u_core (.clk(clk), .rst(rst));
    参数覆写与端口连接里各自还有子括号，用非贪婪正则会因回溯而切错边界，
    把参数列表混进端口连接（曾因此在真实语料上产生 R013 误报）。
    改用括号配平逐段推进，边界可靠。
    """
    m = re.match(r"^([A-Za-z_]\w*)", stmt)
    if not m or m.group(1) in KEYWORDS:
        return False
    module_type = m.group(1)
    pos, n = m.end(), len(stmt)

    while pos < n and stmt[pos].isspace():
        pos += 1

    overrides = False
    if pos < n and stmt[pos] == "#":             # 参数覆写 #( ... )
        pos += 1
        while pos < n and stmt[pos].isspace():
            pos += 1
        if pos < n and stmt[pos] == "(":
            pos = _match_paren(stmt, pos)
            overrides = True

    while pos < n and stmt[pos].isspace():
        pos += 1
    m2 = re.match(r"([A-Za-z_]\w*)", stmt[pos:])
    if not m2 or m2.group(1) in KEYWORDS:
        return False
    instance_name = m2.group(1)
    pos += m2.end()

    while pos < n and stmt[pos].isspace():
        pos += 1
    if pos >= n or stmt[pos] != "(":             # 端口连接 ( ... )
        return False
    end = _match_paren(stmt, pos)
    # 端口列表中常嵌入条件编译指令（`ifdef / `else / `endif），例如
    #     .clk(clk),
    #     `ifdef RISCV_FORMAL
    #     .rvfi_valid(rvfi_valid),
    #     `endif
    # 按逗号切分后，这些项会以反引号开头，若不剥离就会被误判为"按位置连接"，
    # 进而使 R013（混用连接方式）误报 —— 实测在 picorv32 上产生 2 条假告警。
    conns: List[str] = []
    for raw in split_top_level(stmt[pos + 1:end - 1], ","):
        item = re.sub(r"^`\s*\w+[^\n]*\n?", "", raw.strip()).strip()
        if item:
            conns.append(item)

    mod.instances.append(Instance(
        module_type=module_type, instance_name=instance_name,
        connections=conns, line=line, overrides=overrides))

    # 端口连接中的信号登记到符号表，避免误报「未声明」
    for c in conns:
        for nm in read_identifiers(c):
            if nm not in mod.signals:
                mod.signals[nm] = Signal(name=nm, line=line, storage="_implicit")
    return True


def _classify_statement(stmt: str, line: int, mod: Module) -> None:
    """判断一条模块体语句是声明还是实例化。"""
    if DECL_KEYWORD_RE.match(stmt) or re.match(r"^\s*parameter\b", stmt):
        parse_decl_text(stmt, line, mod.params, mod.signals)
        return
    if stmt.startswith(("assign", "defparam", "generate", "endgenerate", "if",
                        "for", "case", "endcase", "initial", "function", "task",
                        "specify", "always", "localparam")):
        return
    _parse_instance(stmt, line, mod)


def parse_file(path: str) -> Tuple[List[Module], List[str]]:
    """解析磁盘上的 Verilog 文件。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    return parse_source(raw, path)
