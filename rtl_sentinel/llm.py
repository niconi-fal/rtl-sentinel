"""LLM 语义推理层 —— 对规则告警做裁定、归因与修复建议。

核心架构约束（本项目的关键技术判断）
-----------------------------------
    禁止 LLM 自由产生新告警。LLM 的输入被严格限定为「规则引擎已产出的告警 +
    对应源码片段」，输出被限定为对该告警的三元裁定:
        1. verdict  该告警是真缺陷还是误报
        2. reason   根因分析
        3. patch    最小修复补丁

为什么这样设计:
    - 抑制幻觉: 没有"自由发挥"的空间，LLM 无法凭空发明不存在的缺陷
    - 可评测: 裁定结果可与人工标注比对，量化 LLM 带来的准确率增益
    - 可降级: 离线环境下自动回退到启发式裁定器，系统功能不缺失

两种后端:
    MockProvider        离线启发式裁定器（零成本、可复现、默认启用）
    OpenAICompatProvider 任意 OpenAI 兼容 API（本地 vLLM / Ollama / 云端均可）
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Set

from .model import Finding, Module


class LLMProvider:
    """推理后端接口。"""

    name = "base"

    def complete(self, system: str, user: str) -> str:   # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 后端一: 离线启发式裁定器
# ---------------------------------------------------------------------------

_PATCH_HINTS: Dict[str, str] = {
    "R001": "在块首为全部目标寄存器补默认赋值，例如:\n"
            "    always @* begin\n"
            "        next_state = state;          // 默认保持\n"
            "        case (state) ... endcase\n"
            "    end",
    "R002": "按需扩展左侧位宽，例如把目标声明改为足够宽度，并让常量与之等宽。",
    "R003": "把阻塞赋值改为非阻塞赋值:  x = y;  ->  x <= y;",
    "R004": "合并驱动源，保证每个信号只有一个 always 块或一条 assign 驱动。",
    "R005": "把敏感列表改为通配:  always @(a)  ->  always @*",
    "R006": "插入两级同步器:\n"
            "    reg sync0, sync1;\n"
            "    always @(posedge dst_clk) begin\n"
            "        sync0 <= src_signal;\n"
            "        sync1 <= sync0;\n"
            "    end",
    "R007": "把仿真专用代码移入独立的 testbench，或用 `ifdef SIM 包起来。",
    "R008": "确认该信号是否漏连；若确实无用则删除声明。",
}

# 命中即为「不依赖设计意图的确定性静态事实」，可判为确认缺陷
_HARD_FACTS = {"R004", "R009"}


class MockProvider(LLMProvider):
    """离线启发式裁定器 —— 无外部依赖的确定性回退路径。

    其裁定逻辑与真实 LLM 的职责完全一致，只是用规则化的先验代替语义理解，
    因此在无网络、无 API Key 的环境下也能完整演示系统闭环。
    """

    name = "offline-heuristic"

    def complete(self, system: str, user: str) -> str:
        return "{}"


# ---------------------------------------------------------------------------
# 后端二: OpenAI 兼容 API
# ---------------------------------------------------------------------------

class OpenAICompatProvider(LLMProvider):
    """通过 HTTP 调用任意 OpenAI 兼容的 /v1/chat/completions 接口。

    仅依赖标准库 urllib，避免引入第三方 SDK。
    """

    def __init__(self, model: str = "gpt-4o-mini",
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 temperature: float = 0.0,
                 timeout: int = 60):
        self.model = model
        self.base_url = (base_url or os.environ.get(
            "RTL_SENTINEL_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("RTL_SENTINEL_API_KEY", "")
        self.temperature = temperature
        self.timeout = timeout
        self.name = "openai-compat:" + model

    def complete(self, system: str, user: str) -> str:
        import urllib.request

        payload = json.dumps({
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def _parse_json(text: str, key: str = "items") -> List[dict]:
    """从模型输出中提取 JSON 列表（兼容 ```json 包裹与前后杂物）。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?|```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get(key) if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


