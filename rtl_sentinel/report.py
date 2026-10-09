"""报告生成层：Text / Markdown / HTML / JSON 四种输出。

HTML 版本面向演示与截图（保研材料、答辩 PPT），Text 版本面向 CI 流水线。
"""
from __future__ import annotations

import datetime
import html
import json
import os
from typing import Dict, List

from .engine import ReviewResult
from .model import SEVERITY_LABEL, VERDICT_LABEL, Finding

SEV_CLASS = {"error": "sev-error", "warning": "sev-warning", "info": "sev-info"}


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 控制台 / 文本
# ---------------------------------------------------------------------------

def to_text(res: ReviewResult, color: bool = True) -> str:
    codes = {"error": "\033[31m", "warning": "\033[33m", "info": "\033[36m"}
    reset = "\033[0m"
    lines: List[str] = []
    sev = res.by_severity()
    lines.append("RTL-Sentinel 审查结果")
    lines.append("-" * 68)
    lines.append("文件数 %d | 模块数 %d | 错误 %d | 警告 %d | 提示 %d | 健康分 %.1f" %
                 (len(res.files), len(res.modules), sev["error"], sev["warning"],
                  sev["info"], res.health))
    lines.append("-" * 68)
    if not res.findings:
        lines.append("未发现任何问题。")
    for f in res.sorted_findings():
        tag = "[%s]" % f.rule_id
        head = "%s %-9s L%-4d %s" % (tag, SEVERITY_LABEL.get(f.severity, f.severity),
                                     f.line, os.path.basename(f.file))
        if color:
            head = "%s%s%s" % (codes.get(f.severity, ""), head, reset)
        lines.append(head)
        lines.append("    %s" % f.message)
        if f.snippet:
            lines.append("    代码 | %s" % f.snippet)
        if f.llm_verdict:
            lines.append("    裁定 | %s — %s" %
                         (VERDICT_LABEL.get(f.llm_verdict, f.llm_verdict), f.llm_reason))
        if f.llm_patch:
            lines.append("    建议 | %s" % f.llm_patch.replace("\n", "\n           "))
    if res.rule_errors:
        lines.append("-" * 68)
        lines.append("规则执行异常: %s" % res.rule_errors)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def to_markdown(res: ReviewResult) -> str:
    sev = res.by_severity()
    out: List[str] = []
    out.append("# RTL-Sentinel 代码审查报告")
    out.append("")
    out.append("生成时间: %s" % _ts())
    out.append("")
    out.append("## 总览")
    out.append("")
    out.append("| 指标 | 数值 |")
    out.append("| --- | --- |")
    out.append("| 审查文件 | %d |" % len(res.files))
    out.append("| 解析模块 | %d |" % len(res.modules))
    out.append("| 错误 | %d |" % sev["error"])
    out.append("| 警告 | %d |" % sev["warning"])
    out.append("| 提示 | %d |" % sev["info"])
    out.append("| 健康分 | %.1f / 100 |" % res.health)
    out.append("")
    if not res.findings:
        out.append("未发现任何问题。")
        return "\n".join(out)
    out.append("## 规则命中分布")
    out.append("")
    out.append("| 规则 | 名称 | 次数 |")
    out.append("| --- | --- | --- |")
    for rid, cnt in sorted(res.by_rule().items()):
        name = next((f.rule_name for f in res.findings if f.rule_id == rid), "")
        out.append("| %s | %s | %d |" % (rid, name, cnt))
    out.append("")
    out.append("## 告警明细")
    out.append("")
    for f in res.sorted_findings():
        out.append("### [%s] %s — 第 %d 行" % (f.rule_id, f.rule_name, f.line))
        out.append("")
        out.append("- 严重度: %s" % SEVERITY_LABEL.get(f.severity, f.severity))
        out.append("- 位置: `%s:%d` (%s)" % (f.file, f.line, f.module))
        out.append("- 问题: %s" % f.message)
        if f.snippet:
            out.append("- 代码: `%s`" % f.snippet)
        if f.llm_verdict:
            out.append("- 裁定: **%s** — %s" %
                       (VERDICT_LABEL.get(f.llm_verdict, f.llm_verdict), f.llm_reason))
        if f.llm_patch:
            out.append("- 修复建议:")
            out.append("")
            out.append("```")
            out.append(f.llm_patch)
            out.append("```")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def to_json(res: ReviewResult) -> str:
    return json.dumps({
        "generated_at": _ts(),
        "summary": {
            "files": res.files,
            "modules": [m.name for m in res.modules],
            "severity": res.by_severity(),
            "rules_hit": res.by_rule(),
            "risk_score": res.risk_score,
            "health": res.health,
        },
        "findings": [f.to_dict() for f in res.sorted_findings()],
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# SARIF 2.1.0（供 CI / GitHub Code Scanning 消费）
# ---------------------------------------------------------------------------

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_LEVEL = {"error": "error", "warning": "warning", "info": "note"}

_RULE_DESC = {
    "R001": "组合块分支未覆盖全，综合器会推断出锁存器（latch）",
    "R002": "赋值两侧位宽不符，或常量超出目标位宽，高位被静默截断",
    "R003": "时序块内使用阻塞赋值，存在仿真与综合失配风险",
    "R004": "同一信号被多个源驱动，综合会报 multiple driver",
    "R005": "组合块读取的信号未列入敏感列表，仿真与综合行为不一致",
    "R006": "信号跨时钟域被直接采样，存在亚稳态传播风险",
    "R007": "出现不可综合结构（延时控制 / 无法映射为硬件的过程块）",
    "R008": "信号声明后从未被读取，或从未被驱动",
    "R009": "实例端口位宽与模块定义不匹配",
    "R010": "实例化时有输入端口未连接",
}


def to_sarif(res: ReviewResult) -> str:
    """输出 SARIF 2.1.0 JSON，可被 GitHub Code Scanning 等平台直接消费。

    被豁免 / 被判误报的告警不静默丢弃，而是以 `suppressions` 形式保留并附上
    理由 —— 保证"豁免不隐藏"的原则在 CI 视角下同样成立。
    """
    from . import __version__
    from .rules import RULES

    rule_index: Dict[str, int] = {}
    rules_meta: List[dict] = []
    for rid, name, _fn in RULES:
        rule_index[rid] = len(rules_meta)
        rules_meta.append({
            "id": rid,
            "name": rid,
            "shortDescription": {"text": name},
            "fullDescription": {"text": _RULE_DESC.get(rid, name)},
            "defaultConfiguration": {"level": "warning"},
        })

    results: List[dict] = []
    for f in res.sorted_findings():
        entry: Dict = {
            "ruleId": f.rule_id,
            "ruleIndex": rule_index.get(f.rule_id, 0),
            "level": SARIF_LEVEL.get(f.severity, "warning"),
            "message": {"text": f.message + (("  修复建议: " + f.hint) if f.hint else "")},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": f.file.replace("\\", "/")},
                    "region": {
                        "startLine": max(1, int(f.line)),
                        "snippet": {"text": f.snippet},
                    },
                }
            }],
            "properties": {
                "confidence": round(f.confidence, 3),
                "module": f.module,
                "status": f.status,
            },
        }
        if f.waived:
            entry["suppressions"] = [{"kind": "external",
                                      "justification": f.waive_reason}]
        elif f.llm_verdict == "false_positive":
            entry["suppressions"] = [{"kind": "external",
                                      "justification": f.llm_reason or "判定为误报"}]
        results.append(entry)

    doc = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "RTL-Sentinel",
                "version": __version__,
                "informationUri": "https://github.com/",
                "rules": rules_meta,
            }},
            "results": results,
            "invocations": [{
                "executionSuccessful": True,
                "exitCode": 1 if res.by_severity()["error"] else 0,
            }],
        }],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#F5F4F0;color:#2C2C2A;
 font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
 font-size:14px;line-height:1.7}
