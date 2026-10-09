"""跨文件模块索引 —— 建立「模块名 -> 端口表」的全局视图。

为什么必需
----------
真实 RTL 工程是多文件的，模块通过实例化互连。没有全局模块视图时，
对实例连接的判断会全面失真：

    SB_SPRAM256KA ram00 (
        .ADDRESS(addr[13:0]),      // 这是 ram00 的输入！
        .CHIPSELECT(cs_0),         // 也是输入
    );

若把实例的所有端口连接都当成"驱动源"，每个输入连接都会制造一条假的多驱动告警
（在真实的 42 文件语料上实测产生 200+ 条误报）。因此必须查模块定义判断端口方向。

设计取舍
--------
- 索引只依赖各文件的模块头部（端口声明），不需完整解析，成本低。
- 查不到定义的模块（FPGA 原语如 SB_SPRAM256KA、工艺库单元、第三方 IP）**保守不判**
  —— 宁可漏报，也不制造误报。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import Signal
from .parser import parse_source


@dataclass
class ModuleDef:
    """一个模块定义的端口视图。"""

    name: str
    file: str
    line: int
    ports: Dict[str, Signal] = field(default_factory=dict)

    def port(self, name: str) -> Optional[Signal]:
        return self.ports.get(name)


class ModuleIndex:
    """全局模块定义索引。"""

    def __init__(self) -> None:
        self.modules: Dict[str, ModuleDef] = {}

    def add_source(self, src: str, path: str) -> None:
        try:
            mods, _lines = parse_source(src, path)
        except Exception:                                    # noqa: BLE001
            return
        for mod in mods:
            ports = {n: s for n, s in mod.signals.items() if s.is_port}
            # 先到先得：同名模块重复定义时保留首次出现（工程中多为一处定义）
            self.modules.setdefault(
                mod.name, ModuleDef(mod.name, path, mod.line_start, ports))

    def add_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                self.add_source(fh.read(), path)
        except OSError:
            return

    def add_files(self, paths: List[str]) -> None:
        for path in paths:
            self.add_file(path)

    def lookup(self, name: str) -> Optional[ModuleDef]:
        return self.modules.get(name)

    def port_direction(self, module: str, port: str) -> Optional[str]:
        """返回实例端口方向: input / output / inout；模块或端口未知时返回 None。"""
        mod = self.modules.get(module)
        if mod is None:
            return None
        sig = mod.ports.get(port)
        return sig.direction if sig else None

    def stats(self) -> Dict[str, int]:
        return {
            "modules": len(self.modules),
            "ports": sum(len(m.ports) for m in self.modules.values()),
        }
