"""环境自检：确认 LLM 相关环境变量是否配置正确、能否连通。

用途: 接真实大模型（DeepSeek / 通义 / 本地 Ollama 等）后的第一步验证。
不依赖任何第三方库，也不需要联网以外的前置条件。

用法（PowerShell 单行，避免多行粘贴被当成续行）:
    cd C:\\Users\\byx\\WorkBuddy\\2026-10-09-20-58-39\\rtl-sentinel; python check_llm.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rtl_sentinel  # noqa: E402,F401  必须最先导入：它触发本地配置自动加载

VARS = [
    ("RTL_SENTINEL_API_KEY", "API 密钥", True),
    ("RTL_SENTINEL_BASE_URL", "OpenAI 兼容端点", False),
    ("RTL_SENTINEL_MODEL", "模型名", False),
]


def main() -> int:
    print("=" * 62)
    print("RTL-Sentinel 环境自检")
    print("=" * 62)
    print("Python: %s" % sys.version.split()[0])
    if rtl_sentinel.ENV_FILE_LOADED:
        print("已从配置文件加载: %s" % rtl_sentinel.ENV_FILE_LOADED)
    else:
        print("未找到本地配置文件（也支持系统环境变量）")
    print()
    print("--- 环境变量 ---")
    key = os.environ.get("RTL_SENTINEL_API_KEY", "")
    base = os.environ.get("RTL_SENTINEL_BASE_URL", "")
    model = os.environ.get("RTL_SENTINEL_MODEL", "")
    print("  %-24s %s" % (VARS[0][0], "已设置（长度 %d）" % len(key) if key else "✗ 未设置"))
    print("  %-24s %s" % (VARS[1][0], base if base else "✗ 未设置（会退回 OpenAI 默认端点）"))
    print("  %-24s %s" % (VARS[2][0], model if model else "未设置（默认 gpt-4o-mini）"))
    print()

    if not key:
        print("--- 下一步 ---")
        print("在 PowerShell 里执行（把第一行换成你的 key）:")
        print('  setx RTL_SENTINEL_API_KEY "sk-你的key"')
        print('  setx RTL_SENTINEL_BASE_URL "https://api.deepseek.com/v1"')
        print('  setx RTL_SENTINEL_MODEL "deepseek-chat"')
        print()
        print("然后【关闭终端重新打开】再运行本脚本 —— setx 不影响已开的终端。")
        return 2

    print("--- 连通性测试 ---")
    print("  端点: %s" % (base or "https://api.openai.com/v1（默认）"))
    print("  模型: %s" % (model or "gpt-4o-mini"))
    try:
        from rtl_sentinel.llm import OpenAICompatProvider
        provider = OpenAICompatProvider(
            model=model or "gpt-4o-mini",
            base_url=base or None,
            timeout=30,
        )
        reply = provider.complete("你是一个测试助手，回答要极简。", "只回复两个字：连通")
        print()
        print("  ✅ 连通成功，模型回复: %s" % reply.strip()[:30])
        print("  ✅ 后端标识: %s" % provider.name)
        print()
        print("--- 可以开始用了 ---")
        print("  python -m rtl_sentinel.cli samples -f html -o out\\report_llm.html")
        return 0
    except Exception as exc:                        # noqa: BLE001
        print()
        print("  ✗ 调用失败: %s: %s" % (type(exc).__name__, exc))
        print()
        print("常见原因:")
        print("  1. BASE_URL 没设 -> 会去连 OpenAI，国内通常不通；应设为 https://api.deepseek.com/v1")
        print("  2. Key 错误或已被删除")
        print("  3. 网络/代理问题")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