.wrap{max-width:1000px;margin:0 auto;padding:32px 28px 64px}
h1{font-size:24px;font-weight:600;margin:0 0 6px}
h2{font-size:17px;font-weight:600;margin:36px 0 12px;padding-bottom:8px;
 border-bottom:1px solid #E2E0D8}
.sub{color:#6B6A65;font-size:13px;margin:0}
.grid{display:grid;grid-template-columns:200px 1fr;gap:20px;margin-top:24px}
.gauge{background:#fff;border:1px solid #E2E0D8;border-radius:14px;
 padding:20px;text-align:center}
.gauge .num{font-size:40px;font-weight:600;line-height:1.1}
.gauge .lbl{font-size:12px;color:#6B6A65;letter-spacing:.06em;margin-top:6px}
.bar{height:8px;border-radius:4px;background:#EDEBE4;margin-top:14px;overflow:hidden}
.bar>i{display:block;height:100%}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{background:#fff;border:1px solid #E2E0D8;border-radius:14px;padding:16px 18px}
.card .k{font-size:12px;color:#6B6A65}
.card .v{font-size:26px;font-weight:600;line-height:1.3}
table{width:100%;border-collapse:collapse;background:#fff;
 border:1px solid #E2E0D8;border-radius:12px;overflow:hidden;font-size:13px}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid #EFEDE7}
th{background:#FAF9F6;font-weight:600;color:#55544F;font-size:12px}
tr:last-child td{border-bottom:none}
.finding{background:#fff;border:1px solid #E2E0D8;border-left-width:4px;
 border-radius:12px;padding:16px 18px;margin-bottom:12px}
.sev-error{border-left-color:#C0392B}
.sev-warning{border-left-color:#C98A16}
.sev-info{border-left-color:#2E7BB8}
.fhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.rid{font-family:ui-monospace,Consolas,monospace;font-size:12px;font-weight:600;
 background:#F0EEE7;border-radius:6px;padding:2px 8px}
.sevtag{font-size:11px;font-weight:600;border-radius:999px;padding:2px 10px}
.sev-error .sevtag{background:#FBE9E7;color:#A32D2D}
.sev-warning .sevtag{background:#FBF0DC;color:#8A5A0B}
.sev-info .sevtag{background:#E8F1F9;color:#1B5E8C}
.loc{margin-left:auto;color:#7A7973;font-size:12px;
 font-family:ui-monospace,Consolas,monospace}
.msg{margin:0 0 10px}
.code{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#FAF9F6;
 border:1px solid #E9E7DF;border-radius:8px;padding:9px 12px;overflow-x:auto;
 white-space:pre;color:#3A3A36}
.llm{margin-top:10px;padding-top:10px;border-top:1px dashed #E9E7DF;font-size:13px}
.llm .t{font-weight:600;color:#44433E;margin-bottom:4px}
.verdict{font-size:11px;font-weight:600;border-radius:999px;padding:2px 9px;
 margin-left:8px}
.v-confirmed{background:#FBE9E7;color:#A32D2D}
.v-false_positive{background:#E7F3EC;color:#2F6B4A}
.v-uncertain{background:#FBF0DC;color:#8A5A0B}
.v-waived{background:#EDEBE4;color:#5F5E5A}
.v-review{background:#E8F1F9;color:#1B5E8C}
.finding.waived{border-left-color:#B4B2A9;opacity:.78}
.patch{font-family:ui-monospace,Consolas,monospace;font-size:12px;
 background:#F7FAF6;border:1px solid #DDE8DC;border-radius:8px;padding:9px 12px;
 margin-top:6px;white-space:pre-wrap;color:#2F4A31}
.empty{background:#fff;border:1px solid #E2E0D8;border-radius:12px;padding:28px;
 text-align:center;color:#5A5A55}
footer{margin-top:40px;color:#8A8983;font-size:12px;text-align:center}
"""


def to_html(res: ReviewResult, title: str = "RTL-Sentinel 代码审查报告") -> str:
    sev = res.by_severity()
    health = res.health
    hue = "#2F6B4A" if health >= 80 else ("#C98A16" if health >= 50 else "#C0392B")

    def card(label: str, value, color: str = "#2C2C2A") -> str:
        return ('<div class="card"><div class="k">%s</div>'
                '<div class="v" style="color:%s">%s</div></div>'
                % (_esc(label), color, _esc(value)))

    cards = "".join([
        card("错误", sev["error"], "#A32D2D"),
        card("警告", sev["warning"], "#8A5A0B"),
        card("提示", sev["info"], "#1B5E8C"),
        card("审查文件", len(res.files)),
    ])

    rows = "".join(
        "<tr><td><span class='rid'>%s</span></td><td>%s</td><td>%d</td></tr>"
        % (_esc(rid), _esc(next((f.rule_name for f in res.findings
                                 if f.rule_id == rid), "")), cnt)
        for rid, cnt in sorted(res.by_rule().items())
    ) or "<tr><td colspan='3'>无</td></tr>"

    blocks: List[str] = []
    for f in res.sorted_findings():
        llm = ""
        if f.llm_verdict:
            v = f.llm_verdict
            llm = ('<div class="llm"><div class="t">语义推理裁定'
                   '<span class="verdict v-%s">%s</span></div><div>%s</div>'
                   % (_esc(v), _esc(VERDICT_LABEL.get(v, v)), _esc(f.llm_reason)))
            if f.llm_patch:
                llm += '<div class="patch">%s</div>' % _esc(f.llm_patch)
            if f.llm_verify:
                llm += '<div style="margin-top:8px;color:#6B6A65">验证方法: %s</div>' % _esc(f.llm_verify)
            llm += "</div>"
        # 豁免标记: 豁免不隐藏 —— 被豁免的告警依然展示，只是标明理由
        waived_tag = ""
        if f.waived:
            waived_tag = ('<span class="sevtag v-waived">已豁免 · %s</span>'
                          % _esc(f.waive_reason))
        if "needs_review" in f.tags:
            waived_tag += '<span class="sevtag v-review">待复核 · LLM 线索</span>'
        blocks.append(
            '<div class="finding %s%s">'
            '<div class="fhead"><span class="rid">%s</span>'
            '<span class="sevtag">%s</span>%s'
            '<span style="font-weight:600">%s</span>'
            '<span class="loc">%s : %d</span></div>'
            '<div class="msg">%s</div>'
            '%s%s</div>'
            % (SEV_CLASS.get(f.severity, "sev-info"), " waived" if f.waived else "",
               _esc(f.rule_id),
               _esc(SEVERITY_LABEL.get(f.severity, f.severity)), waived_tag,
               _esc(f.rule_name),
               _esc(os.path.basename(f.file)), f.line, _esc(f.message),
               ('<div class="code">%s</div>' % _esc(f.snippet)) if f.snippet else "",
               llm)
        )
    body = "".join(blocks) or ('<div class="empty">未发现任何问题，'
                               '该设计通过全部 %d 条规则检查。</div>' % len(_rules_count()))

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body><div class="wrap">
<h1>%s</h1>
<p class="sub">生成时间 %s ｜ 静态规则引擎 + 语义推理双引擎架构</p>
<div class="grid">
  <div class="gauge"><div class="num" style="color:%s">%.1f</div>
    <div class="lbl">健康分 / 100</div>
    <div class="bar"><i style="width:%.1f%%;background:%s"></i></div>
    <div class="lbl" style="margin-top:10px">风险分 %.1f</div></div>
  <div class="cards">%s</div>
</div>
<h2>规则命中分布</h2>
<table><thead><tr><th>规则</th><th>名称</th><th>命中次数</th></tr></thead>
<tbody>%s</tbody></table>
<h2>告警明细</h2>
%s
<footer>RTL-Sentinel v0.1.0 ｜ 规则引擎输出确定性告警，LLM 仅对告警做裁定与解释，不产生新告警</footer>
</div></body></html>""" % (_esc(title), _CSS, _esc(title), _ts(), hue, health,
                          health, hue, res.risk_score, cards, rows, body)


def _rules_count():
    from .rules import RULES
    return RULES
