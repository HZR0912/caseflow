"""PII 识别与脱敏：可逆的 token 化，而不是不可逆的涂黑。

三个设计决定，每个都有被否掉的替代方案：

1. **可逆（vault）而不是直接替换成 `***`**。
   办事 Agent 必须能在执行阶段用真实值去调 MockCorp / 真实系统——邮箱要真建、
   工单要实名。所以脱敏产物是带类型和序号的占位符 `[EMAIL_1]`，真实值进
   请求作用域的 `PiiVault`，只在调用下游工具的那一刻解析回来。
   落库、进日志、进模型上下文的永远是占位符。

2. **校验位过滤，宁漏勿错**。
   手机号 `1[3-9]\\d{9}` 会匹配到订单号里的片段；身份证末位是模 11 校验；
   银行卡 16–19 位数字要过 Luhn。不做校验的后果不只是"多打几个码"——
   每个误判都会让真实信息被替换，下游拿到 `[ID_CARD_2]` 却以为是用户真的这么写的。
   已知代价：校验位造假的测试身份证会漏掉，这是可接受的。

3. **不做自由文本人名识别，只在结构化槽位上做**。
   中文人名没有词边界，`张伟` 两个字在制度文本里可能是"张开伟岸"的一部分，
   自由扫描必然大面积误伤。人名只在**槽位抽取阶段**（"给张三开邮箱"）处理，
   而且脱敏动作发生在槽位被抽取之后——姓名进 `user_name` 槽位，正文里替换。

类型与占位符：
    phone → [PHONE_n]   id_card → [ID_CARD_n]   bank_card → [BANK_CARD_n]
    email → [EMAIL_n]   ip → [IP_n]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 校验位

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECKSUM = "10X98765432"


def valid_id_card(number: str) -> bool:
    """18 位居民身份证：GB 11643-1999 模 11 校验。"""
    if len(number) != 18:
        return False
    body = number[:17]
    if not body.isdigit():
        return False
    total = sum(int(char) * weight for char, weight in zip(body, _ID_WEIGHTS, strict=True))
    return _ID_CHECKSUM[total % 11] == number[17].upper()


def valid_luhn(number: str) -> bool:
    """银行卡 Luhn 校验。"""
    digits = [int(char) for char in number if char.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for offset, digit in enumerate(reversed(digits)):
        if offset % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# ---------------------------------------------------------------- 检测规则


@dataclass(frozen=True)
class PiiRule:
    kind: str
    pattern: re.Pattern[str]
    validator: object = None  # callable[[str], bool]
    description: str = ""


RULES: tuple[PiiRule, ...] = (
    PiiRule(
        "email",
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        description="邮箱地址",
    ),
    PiiRule(
        "id_card",
        re.compile(r"(?<![0-9A-Za-z])[1-9]\d{16}[\dXx](?![0-9A-Za-z])"),
        validator=valid_id_card,
        description="18 位身份证",
    ),
    PiiRule(
        "phone",
        # 中国大陆手机号：1[3-9] + 9 位，前后不能紧邻数字（避免切走订单号中间段）。
        re.compile(r"(?<![0-9])(?:\+?86[\s\-]?)?1[3-9]\d{9}(?![0-9])"),
        description="手机号",
    ),
    PiiRule(
        "bank_card",
        re.compile(r"(?<![0-9])(?:\d{4}[\s\-]?){3,4}\d{1,4}(?![0-9])"),
        validator=valid_luhn,
        description="银行卡",
    ),
    PiiRule(
        "ip",
        re.compile(
            r"(?<![0-9.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
            r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![0-9.])"
        ),
        description="IPv4 地址",
    ),
)


@dataclass
class PiiHit:
    kind: str
    value: str
    start: int
    end: int

    def model_dump(self) -> dict:
        return {"kind": self.kind, "start": self.start, "end": self.end}


def detect(text: str) -> list[PiiHit]:
    """找出所有 PII 片段。重叠时**取长**：身份证里天然包含 11 位手机号片段，
    先收身份证再收手机号会把一个值切成两半。"""
    hits: list[PiiHit] = []
    occupied: list[tuple[int, int]] = []
    # 校验规则优先、长模式优先：按 (是否校验, 模式长度) 排序处理。
    ordered = sorted(
        RULES, key=lambda rule: (rule.validator is None, -rule.pattern.pattern.count(r"\d"))
    )
    for rule in ordered:
        for match in rule.pattern.finditer(text):
            start, end = match.span()
            if any(start < other_end and other_start < end for other_start, other_end in occupied):
                continue
            value = match.group(0)
            if rule.validator is not None and not rule.validator(value):
                continue
            occupied.append((start, end))
            hits.append(PiiHit(kind=rule.kind, value=value, start=start, end=end))
    return sorted(hits, key=lambda hit: hit.start)


# ---------------------------------------------------------------- vault


class PiiVault:
    """请求作用域的真实值表。生命周期 = 一次请求，**不序列化、不落盘**。

    同一个值重复出现复用同一个占位符（用户不会在两句话里把同一个人写成两个实体）。
    `restore` 把占位符换回真实值，只允许在调用下游工具前使用。
    """

    def __init__(self) -> None:
        self._by_placeholder: dict[str, str] = {}
        self._by_value: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def tokenize(self, kind: str, value: str) -> str:
        if value in self._by_value:
            return self._by_value[value]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        placeholder = f"[{kind.upper()}_{self._counters[kind]}]"
        self._by_placeholder[placeholder] = value
        self._by_value[value] = placeholder
        return placeholder

    def restore(self, text: str) -> str:
        for placeholder, value in self._by_placeholder.items():
            text = text.replace(placeholder, value)
        return text

    @property
    def entries(self) -> dict[str, str]:
        """只给审计用的**键**列表（占位符 → 值），值仅存在于内存。"""
        return dict(self._by_placeholder)

    def __len__(self) -> int:
        return len(self._by_placeholder)


@dataclass
class MaskedText:
    text: str
    hits: list[PiiHit] = field(default_factory=list)
    vault: PiiVault = field(default_factory=PiiVault)

    def model_dump(self) -> dict:
        return {"text": self.text, "hits": [hit.model_dump() for hit in self.hits]}


def mask(text: str, vault: PiiVault | None = None) -> MaskedText:
    """把 PII 替换成类型化占位符。

    占位符编号按**原文从左到右**分配（读者看到的第一处是 `_1`，和白名单/审计对齐），
    替换本身从右往左做——后面的替换不影响前面的偏移。
    """
    vault = vault or PiiVault()
    hits = detect(text)
    placeholders = [vault.tokenize(hit.kind, hit.value) for hit in hits]
    masked = text
    for hit, placeholder in zip(reversed(hits), reversed(placeholders), strict=True):
        masked = masked[: hit.start] + placeholder + masked[hit.end :]
    return MaskedText(text=masked, hits=hits, vault=vault)