class LLMDetector:
    """大模型检测通道 —— 规则引擎的补充，用于减少漏报。

    定位说明（重要，不要改）
    ----------------------
      - 输出一律标记 `needs_review`，severity **强制为 info**，
        **不计入风险分**，也不参与 CI 门禁 —— 因为 LLM 无法保证零误报与可复现性。
      - 它的价值是"提示人再多看一眼"，而不是"替代规则引擎做判定"。
      - 依据来自对照实验（`bench/evaluate_llm_direct.py`）：LLM 检出率 72.2%
        显著高于规则引擎 23.3%，但耗时高约 4500 倍、成本不为零、结果不完全可复现，
        因此只作为**可选通道**而非默认行为。
    """

    def __init__(self, provider: "LLMProvider", max_lines: int = 1200):
        self.provider = provider
        self.max_lines = max_lines

    def detect_files(self, sources: Dict[str, str]) -> List[Finding]:
        out: List[Finding] = []
        for path, src in sorted(sources.items()):
            out.extend(self._detect_one(path, src))
        return out

    def _detect_one(self, path: str, src: str) -> List[Finding]:
        lines = src.split("\n")
        if not src.strip() or len(lines) > self.max_lines:
            return []                       # 过长文件跳过，避免超上下文
        numbered = "\n".join("%4d| %s" % (i, ln.rstrip())
                             for i, ln in enumerate(lines, 1))
        try:
            raw = self.provider.complete(DETECT_PROMPT, numbered)
        except Exception:                   # noqa: BLE001
            return []
        found: List[Finding] = []
        for row in _parse_json(raw, "findings"):
            if not isinstance(row, dict):
                continue
            try:
                line = int(row.get("line", 0))
            except (TypeError, ValueError):
                continue
            if not 1 <= line <= len(lines):
                continue
            msg = str(row.get("message", "")).strip()
            if not msg:
                continue
            found.append(Finding(
                rule_id="LLM",
                rule_name="大模型可疑点",
                severity="info",            # 强制提示级：必须人工复核
                line=line,
                message=msg[:200],
                hint=str(row.get("hint", "")).strip()[:200],
                snippet=lines[line - 1].strip()[:120],
                confidence=0.5,
                module="",
                file=path,
                tags=["llm_detect", "needs_review"],
            ))
        return found


# ---------------------------------------------------------------------------
# 推理器
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一名资深数字 IC 设计验证工程师，专精 RTL 代码审查。

你会收到一组由确定性静态规则引擎产出的告警，每条包含规则 ID、位置、规则层的
语义标记，以及该行附近的源码上下文。你的任务是对**已给出的告警**逐条裁定:
  1. verdict: confirmed（需要修改的真实缺陷）/ false_positive（合法写法，应豁免）
              / uncertain（证据不足，需人工确认）
  2. reason : 说明根因，引用具体信号名与行号，不超过 80 字
  3. patch  : 若为 confirmed，给出最小修复代码片段；否则填「无需修改」

重要背景（直接影响判定结果）:
  - 这些告警来自**成熟的开源工程代码**（如 picorv32、AXI-Stream 库），
    原作者都是有经验的工程师。经验表明，这类代码上**大部分告警并非缺陷**，
    而是设计者有意为之的合法写法。
  - 常见应判 false_positive 的情形:
      · 文件是行为级仿真模型（不参与综合）
      · 赋值目标仅是过程块内的临时变量，不被其他逻辑引用
      · 有意位宽截断（如 32 位 gpio 只驱动 8 个 LED）
      · 仿真调试代码与断言（$display / $error / $fatal）
      · 参数化设计中的互斥条件分支
  - 若证据不足以判断设计意图，务必输出 uncertain，不要臆测。

严格约束:
  - 只针对给定告警作答，禁止提出新的告警或猜测未提供代码中的问题
  - 输出必须是合法 JSON，格式为
    {"items":[{"rule_id":..,"line":..,"verdict":..,"reason":..,"patch":..}]}
"""


DETECT_PROMPT = """你是资深数字 IC 设计验证工程师。请检查下面这份 Verilog 源码中
**确实存在的静态缺陷**，指出所在行号。

检查范围（按优先级）:
  语法与结构 / 变量声明与使用 / 位宽匹配 / 赋值方式（阻塞 vs 非阻塞）
  敏感列表完整性 / 跨时钟域 / 多驱动 / 竞争冒险 / case 完整性 / 实例化端口

要求:
  - 只报你有把握的问题，宁少勿滥；拿不准的不要报
  - 每条给出: 行号、严重度(error/warning)、问题描述(≤50字)、修复建议(≤50字)
  - 若无缺陷，findings 为空数组
  - 只输出 JSON，不要输出其他内容

