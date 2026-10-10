"""RTL 规则引擎 —— 13 条确定性静态检查规则。

为什么规则引擎必须是确定性的
--------------------------
位宽截断、多驱动、敏感列表完整性这类判断，本质上是**符号计算**而非语义理解。
LLM 在 tokenization 之后会破坏位级语义（把 4'hF 与 8'hFF 当成"看起来都像 0xFF"），
因此这部分必须由可复现的规则实现：零随机性、零误报、可写进测试用例。

规则清单
--------
R001 推断锁存器          组合块分支未覆盖全 -> 综合出 latch
R002 位宽截断/不匹配      赋值两侧位宽不符或常量超范围
R003 时序块误用阻塞赋值   导致仿真与综合行为不一致
R004 多驱动              同一信号被多个源驱动
R005 敏感列表不完整      组合块漏列被读信号
R006 跨时钟域未同步      信号跨时钟域被直接采样
R007 不可综合结构        含仿真专用语法
R008 未使用 / 悬空信号   声明后从未被读取或从未被驱动
R009 实例端口位宽不匹配   跨文件比对实例连接与模块端口定义
R010 实例端口未连接      实例化时漏连的输入端口
R011 组合逻辑误用非阻塞   组合块内使用 <=，仿真与综合行为不一致
R012 赋值运算符误用      assign x == y; 把赋值误写成比较
R013 实例化连接方式混用   同一实例内混用按名与按位置连接
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Set, Tuple

from .model import AlwaysBlock, Finding, Module
from .parser import (IDENT_RE, KEYWORDS, eval_const, min_width, parse_literal,
                     read_identifiers)

# 规则签名: (module, ctx) -> List[Finding]
RuleFn = Callable[[Module, Dict], List[Finding]]
RULES: List[Tuple[str, str, RuleFn]] = []


def rule(rid: str, name: str):
    def deco(fn: RuleFn) -> RuleFn:
        RULES.append((rid, name, fn))
        return fn
    return deco


def _snippet(lines: List[str], line: int) -> str:
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:120]
    return ""


def _read_signals(text: str, mod: Module) -> Set[str]:
    """取文本中出现的、属于本模块的真实信号（排除常量与隐式符号）。"""
    out: Set[str] = set()
    for ident in read_identifiers(text):
        sig = mod.signals.get(ident)
        if sig is None:
            continue
        if sig.storage in ("parameter", "localparam", "_implicit"):
            continue
        out.add(ident)
    return out


def _mk(rid: str, name: str, severity: str, line: int, msg: str, hint: str,
        ctx: Dict, mod: Module, conf: float = 1.0,
        tags: Optional[List[str]] = None) -> Finding:
    return Finding(
        rule_id=rid, rule_name=name, severity=severity, line=line,
        message=msg, hint=hint, snippet=_snippet(ctx["lines"], line),
        confidence=conf, module=mod.name, file=mod.file,
        tags=list(tags or []),
    )


def _is_block_local(mod: Module, blk, sig: str) -> bool:
    """判断信号是否**仅**作为该过程块内的临时变量使用。

    若它不被任何其他过程块读取、不出现在连续赋值或实例连接中、
    也不是模块输出端口，那么它的阻塞赋值只是块内顺序计算的手段
    （如 picorv32 的 set_mem_do_rinst、axis 库的 wr_ptr_temp），
    属可接受的编码风格，不构成仿真/综合失配风险。
    """
    s = mod.signal(sig)
    if s is None:
        # 符号表里查不到该信号（例如声明落在 generate / `ifdef 区间内、
        # 被解析器的区间屏蔽掉了）——此时**无法证明**它只是块内临时量，
        # 因此不发放豁免标记（宁可不豁免，也不能误豁免一个真缺陷）。
        return False
    if s.direction in ("output", "inout"):
        return False
    for other in mod.always_blocks:
        if other is blk or sig in other.assigned_signals:
            continue
        if sig in _read_signals(other.source, mod):
            return False
    for a in mod.assigns:
        if sig in _read_signals(a.expr, mod):
            return False
    for inst in mod.instances:
        for conn in inst.connections:
            if sig in _read_signals(conn, mod):
                return False
    return True


# ---------------------------------------------------------------------------
# R001  推断锁存器
# ---------------------------------------------------------------------------

@rule("R001", "推断锁存器")
def r001_inferred_latch(mod: Module, ctx: Dict) -> List[Finding]:
    out: List[Finding] = []
    for blk in mod.always_blocks:
        if not blk.is_comb:
            continue
        if blk.has_full_case:
            continue          # (* full_case *) 已声明分支完全覆盖，不推断 latch
        covered: Set[str] = set(blk.default_assigned)
        if covered and blk.is_full_sens:
            continue                      # 块首默认赋值 + @* -> 确定为安全
        # else if 链若没有终结 else，仍存在未赋值路径
        else_if = len(re.findall(r"(?<![\w$])else\s+if(?![\w$])", blk.source))
        else_all = len(re.findall(r"(?<![\w$])else(?![\w$])", blk.source))
        terminal_else = else_all > else_if
        reasons: List[str] = []
        if blk.has_if and not terminal_else:
            reasons.append("if 链缺少终结 else 分支")
        if blk.has_case and not blk.has_default and not covered:
            reasons.append("case 缺少 default 分支")
        if not reasons:
            continue
        risky = sorted(blk.assigned_signals - covered)
        if not risky:
            continue
        out.append(_mk(
            "R001", "推断锁存器", "warning", blk.line_start,
            "组合逻辑块中 %s，综合器会为 %s 推断出锁存器（latch）" %
            ("、".join(reasons), "、".join(risky)),
            "补齐 else / default 分支，或在块首对全部目标寄存器赋默认值（推荐后者）",
            ctx, mod, conf=0.9,
        ))
    return out


# ---------------------------------------------------------------------------
# R002  位宽截断 / 不匹配
# ---------------------------------------------------------------------------

def _split_top_op(expr: str, ops: str = "+-|&^"):
    """按顶层二元运算符切分表达式，返回 (左, 运算符, 右)；无顶层运算符返回 None。"""
    depth = 0
    for i, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in ops:
            if i == 0 or expr[i - 1] in "+-|&^~(":
                continue                      # 一元运算符，不是二元切分点
            return expr[:i].strip(), ch, expr[i + 1:].strip()
    return None


def _expr_width(expr: str, mod: Module, depth: int = 0):
    """推断表达式位宽（保守简化版）。无法确定时返回 None。

    为什么需要: Verilog 的算术运算符**不会自动扩展位宽** ——
    `a + b` 的结果位宽是 max(width(a), width(b)) 而非 max+1，
    因此 `reg [3:0] sum; assign sum = a + b;`（a、b 更宽时）会静默丢失高位。
    这是经典 RTL 陷阱，原实现因为「表达式一律跳过」而完全漏检。
    """
    if depth > 4:
        return None
    e = expr.strip()
    while e.startswith("(") and e.endswith(")") and len(e) > 1:
        e = e[1:-1].strip()
    val, w = parse_literal(e)
    if w is not None:
        return w
    if IDENT_RE.fullmatch(e):
        s = mod.signal(e)
        if s is None or s.width_unknown:
            return None
        return s.width
    split = _split_top_op(e)
    if split:
        left, _op, right = split
        wl = _expr_width(left, mod, depth + 1)
        wr = _expr_width(right, mod, depth + 1)
        if wl and wr:
            return max(wl, wr)                # 与 Verilog 语义一致
    return None


@rule("R002", "位宽截断")
def r002_width_mismatch(mod: Module, ctx: Dict) -> List[Finding]:
    out: List[Finding] = []
    stmts = []
    for blk in mod.always_blocks:
        stmts.extend(blk.assigns)
    stmts.extend(mod.assigns)

    for a in stmts:
        lhs = mod.signal(a.lhs)
        if lhs is None or lhs.storage in ("parameter", "localparam", "_implicit"):
            continue
        if lhs.width_unknown:
            continue          # 位宽表达式未求值（如参数化位宽），不可据此判定
        expr = a.expr.strip()
        kind = ""
        rhs_w = None
        rhs_val = None
        if IDENT_RE.fullmatch(expr):
            rs = mod.signal(expr)
            if rs is None or rs.storage in ("parameter", "localparam", "_implicit"):
                continue
            if rs.width_unknown:
                continue
            rhs_w, kind = rs.width, "源信号"
        else:
            val, explicit_w = parse_literal(expr)
            if val is None and explicit_w is None:
                # 表达式：尝试位宽推断（保守，推不出就跳过）
                ew = _expr_width(expr, mod)
                if ew is None:
                    continue
                rhs_w, kind = ew, "表达式"
            else:
                rhs_val, kind = val, "常量"
                rhs_w = explicit_w if explicit_w is not None else (
                    min_width(val) if val is not None else None)
        if rhs_w is None:
            continue

        lw = lhs.width
        if a.bit_select and not lhs.is_array:
            # LHS 带位选时（如 cfgreg_do[31]、buffer[7:0]），实际被赋值的宽度是
            # 位选宽度而非信号总宽度。用总宽度比较会在真实代码上产生大量误报
            # （spimemio 与 axis_ram_switch 共计 22 条）。
            #
            # 但数组元素访问（axis_tdata_pipe[0]）语法上与位选完全相同，
            # 其宽度应是元素宽度（即信号宽度）。必须靠声明中的数组维度区分，
            # 否则会把 8 位赋 8 位误判为"8 位截断成 1 位"。
            rng = _bit_range(a.bit_select)
            if rng is None:
                continue          # 位选含变量或 +: / -: 语法，无法静态判定
            lw = rng[1] - rng[0] + 1
        if rhs_w > lw:
            detail = ("常量 %s 需 %d 位" % (expr, rhs_w)) if kind == "常量" else \
                     ("源信号 %s 为 %d 位" % (expr, rhs_w))
            out.append(_mk(
                "R002", "位宽截断", "error", a.line,
                "%s[%d 位] 被赋值为 %s，高位将被静默截断" % (lhs.name, lw, detail),
                "扩展左侧位宽，或显式截断（如 %s[%d:0]）以表明意图" % (a.lhs, lw - 1),
                ctx, mod, conf=0.95,
            ))
        elif rhs_w < lw and kind == "源信号":
            out.append(_mk(
                "R002", "位宽截断", "warning", a.line,
                "%s[%d 位] 由 %d 位的 %s 赋值，将发生零扩展" % (lhs.name, lw, rhs_w, expr),
                "确认零扩展是否符合设计意图，必要时做符号扩展",
                ctx, mod, conf=0.7,
            ))
    return out


# ---------------------------------------------------------------------------
# R003  时序块误用阻塞赋值
# ---------------------------------------------------------------------------

@rule("R003", "阻塞赋值误用")
def r003_blocking_in_seq(mod: Module, ctx: Dict) -> List[Finding]:
    out: List[Finding] = []
    for blk in mod.always_blocks:
        if not blk.is_seq:
            continue
        for a in blk.assigns:
            if a.op != "=":
                continue
            sig = mod.signal(a.lhs)
            if sig is not None and sig.storage in ("parameter", "localparam"):
                continue
            out.append(_mk(
                "R003", "阻塞赋值误用", "warning", a.line,
                "时序块（%s 沿触发）内对 %s 使用阻塞赋值 =，存在仿真与综合失配风险" %
                (blk.clock or "边沿", a.lhs),
                "若该信号仅为本块内顺序计算的中间量，属可接受的个人风格；"
                "若为跨块使用的寄存器，应改用非阻塞赋值 <=",
                ctx, mod, conf=0.75,
                tags=["block_local_temp"] if _is_block_local(mod, blk, a.lhs) else None,
            ))
    return out


# ---------------------------------------------------------------------------
# R004  多驱动
# ---------------------------------------------------------------------------

CONN_RE = re.compile(r"^\.\s*([A-Za-z_]\w*)\s*\(\s*([^)]*)\)\s*$", re.S)


def parse_connection(conn: str) -> Tuple[str, str]:
    """解析一个实例端口连接，返回 (端口名, 信号名)；无法确定时返回空串。"""
    m = CONN_RE.match(conn.strip())
    if not m:
        return "", ""
    nm = re.match(r"^\s*([A-Za-z_]\w*)", m.group(2))
    return m.group(1), (nm.group(1) if nm else "")


def _bit_range(sel: str) -> Optional[Tuple[int, int]]:
    """把位选 '[msb:lsb]' / '[n]' 解析为 (低, 高)；无法解析返回 None。"""
    m = re.match(r"^\[\s*([^:\]]+?)\s*(?::\s*([^\]]+?)\s*)?\]$", (sel or "").strip())
    if not m:
        return None
    hi = eval_const(m.group(1), {})
    lo = eval_const(m.group(2), {}) if m.group(2) else hi
    if hi is None or lo is None:
        return None
    return (min(lo, hi), max(lo, hi))


def _has_real_conflict(srcs: List[Tuple[str, int, str]]) -> bool:
    """判断多驱动是否构成真实冲突。

    带位选的连续赋值可以合法地**分段驱动**同一向量:
        assign cfgreg_do[31]    = config_en;
        assign cfgreg_do[30:23] = 0;
    这是标准写法（spimemio 用一个 32 位向量拼装配置字），并非多驱动。
    只有「整体驱动(无位选) >= 2」或「位选区间相互重叠」才是真冲突。
    """
    whole = 0
    ranges: List[Tuple[int, int]] = []
    for _desc, _line, sel in srcs:
        if not sel:
            whole += 1
            continue
        r = _bit_range(sel)
        if r is None:
            whole += 1                  # 位选不可解析，按整体驱动处理
            continue
        ranges.append(r)
    if whole >= 2:
        return True
    if whole == 1 and ranges:
        return True
    ranges.sort()
    for i in range(1, len(ranges)):
        if ranges[i][0] <= ranges[i - 1][1]:
            return True
    return False


@rule("R004", "多驱动")
def r004_multiple_driver(mod: Module, ctx: Dict) -> List[Finding]:
    index = ctx.get("index")
    drivers: Dict[str, List[Tuple[str, int, str]]] = {}
    for blk in mod.always_blocks:
        for s in sorted(blk.assigned_signals):
            if s in mod.signals:
                drivers.setdefault(s, []).append(
                    ("always 块（第 %d 行）" % blk.line_start, blk.line_start, ""))
    for a in mod.assigns:
        drivers.setdefault(a.lhs, []).append(
            ("连续赋值 assign", a.line, a.bit_select or ""))

    if index is not None:
        for inst in mod.instances:
            for conn in inst.connections:
                port, sig_name = parse_connection(conn)
                # 只把"纯标识符"连接算作驱动源：数组元素 / 位选 / 拼接 / 常量连接
                # （如 .m_axis_tdata(axis_tdata[i])）不能算 —— 否则一个逐元素赋值的
                # 数组会被误判成"整条信号被实例输出驱动"，与其它元素的 assign 撞成
                # 假多驱动（实测在 axis_pipeline_register 上产生 8 条误报）。
                sig_name = conn_signal_exact(conn)
                if not port or not sig_name or sig_name not in mod.signals:
                    continue
                direction = index.port_direction(inst.module_type, port)
                # 只有确认属于输出/inout 端口的连接才构成驱动源。
                # 模块定义未知时（FPGA 原语、工艺库单元、第三方 IP）保守跳过 ——
                # 否则每个输入连接都会制造一条假的多驱动告警
                # （在 42 文件的真实语料上曾因此产生 200+ 条误报）。
                if direction in ("output", "inout"):
                    drivers.setdefault(sig_name, []).append(
                        ("实例 %s 的 .%s 输出" % (inst.instance_name, port),
                         inst.line, ""))

    # 参数化 generate 的互斥分支与条件编译分支都会造成"表观多驱动"：
    # 静态看是多个源驱动同一信号，实际只会编译其中一个。
    # 判定规则: 只有「无条件驱动源 >= 2」或「条件源与无条件源混用」才进入候选。
    conditional = mod.ifdef_lines | mod.generate_lines
    out: List[Finding] = []
    for sig, srcs in drivers.items():
        if len(srcs) < 2:
            continue
        real = [s for s in srcs if s[1] not in conditional]
        cond = [s for s in srcs if s[1] in conditional]
        if len(real) < 2 and not (real and cond):
            continue
        if not _has_real_conflict(srcs):
            continue                    # 位选分段驱动，合法
        desc = "、".join(s[0] for s in srcs)
        out.append(_mk(
            "R004", "多驱动", "error", srcs[0][1],
            "信号 %s 被 %d 个源同时驱动（%s），综合会报 multiple driver" %
            (sig, len(srcs), desc),
            "合并驱动源：同一信号只允许一个 always 块或一条 assign 驱动",
            ctx, mod, conf=1.0 if len(real) >= 2 else 0.85,
        ))
    return out


# ---------------------------------------------------------------------------
# R005  敏感列表不完整
# ---------------------------------------------------------------------------

@rule("R005", "敏感列表不完整")
def r005_incomplete_sensitivity(mod: Module, ctx: Dict) -> List[Finding]:
    out: List[Finding] = []
    for blk in mod.always_blocks:
        if blk.is_seq or blk.is_full_sens:
            continue
        read = _read_signals(blk.source, mod) - blk.assigned_signals - set(blk.sens)
        if not read:
            continue
        out.append(_mk(
            "R005", "敏感列表不完整", "warning", blk.line_start,
            "组合块读取了 %s，但敏感列表 %s 中未包含，仿真与综合行为将不一致" %
            ("、".join(sorted(read)), blk.sens),
            "改写为 always @* 或 always_comb，由工具自动推导完整敏感列表",
            ctx, mod, conf=0.9,
        ))
    return out


# ---------------------------------------------------------------------------
# R006  跨时钟域未同步
# ---------------------------------------------------------------------------

# 命名约定表明该信号已是同步器输出或格雷码指针，属于已正确处理的跨域信号。
# 注意: 必须要求下划线/数字边界, 否则 "cdc_flag" 这类仅名字含 cdc 的
# 普通跨域源会被误豁免（曾在受控集上造成 R006 漏报）。
SYNC_HINT_RE = re.compile(r"(_sync\d*|_meta|_gray|gray_|_synchroniz)", re.I)


@rule("R006", "跨时钟域未同步")
def r006_clock_domain_crossing(mod: Module, ctx: Dict) -> List[Finding]:
    written: Dict[str, Set[str]] = {}
    read: Dict[str, Set[str]] = {}
    for blk in mod.always_blocks:
        if not blk.is_seq or not blk.clock:
            continue
        written.setdefault(blk.clock, set()).update(
            s for s in blk.assigned_signals if s in mod.signals)
        read.setdefault(blk.clock, set()).update(
            _read_signals(blk.source, mod) - blk.assigned_signals)

    out: List[Finding] = []
    for clk_w, sigs in written.items():
        for clk_r, reads in read.items():
            if clk_w == clk_r:
                continue
            for sig in sorted(sigs & reads):
                sig_obj = mod.signal(sig)
                if sig_obj is None or sig_obj.storage in ("parameter", "localparam"):
                    continue
                if SYNC_HINT_RE.search(sig):
                    # 该信号本身即同步器输出（如 wr_ptr_gray_sync1_reg）或
                    # 格雷码指针，跨域采样是数设计的正确做法，不报。
                    continue
                out.append(_mk(
                    "R006", "跨时钟域未同步", "warning", sig_obj.line or 1,
                    "%s 在 %s 域写入，却被 %s 域直接采样，存在亚稳态传播风险" %
                    (sig, clk_w, clk_r),
                    "插入两级同步器（打两拍），或改用异步 FIFO / 握手同步",
                    ctx, mod, conf=0.8,
                ))
    return out


# ---------------------------------------------------------------------------
# R007  不可综合结构
# ---------------------------------------------------------------------------

# (正则, 描述, 严重度, 处置建议)
#
# 严重度分级依据真实工程实践:
#   #N 延时 / while / fork 等 -> 确实无法映射为硬件，error
#   initial 块               -> FPGA 流程常用于寄存器上电初值（合法），降为 warning
#   $display 等系统任务       -> 纯调试代码，综合工具直接忽略，降为 info
# 未分级会导致成熟设计（如 picorv32 的调试打印）产生上百条噪音告警。
UNSYNTH_PATTERNS: List[Tuple[str, str, str, str]] = [
    (r"(?<![\w$])initial(?![\w$])", "initial 过程块", "warning",
     "FPGA 流程可用于寄存器上电初值；ASIC 流程应改用复位逻辑"),
    (r"#\s*\d+", "延时控制 #N", "error", "延时不可综合，仅适用于仿真"),
    (r"\$(display|monitor|finish|stop|time|random|dumpfile|dumpvars|fopen|"
     r"fwrite|fclose|fflush|fatal|error|warning|readmem)\b",
     "仿真系统任务", "info", "纯调试代码，综合工具会忽略；建议用 `ifdef 隔离"),
    (r"(?<![\w$])while(?![\w$])", "while 循环", "error", "不可综合，改用 for 固定次数循环"),
    (r"(?<![\w$])forever(?![\w$])", "forever 循环", "error", "不可综合，仅用于 testbench"),
    (r"(?<![\w$])(fork|join)(?![\w$])", "fork/join 并行块", "error", "不可综合"),
    (r"(?<![\w$])wait(?![\w$])", "wait 语句", "error", "不可综合"),
    (r"(?<![\w$])(force|release|deassign)(?![\w$])", "强制/释放语句", "error", "不可综合"),
    (r"(?<![\w$])(real|realtime)(?![\w$])", "实数类型", "error", "不可综合"),
    (r"(?<![\w$])repeat(?![\w$])", "repeat 语句", "warning", "部分工具支持，建议改用 for"),
]


@rule("R007", "不可综合结构")
def r007_non_synthesizable(mod: Module, ctx: Dict) -> List[Finding]:
    out: List[Finding] = []
    body = mod.body
    for pat, label, severity, note in UNSYNTH_PATTERNS:
        hits = list(re.finditer(pat, body))
        extra = ""
        if label == "仿真系统任务" and len(hits) > 1:
            # 成熟设计里调试打印与断言常达数十上百处（picorv32 / verilog-axis
            # 均如此），逐行上报会淹没真正的问题。按模块聚合为一条，
            # 并在消息中给出总处数，兼顾降噪与信息完整。
            extra = "（本模块共 %d 处）" % len(hits)
            hits = hits[:1]
        for m in hits:
            # 基准必须是模块体的起始行，而不是 module 关键字所在行
            line = mod.body_line + body.count("\n", 0, m.start())
            out.append(_mk(
                "R007", "不可综合结构", severity, line,
                "综合目标代码中出现 %s%s" % (label, extra), note,
                ctx, mod, conf=0.95 if severity == "error" else 0.85,
            ))
    # 同一行只保留一条
    seen: Set[int] = set()
    unique: List[Finding] = []
    for f in out:
        if f.line in seen:
            continue
        seen.add(f.line)
        unique.append(f)
    return unique


# ---------------------------------------------------------------------------
# R008  未使用 / 悬空信号
# ---------------------------------------------------------------------------

DECL_MASK_RE = re.compile(
    r"(?<![\w$])(input|output|inout|wire|reg|logic|integer|genvar|bit|parameter|"
    r"localparam|real|time|realtime|int|byte|shortint|longint|tri|wand|wor)"
    r"\b[^;]*;", re.S)


def _mask_decls(text: str) -> str:
    """抹掉声明语句中的信号名，但保留其初始化表达式。

    必要性: 若只抹赋值左侧，声明语句 `reg [1:0] dbg_state;` 中的 dbg_state
    会被误计入"被读取"，导致真正的只写不读信号被漏报（FN）。
    而 `wire wr_ok = wr_en && !full;` 的右式又必须保留，否则 wr_en/full 会
    被反向误报为未使用（FP）。
    """
    def repl(m: "re.Match") -> str:
        stmt = m.group(0)
        if "=" in stmt:
            return " " + stmt.split("=", 1)[1]
        return " "
    return DECL_MASK_RE.sub(repl, text)


@rule("R008", "未使用信号")
def r008_unused_signal(mod: Module, ctx: Dict) -> List[Finding]:
    # "被读取" 的精确判定 = 抹掉声明 + 抹掉赋值左侧, 再取标识符。两次掩码缺一不可:
    #   只抹赋值左侧 -> 声明名污染 used   (漏报, 见 _mask_decls 注释)
    #   只抹声明     -> 自引用被判只写不读 (误报)
    lhs_mask = re.compile(
        r"(?<![\w$])([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(<=|=)(?![=<>])")
    used: Set[str] = set()
    used |= _read_signals(lhs_mask.sub(" ", _mask_decls(mod.body)), mod)
    for blk in mod.always_blocks:
        used |= {s for s in blk.sens if s in mod.signals}
        if blk.clock:
            used.add(blk.clock)
    for inst in mod.instances:
        for conn in inst.connections:
            used |= _read_signals(conn, mod)

    out: List[Finding] = []
    for name, sig in sorted(mod.signals.items()):
        if sig.direction in ("output", "inout") or sig.storage in (
                "parameter", "localparam", "_implicit"):
            continue
        if sig.in_ifdef:
            continue   # 条件编译块内的声明，其使用与否取决于宏定义，静态不可知
        if name in used:
            continue
        out.append(_mk(
            "R008", "未使用信号", "info", sig.line or 1,
            "信号 %s 声明后从未被读取，可能是冗余逻辑或连线遗漏" % name,
            "确认是否为遗忘连接；若确实无用请删除以保持代码整洁",
            ctx, mod, conf=0.7,
        ))
    return out


# ---------------------------------------------------------------------------
# R009 / R010  实例化相关（需要跨文件模块索引）
# ---------------------------------------------------------------------------

def conn_signal_exact(conn: str) -> str:
    """仅当连接右侧是**纯标识符**时返回信号名，否则返回空串。

    位选（addr[23:0]）、拼接（{a,b}）、常量（1'b1）、表达式都必须排除，
    否则会把 `spimemio .addr(mem_addr[23:0])` 误判为"24 位端口接了 32 位信号"。
    """
    m = CONN_RE.match(conn.strip())
    if not m:
        return ""
    expr = m.group(2).strip()
    return expr if IDENT_RE.fullmatch(expr) else ""


@rule("R009", "实例端口位宽不匹配")
def r009_instance_port_width(mod: Module, ctx: Dict) -> List[Finding]:
    index = ctx.get("index")
    if index is None:
        return []
    out: List[Finding] = []
    for inst in mod.instances:
        # 带参数覆写的实例（#(.W(32))）会改变端口位宽，索引里只有默认值，无法判断
        if inst.overrides:
            continue
        moddef = index.lookup(inst.module_type)
        if moddef is None or not moddef.ports:
            continue
        for conn in inst.connections:
            port, _any = parse_connection(conn)
            sig_name = conn_signal_exact(conn)
            if not port or not sig_name:
                continue
            psig = moddef.port(port)
            lsig = mod.signal(sig_name)
            if psig is None or lsig is None:
                continue
            if (psig.width_unknown or lsig.width_unknown
                    or psig.width_param_dependent):
                continue
            if psig.width == lsig.width:
                continue
            wider = max(psig.width, lsig.width)
            narrower = max(min(psig.width, lsig.width), 1)
            sev = "error" if wider / narrower >= 2 else "warning"
            if psig.direction == "input":
                desc = ("实例 %s 的输入端口 .%s 为 %d 位，实际连接的 %s 为 %d 位" %
                        (inst.instance_name, port, psig.width, sig_name, lsig.width))
                hint = "调整连接信号位宽或加显式位选，避免隐式截断/零扩展"
            else:
                desc = ("实例 %s 的输出端口 .%s 为 %d 位，但连接的 %s 只有 %d 位" %
                        (inst.instance_name, port, psig.width, sig_name, lsig.width))
                hint = "加宽接收信号，否则输出高位被直接丢弃"
            out.append(_mk("R009", "实例端口位宽不匹配", sev, inst.line,
                           desc, hint, ctx, mod, conf=0.8))
    return out


@rule("R010", "实例端口未连接")
def r010_unconnected_port(mod: Module, ctx: Dict) -> List[Finding]:
    index = ctx.get("index")
    if index is None:
        return []
    out: List[Finding] = []
    for inst in mod.instances:
        moddef = index.lookup(inst.module_type)
        if moddef is None or not moddef.ports:
            continue
        named: Set[str] = set()
        has_positional = False
        for conn in inst.connections:
            port, _sig = parse_connection(conn)
            if port:
                named.add(port)
            else:
                has_positional = True
        if has_positional or not named:
            continue                      # 位置连接无法判断端口归属
        missing_in = [p for p in moddef.ports
                      if p not in named and moddef.ports[p].direction == "input"]
        if missing_in:
            shown = "、".join(sorted(missing_in)[:6])
            more = "等" if len(missing_in) > 6 else ""
            out.append(_mk(
                "R010", "实例端口未连接", "warning", inst.line,
                "实例 %s（%s）有 %d 个输入端口未连接：%s%s" %
                (inst.instance_name, inst.module_type, len(missing_in), shown, more),
                "确认是否为遗漏连接；若端口设计上确实可悬空，请显式写 .port() 表明意图",
                ctx, mod, conf=0.7))
    return out


# ---------------------------------------------------------------------------
# R011 / R012 / R013 —— 调研公开 benchmark 后补充的规则
#
# 来源: LintLLM benchmark（GLSVLSI 2025）的 11 类注入缺陷中，本项目原先
# 未覆盖的三类。选择标准是「误报风险低 + 后果明确」，以维持工具"宁漏勿误"
# 的一贯取向。
# ---------------------------------------------------------------------------

@rule("R011", "组合逻辑误用非阻塞赋值")
def r011_nonblocking_in_comb(mod: Module, ctx: Dict) -> List[Finding]:
    """组合块内使用 `<=`：仿真中要等到时间片末才生效，与综合结果不一致。

    对应 LintLLM benchmark 的 "Combinational or Sequential" 类，
    也是 Verible 内置规则 always-comb-blocking 覆盖的场景。
    """
    out: List[Finding] = []
    for blk in mod.always_blocks:
        if not blk.is_comb:
            continue
        sens = "、".join(blk.sens) if blk.sens else "*"
        for a in blk.assigns:
            if a.op != "<=" or not mod.signal(a.lhs):
                continue
            out.append(_mk(
                "R011", "组合逻辑误用非阻塞赋值", "warning", a.line,
                "组合逻辑块（敏感列表 %s）内对 %s 使用非阻塞赋值 <=" % (sens, a.lhs),
                "组合逻辑应使用阻塞赋值 =；若要建模时序逻辑，请改用带时钟边沿的敏感列表",
                ctx, mod, conf=0.85))
    return out


ASSIGN_CMP_RE = re.compile(
    r"(?<![\w$])assign(?![\w$])\s+([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(==|!=)")


@rule("R012", "赋值运算符误用")
def r012_assign_operator_misuse(mod: Module, ctx: Dict) -> List[Finding]:
    """`assign x == expr;` 中把赋值 `=` 误写成比较 `==`。

    后果明确且严重：该语句不驱动任何信号，而是变成一条无副作用的比较，
    综合后目标信号悬空。属高频真实缺陷。
    """
    out: List[Finding] = []
    for m in ASSIGN_CMP_RE.finditer(mod.body):
        if m.group(1) in KEYWORDS:
            continue
        line = mod.body_line + mod.body.count("\n", 0, m.start())
        out.append(_mk(
            "R012", "赋值运算符误用", "error", line,
            "assign 语句中使用了比较运算符 %s，疑似将赋值 = 误写为比较" % m.group(3),
            "改为 = —— 当前写法不会驱动 %s" % m.group(1),
            ctx, mod, conf=0.9))
    return out


@rule("R013", "实例化连接方式混用")
def r013_mixed_instantiation(mod: Module, ctx: Dict) -> List[Finding]:
    """同一实例内混用按名连接 `.port(sig)` 与按位置连接 `sig`。

    Verilog LRM 禁止在同一实例中混用两种方式；即便某些工具容忍，
    按位置连接也极易因端口顺序变化而静默错连。
    """
    out: List[Finding] = []
    for inst in mod.instances:
        named = any(c.strip().startswith(".") for c in inst.connections)
        positional = any(c.strip() and not c.strip().startswith(".")
                         for c in inst.connections)
        if named and positional:
            out.append(_mk(
                "R013", "实例化连接方式混用", "error", inst.line,
                "实例 %s（%s）同时使用了按名连接与按位置连接" %
                (inst.instance_name, inst.module_type),
                "统一改为按名连接 .port(sig)，可避免端口顺序变化导致的隐式错连",
                ctx, mod, conf=0.95))
    return out


def all_rules() -> List[Tuple[str, str, RuleFn]]:
    return list(RULES)
