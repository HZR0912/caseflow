"""槽位抽取：从自然语言里只取**说出来了的**值，缺的进追问清单，绝不推断。

为什么不用"让模型填 schema"：
W5 之后模型当然会参与，但护栏层的职责是给出一个**独立的**第二视角——
模型抽取的结果和护栏抽取的结果不一致时，差异本身就是信号（可能是提示词注入，
也可能是模型在编）。规则版抽取可以做到每个值都有原文 span 佐证。

槽位的三个来源，优先级从高到低：
1. **本轮明确说出的值**（本模块负责）
2. **上一轮已确认的值**（merge 时保留，不重复追问）
3. **记忆里的默认值**（W9 之后接入；本模块只留 `existing` 参数）

日期解析只支持相对表述的一个子集（下周X / X点 / X月X日 / 月底 / N天后 / N个月），
且**必须传入参照日期**。不传就报错而不是拿 `date.today()`——测试和回放要可复现，
"今天"必须是调用方的输入，不是系统状态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# ---------------------------------------------------------------- 日期解析

WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_ABS_DATE = re.compile(r"(?:(\d{4})\s*年)?(\d{1,2})\s*月(\d{1,2})[日号]?")
_MONTH_END = re.compile(r"(\d{1,2})\s*月底|月底")
_REL_DAY = re.compile(
    r"(今天|明天|后天|大后天|下下周([一二三四五六日天])"
    r"|下(?:周|星期)([一二三四五六日天])|本周([一二三四五六日天]))"
)
_N_DAYS = re.compile(r"(\d{1,3}|两|三|四|五|六|七|八|九|十)\s*(?:个)?(?:工作)?(?:天|日)后")
_N_MONTHS = re.compile(r"(\d{1,2}|两|三|四|五|六|七|八|九|十|半)\s*个?月(?:后)?")

# 日期的所有候选模式。多日期场景（"下周一到岗，权限给到 6 月底"）必须逐段扫描，
# 只取第一个会把起止日期搞混。
_DATE_PATTERNS = (_MONTH_END, _ABS_DATE, _REL_DAY, _N_DAYS, _N_MONTHS)

_CN_NUM = {"两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _num(token: str) -> int:
    return _CN_NUM.get(token, 0) if token in _CN_NUM else int(token)


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def parse_dates(text: str, *, today: date) -> list[tuple[int, int, date]]:
    """找出文本里**全部**日期提及，按出现位置排序返回 (start, end, date)。

    单日期抽取用 `parse_date`；多日期场景（"下周一到岗，权限给到 6 月底"）需要
    这层才能把起止日期分开。重叠时取更早出现的那个（"6 月底"和"月底"同时命中时保留前者）。
    """
    found: list[tuple[int, int, date]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < other_end and other_start < end for other_start, other_end in occupied):
                continue
            parsed = parse_date(match.group(0), today=today)
            if parsed is None:
                continue
            occupied.append((start, end))
            found.append((start, end, parsed))
    return sorted(found, key=lambda item: item[0])


def parse_date(text: str, *, today: date) -> date | None:
    """从一段文本里解析出最可能的日期。解析不出来返回 None，不猜。"""
    month_match = _MONTH_END.search(text)
    if month_match and "月底" in text:
        raw = month_match.group(1)
        month = int(raw) if raw and raw.isdigit() else today.month
        year = today.year
        candidate = _month_end(year, month)
        if candidate < today:  # 「3月底」在 10 月说，指明年
            candidate = _month_end(year + 1, month)
        return candidate

    absolute = _ABS_DATE.search(text)
    if absolute:
        year_raw, month, day = absolute.groups()
        year = int(year_raw) if year_raw else today.year
        candidate = date(year, int(month), int(day))
        if not year_raw and candidate < today - timedelta(days=1):
            candidate = date(year + 1, int(month), int(day))
        return candidate

    relative = _REL_DAY.search(text)
    if relative:
        word = relative.group(1)
        if word == "今天":
            return today
        if word == "明天":
            return today + timedelta(days=1)
        if word == "后天":
            return today + timedelta(days=2)
        if word == "大后天":
            return today + timedelta(days=3)
        if relative.group(2):  # 下下周X
            weekday = WEEKDAYS[relative.group(2)]
            return _next_weekday(today, weekday) + timedelta(days=7)
        if relative.group(3):  # 下周X / 下星期X
            return _next_weekday(today, WEEKDAYS[relative.group(3)])
        if relative.group(4):  # 本周X
            return _this_weekday(today, WEEKDAYS[relative.group(4)])

    days = _N_DAYS.search(text)
    if days:
        return today + timedelta(days=_num(days.group(1)))

    months = _N_MONTHS.search(text)
    if months:
        raw = months.group(1)
        count = 6 if raw == "半" else _num(raw)
        month = today.month + count
        year = today.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(today.day, _month_end(year, month).day)
        return date(year, month, day)
    return None


def _next_weekday(today: date, weekday: int) -> date:
    """下周X：下一自然周（周一起）的第 X 天。"""
    days_to_monday = 7 - today.weekday() if today.weekday() != 0 else 7
    next_monday = today + timedelta(days=days_to_monday)
    return next_monday + timedelta(days=weekday)


def _this_weekday(today: date, weekday: int) -> date:
    monday = today - timedelta(days=today.weekday())
    candidate = monday + timedelta(days=weekday)
    return candidate if candidate >= today else candidate + timedelta(days=7)


# ---------------------------------------------------------------- 槽位定义


@dataclass(frozen=True)
class SlotSpec:
    name: str
    description: str
    required: bool
    kind: str = "text"  # text / date / int / enum
    question: str = ""
    enum: tuple[str, ...] = ()


@dataclass
class SlotState:
    values: dict[str, object] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # 值 → 来源：turn / confirmed / memory
    missing: list[SlotSpec] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    def model_dump(self) -> dict:
        return {
            "values": self.values,
            "sources": self.sources,
            "missing": [spec.name for spec in self.missing],
            "questions": self.questions,
            "warnings": self.warnings,
            "complete": self.complete,
        }


# 每个意图的最小必要槽位。只列**真的影响执行**的字段：多问一个不影响的字段
# 就是多一次来回拉扯，而设计方案明确要求「只问真的缺的，一次问完」。
SLOTS: dict[str, tuple[SlotSpec, ...]] = {
    "provision_employee": (
        SlotSpec("name", "员工姓名", True, question="这几位同事的姓名是？"),
        SlotSpec("role", "岗位/团队", True, question="岗位或所在团队是什么（决定默认权限）？"),
        SlotSpec("start_date", "入职日期", True, "date", "哪天到岗？"),
        SlotSpec("end_date", "权限截止日期（实习生/外包）", False, "date"),
        SlotSpec("email_domain", "邮箱域", False),
        SlotSpec("mentor", "导师", False),
    ),
    "offboard_employee": (
        SlotSpec("name", "员工姓名", True, question="离职的是哪位同事？"),
        SlotSpec("last_day", "最后工作日", True, "date", "最后工作日是哪天？"),
    ),
    "grant_access": (
        SlotSpec("name", "申请人", True, question="给谁申请？"),
        SlotSpec("system", "目标系统", True, enum=("生产库", "代码仓库", "工单系统", "采购系统")),
        SlotSpec("permission", "权限级别", True, enum=("只读", "读写", "管理员")),
        SlotSpec("end_date", "权限有效期", True, "date", "权限要用到哪天？"),
    ),
    "purchase_software": (
        SlotSpec("item", "采购物品", True, question="要买什么？"),
        SlotSpec("quantity", "数量", True, "int", "数量是多少？"),
        SlotSpec("budget_cents", "预算（分）", False, "int"),
    ),
    "refund": (
        SlotSpec("order_id", "订单号", True, question="是哪一笔订单？"),
        SlotSpec("amount", "退款金额", False, "int"),
    ),
    "ticket_work": (
        SlotSpec("title", "工单标题", True, question="工单标题写什么？"),
        SlotSpec("project", "项目", False),
        SlotSpec("assignee", "处理人", False),
    ),
    "policy_query": (),  # 只读问答，无必填槽位
    "data_export": (
        SlotSpec("scope", "导出范围", True, question="要导出哪个范围的数据？"),
        SlotSpec("recipient", "接收人", True, question="发给谁？"),
    ),
}


# 槽位的本轮正则抽取。都是"值紧接着触发词"的形态；抽不到的进追问，而不是猜。
_EMAIL_DOMAIN = re.compile(r"@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_QUANTITY = re.compile(r"(\d+)\s*(?:个|台|套|份|人|席|位)")
_AMOUNT = re.compile(r"(?:预算|不超过|最多|金额)\s*(?:是|为)?\s*(\d+(?:\.\d+)?)\s*(万|千|元)?")
_ORDER = re.compile(r"(?:订单号?|单号)\s*(?:是|为)?\s*([A-Za-z0-9\-]{6,})")
# 「给张三开邮箱」——姓名是给/让/请 与 动作动词之间、由「的」隔开的那一段。
# 不锚定会吃进「我给张工」。常见姓氏集合做首字约束，2–3 个字。
_SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹苏潘葛范彭"
_PRONOMINAL = ("我", "你", "他", "她", "我们", "你们", "大家", "自己")
# 「帮我给张三开个邮箱」——中间夹着"我"，且不能把代词当成姓名。
_NAME_AFTER = re.compile(
    rf"(?:给|让|请|帮)\s*(?:我\s*|一下\s*)?([{_SURNAMES}][一-鿿]{{1,2}}?)"
    r"(?:开|建|办|申请|准备|分配|发|离职|交接|入职)"
)
# 「张三下周五入职」——姓名直接出现在日期/动作前（无"给"引导）。
_NAME_BEFORE = re.compile(
    rf"(?<![一-鿿])([{_SURNAMES}][一-鿿]{{1,2}}?)"
    r"(?=下周|本周|明天|后天|今天|\d{1,2}\s*月|入职|离职|到岗)"
)


def extract(
    text: str,
    intent: str,
    *,
    today: date,
    existing: dict[str, object] | None = None,
) -> SlotState:
    """抽取本轮槽位并与已确认值合并。

    `existing` 是上一轮/记忆里已确认的值，**优先级高于本轮未提及**、
    低于本轮明确重述。用户在追问后回答"改到下周三"，以本轮为准。
    """
    specs = {spec.name: spec for spec in SLOTS.get(intent, ())}
    state = SlotState()

    for name, value in (existing or {}).items():
        if name in specs:
            state.values[name] = value
            state.sources[name] = "confirmed"

    per_turn: dict[str, object] = {}

    if any(name in specs for name in ("start_date", "end_date", "last_day")):
        mentions = parse_dates(text, today=today)
        if mentions:
            # 日期按位置分配给槽位。起始类槽位由意图决定（入职→start_date，离职→last_day）；
            # 句子里出现"权限/账号/有效期给到/截止"语义时，**最后一处**日期归属截止类槽位，
            # 其余按出现顺序喂给起始类——这覆盖了"下周一到岗，权限给到 6 月底"和
            # "申请读写权限，用到 11 月底"两种说法。
            primary = (
                "last_day"
                if intent == "offboard_employee"
                else "start_date"
                if "start_date" in specs
                else "end_date"
            )
            cutoff_semantics = bool(
                re.search(r"(权限|账号|有效期|报销)[^。\n]{0,8}?(给到|到|截止|有效到|用到)", text)
            )
            dates = [parsed for _, _, parsed in mentions]
            if cutoff_semantics and "end_date" in specs and len(dates) >= 2:
                per_turn[primary] = dates[0].isoformat()
                per_turn["end_date"] = dates[-1].isoformat()
            elif cutoff_semantics and "end_date" in specs:
                per_turn["end_date"] = dates[0].isoformat()
            else:
                per_turn[primary] = dates[0].isoformat()
                if len(dates) > 1 and "end_date" in specs and "end_date" not in per_turn:
                    per_turn["end_date"] = dates[1].isoformat()

    if "email_domain" in specs:
        match = _EMAIL_DOMAIN.search(text)
        if match:
            per_turn["email_domain"] = match.group(1)

    if "quantity" in specs:
        match = _QUANTITY.search(text)
        if match:
            per_turn["quantity"] = int(match.group(1))

    if "budget_cents" in specs:
        match = _AMOUNT.search(text)
        if match:
            number = float(match.group(1))
            unit = match.group(2)
            if unit == "万":
                number *= 10000
            elif unit == "千":
                number *= 1000
            per_turn["budget_cents"] = int(number * 100)

    if "amount" in specs:
        match = _AMOUNT.search(text)
        if match:
            per_turn["amount"] = int(float(match.group(1)))

    if "order_id" in specs:
        match = _ORDER.search(text)
        if match:
            per_turn["order_id"] = match.group(1)

    if "name" in specs:
        match = _NAME_AFTER.search(text) or _NAME_BEFORE.search(text)
        if match and match.group(1) not in _PRONOMINAL:
            per_turn["name"] = match.group(1)

    if "role" in specs:
        match = re.search(
            r"做([一-鿿]{2,8}?)(?:的|开发|工程师|团队|方向|[，,。]|$)", text
        ) or re.search(r"([一-鿿]{2,6}(?:组|部|团队))", text)
        if match:
            per_turn["role"] = match.group(1)

    # 枚举槽位只认清单里的值，抽不到就进追问——把"读写"猜成"管理员"是这类系统最危险的错误。
    if "system" in specs:
        for candidate in specs["system"].enum:
            if candidate in text:
                per_turn["system"] = candidate
                break
    if "permission" in specs:
        for candidate in specs["permission"].enum:
            if candidate in text:
                per_turn["permission"] = candidate
                break

    # 部门/范围：只取"X 部门/X 组"这种显式结构，不猜。
    if "scope" in specs:
        match = re.search(
            r"([A-Za-z一-鿿]{1,10}?\s?(?:部门|事业部|组))[^。\n]{0,6}?(?:所有|全部|的)",
            text,
        )
        if match:
            per_turn["scope"] = match.group(1)
    if "recipient" in specs:
        match = re.search(
            r"(?:发给|发送给|抄送|发|给)\s*(?:我|我的)?\s*(?:邮箱)?\s*([一-鿿A-Za-z]{2,12}?)?"
            r"(?:[，,。]|$|的邮箱)",
            text,
        )
        if match and match.group(1):
            per_turn["recipient"] = match.group(1)
        elif re.search(r"(发我|发给我|给我一份|发到我)", text):
            per_turn["recipient"] = "self"

    for name, value in per_turn.items():
        state.values[name] = value
        state.sources[name] = "turn"

    for spec in SLOTS.get(intent, ()):
        if spec.required and spec.name not in state.values:
            state.missing.append(spec)
            if spec.question:
                state.questions.append(spec.question)

    # 一次问完，但最多问 3 个：一次抛 6 个问题比来回拉扯更劝退。
    state.questions = state.questions[:3]
    return state
