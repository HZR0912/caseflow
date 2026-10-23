"""图状态定义。

W1 只有最小状态，字段名刻意与后续阶段对齐：
- `task` 对应真实图里的 intent + slots
- `approval` 对应 interrupt 的恢复值（W6 的审批台写入）
- `trace` 只用于演示断点恢复时的步骤完整性，真实图里由 OTel span 承担
"""

from __future__ import annotations

from typing import TypedDict


class DemoState(TypedDict, total=False):
    task: str
    echo: str
    approval: str
    report: str
    trace: list[str]
