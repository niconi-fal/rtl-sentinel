"""RTL-Sentinel —— 面向集成电路设计的 RTL 智能审查 Agent.

设计理念（本项目的核心架构判断）:
    纯 LLM 做 RTL 审查是不可靠的。LLM 在「位宽算术、数据流追踪、跨时钟域关系」
    这类需要精确符号计算的任务上会系统性出错（tokenization 破坏数字位级语义）。
    因此本项目采用「确定性规则引擎 + 概率性语义推理」双引擎分工:
        - 规则引擎: 负责精确、可复现、零误报的结构性事实提取（如位宽截断、多驱动）
        - LLM 推理层: 只对规则引擎已产出的告警做「裁定 + 归因 + 修复建议」,
                     禁止自主发明新告警 —— 这是抑制幻觉的关键约束。
"""

__version__ = "0.1.0"
__all__ = ["model", "parser", "rules", "engine", "llm", "report"]


#: 记录实际加载的配置文件路径（供自检脚本显示）
ENV_FILE_LOADED: str = ""


def _load_local_env() -> None:
    """加载本地密钥配置（可选）。

    查找顺序: ./rtl-sentinel.env → ./.env → ~/.rtl-sentinel.env

    设计取向:
      - **已存在的系统环境变量优先**，配置文件不会覆盖它们 —— 这样用户
        既可以用系统环境变量，也可以用本地文件，且临时 export 始终生效。
      - 只做最简单的 `KEY=VALUE` 解析，不引入第三方 dotenv 依赖。
      - 这三个文件名都已写入 .gitignore，避免密钥被误提交。

    为什么需要它: 直接读环境变量时，任何"设置后未重启的进程"都读不到新值
    （进程环境是启动时的快照）。本地文件绕开了这个限制。

    注意: 本函数在导入 rtl_sentinel 包时执行。**先读 os.environ 再导入本包的
    脚本会读不到配置** —— 请把 `import rtl_sentinel` 放在最前面。
    """
    import os

    global ENV_FILE_LOADED
    candidates = [
        os.path.join(os.getcwd(), "rtl-sentinel.env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.expanduser("~"), ".rtl-sentinel.env"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except OSError:
            continue
        ENV_FILE_LOADED = path
        break


_load_local_env()
