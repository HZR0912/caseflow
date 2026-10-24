"""输入护栏：注入检测 → PII 脱敏 → 意图分类 → 槽位抽取。

设计原则（与 `core/policy` 保持一致）：
- 每层只给事实与证据，不替调用方做处置决定；
- 每层可单独评测，误报/漏报能定位到具体规则；
- 检测器统一面向 Protocol，W5 接 LLM 分类器时替换实现即可，规则版保留为降级链。
"""

from core.guardrails.normalize import NormalizedInput, normalize

__all__ = ["NormalizedInput", "normalize"]