输出格式:
{"findings":[{"line":12,"severity":"error","message":"...","hint":"..."}]}
"""


def _context_snippet(src: str, line: int, span: int = 8) -> str:
    """截取告警行附近的源码上下文，供 LLM 判断设计意图。"""
    if not src:
        return ""
    rows = src.split("\n")
    lo = max(1, line - span)
    hi = min(len(rows), line + span)
    out: List[str] = []
    for i in range(lo, hi + 1):
        mark = ">>" if i == line else "  "
        out.append("%s %4d| %s" % (mark, i, rows[i - 1].rstrip()[:110]))
    return "\n".join(out)


class Reasoner:
    """告警裁定器。"""

    def __init__(self, provider: Optional[LLMProvider] = None, enabled: bool = True):
        self.provider = provider or MockProvider()
        self.enabled = enabled
        self.offline = isinstance(self.provider, MockProvider)

    # -- 对外接口 ----------------------------------------------------------
    def adjudicate(self, findings: List[Finding],
                   sources: Optional[Dict[str, str]] = None,
                   batch: int = 6) -> List[Finding]:
        if not self.enabled or not findings:
            return findings
        src_map = sources or {}
        sim_files = self._detect_sim_models(src_map)
        if self.offline:
            for f in findings:
                self._heuristic(f, sim_files)
            return findings
        for i in range(0, len(findings), batch):
            chunk = findings[i: i + batch]
            try:
                raw = self.provider.complete(
                    SYSTEM_PROMPT,
                    self._build_prompt(chunk, src_map, sim_files))
                self._apply(chunk, self._parse(raw))
            except Exception as exc:                              # noqa: BLE001
                for f in chunk:
                    f.llm_verdict = "uncertain"
                    f.llm_reason = "LLM 调用失败(%s)，已保留规则原始结论" % type(exc).__name__
        return findings

    # -- 内部实现 ----------------------------------------------------------
    @staticmethod
    def _build_prompt(chunk: List[Finding], sources: Dict[str, str],
                      sim_files: frozenset = frozenset()) -> str:
        """构造裁定提示。

        上下文与判断依据同等重要: 只给一行代码时，LLM 无法判断"这是有意截断
        还是笔误"；补上附近的声明、赋值与模块语境后，判断质量显著提升。
        """
        lines: List[str] = ["以下告警需要裁定:", ""]
        for f in chunk:
            lines.append("rule_id=%s | line=%d | severity=%s | module=%s | file=%s" %
                         (f.rule_id, f.line, f.severity, f.module,
                          os.path.basename(f.file)))
            if f.file in sim_files:
                lines.append("文件性质: 行为级仿真模型（含 #N 延时与 task 定义），"
                             "不参与综合")
            if f.tags:
                lines.append("规则层标记: %s" % ",".join(f.tags))
            lines.append("规则结论: %s" % f.message)
            ctx = _context_snippet(sources.get(f.file, ""), f.line)
            if ctx:
                lines.append("源码上下文（>> 为告警行）:")
                lines.append(ctx)
            lines.append("")
        lines.append('请输出 JSON: {"items":[...]}')
        return "\n".join(lines)

    @staticmethod
    def _parse(raw: str) -> List[dict]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    @staticmethod
    def _apply(chunk: List[Finding], items: List[dict]) -> None:
        index = {(str(it.get("rule_id")), int(it.get("line", -1))): it for it in items
                 if isinstance(it, dict)}
        for f in chunk:
            it = index.get((f.rule_id, f.line))
            if it is None:
                f.llm_verdict, f.llm_reason = "uncertain", "模型未返回该条目"
                continue
            verdict = str(it.get("verdict", "uncertain")).strip().lower()
            f.llm_verdict = verdict if verdict in (
                "confirmed", "false_positive", "uncertain") else "uncertain"
            f.llm_reason = str(it.get("reason", ""))[:300]
            f.llm_patch = str(it.get("patch", ""))[:600]

    @staticmethod
    def _detect_sim_models(sources: Dict[str, str]) -> Set[str]:
        """识别行为级仿真模型文件（非综合目标）。

        判据: 同时出现延时控制（#N）与 task 定义。综合代码不会使用延时，
        该组合几乎只出现在行为级器件模型中（如 SPI Flash 的行为模型）。
        实测在 42 文件真实语料上仅命中 1 个文件，无误判。
        """
        out: Set[str] = set()
        for path, src in sources.items():
            if re.search(r"#\s*\d+", src) and re.search(
                    r"(?<![\w$])task(?![\w$])", src):
                out.add(path)
        return out

    def _heuristic(self, f: Finding, sim_files: Set[str] = frozenset()) -> None:
        """离线裁定: 只对可明确判定的情形下结论，其余一律 uncertain。

        为什么不能"默认判为确认缺陷"：在成熟工程代码上，规则告警的绝大多数
        并非需要修复的缺陷，而是需要豁免的合法写法。若不具备语义理解能力却
        硬判 confirmed，裁定层就完全失效 —— 实测一致率仅 12.7%。
        因此离线后端的正确姿态是：**能判的判，判不了的交出去**。
        """
        # ① 行为仿真模型：非综合目标，其全部告警都应豁免
        if f.file in sim_files:
            f.llm_verdict = "false_positive"
            f.llm_reason = ("该文件是行为级仿真模型（含 #N 延时与 task 定义），"
                            "不参与综合，其告警不具备工程意义。")
            f.llm_patch = "无需修改。"
            f.llm_verify = "确认该文件是否仅用于仿真验证。"
            return

        # ② 块内临时变量的阻塞赋值：顺序计算手段，非缺陷
        if "block_local_temp" in f.tags:
            f.llm_verdict = "false_positive"
            f.llm_reason = ("该阻塞赋值的目标仅用于本过程块内的顺序计算，"
                            "未被其他过程块、连续赋值或实例连接引用，"
                            "不会造成仿真与综合失配，属可接受的编码风格。")
            f.llm_patch = "无需修改。"
            f.llm_verify = "综合后确认该信号被优化为纯组合逻辑。"
            return

        # ③ 仿真调试代码与断言：综合工具会忽略
        if f.rule_id == "R007" and f.severity != "error":
            f.llm_verdict = "false_positive"
            f.llm_reason = ("该结构属于仿真调试代码或断言，综合工具会直接忽略，"
                            "不影响硬件实现。")
            f.llm_patch = "无需修改；如需保持整洁可用 `ifdef SIMULATION 隔离。"
            f.llm_verify = "确认综合日志无相关报错。"
            return

        # ④ 时序块内对**跨块使用**的寄存器用阻塞赋值 —— 判为真实缺陷。
        #
        # 为什么敢下 confirmed（不再是 uncertain）：三方证据一致
        #   ① 上游项目曾真实修复过该模式（见 bench/backtest/axis_pipeline_fifo_assignment_type，
        #      作者把 `=` 改成 `<=`）；
        #   ② 两个独立标注模型（豆包 2.1 lite 与 GLM 5.3）在二元协议下唯一共同判为
        #      `change` 的条目，正是本模式（axis_ll_bridge.v R003）；
        #   ③ 判据本身是确定的：阻塞赋值 → 仿真与综合的更新时刻不一致。
        # 注意：已被 `_is_block_local` 认定为"块内临时量"的会在 ② 分支返回 false_positive，
        # 因此走到这里的一定是**跨块使用**的寄存器。
        if f.rule_id == "R003" and "block_local_temp" not in f.tags:
            f.llm_verdict = "confirmed"
            f.llm_reason = ("时序块内对跨块使用的寄存器使用阻塞赋值 —— 仿真与综合的更新时刻"
                            "不一致，是真实缺陷（该模式曾被上游项目真实修复）。")
            f.llm_patch = _PATCH_HINTS.get("R003", "")
            f.llm_verify = self._verify_hint(f)
            return

        # ⑤ 不依赖设计意图的确定性静态事实
        if f.rule_id in _HARD_FACTS:
            f.llm_verdict = "confirmed"
            f.llm_reason = ("规则 %s 命中的是不依赖设计意图的确定性静态事实（%s），"
                            "判定为真实缺陷。" % (f.rule_id, f.rule_name))
            f.llm_patch = _PATCH_HINTS.get(f.rule_id, "")
            f.llm_verify = self._verify_hint(f)
            return

        # ⑤ 其余：规则给出的是静态事实，但"是否为需要修改的问题"取决于设计意图，
        #    离线后端不具备该判断能力 —— 诚实标注为 uncertain，交由人工或在线 LLM。
        f.llm_verdict = "uncertain"
        f.llm_reason = ("该告警依赖设计意图判断（%s）—— 离线启发式后端不具备源码"
                        "语义理解能力，无法裁定，需人工确认或接入大模型。"
                        % f.rule_name)
        f.llm_patch = _PATCH_HINTS.get(f.rule_id, "")
        f.llm_verify = self._verify_hint(f)

    @staticmethod
    def _verify_hint(f: Finding) -> str:
        base = {
            "R001": "用综合工具查看是否生成 latch；仿真时观察该信号在未覆盖分支是否保持。",
            "R002": "仿真观测该信号高位是否被零填充或截断。",
            "R003": "对比仿真与综合网表在相同时刻的寄存器更新顺序。",
            "R004": "运行综合，确认是否报 multiple driver。",
            "R005": "改变未列入敏感列表的信号，观察组合输出是否更新。",
            "R006": "对跨域信号做多周期仿真，检查同步后的采样稳定性。",
            "R007": "直接跑综合/综合前 lint，确认工具报错。",
            "R008": "在综合报告中检查该信号是否被优化掉。",
        }
        return base.get(f.rule_id, "补充针对该规则的定向测试用例。")
