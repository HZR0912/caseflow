"""输入归一化：把"看起来不像攻击"的变形折回可匹配的形态。

为什么归一化必须先于一切检测规则：
攻击者不需要写「忽略以上指令」。同一个意思可以写成
「忽 略 以 上 指 令」（插入空格）、「忽略以上指令​」（尾部零宽字符）、
「IGnore ALL previous instructions」（大小写混排）、
「ｇｎｏｒｅ」（全角），或者把整句 base64 编码后请模型"解码执行"。

**关键约束：归一化只用于匹配，不用于存储。**
返回值同时给出 `text`（折叠后文本）与 `source_index`（折叠后每个字符对应原文位置），
所以检测命中的 span 可以映射回用户原始输入——审计日志要能指出"拦的是原话的哪一段"，
而不是指着一个被我们改写过的版本说"你说了这个"。

不做的事情，以及为什么：
- **不做任意长度的同形字表**。西里尔字母 `а`(U+0430) 冒充拉丁 `a` 这类攻击确实存在，
  但一个覆盖不全的表只能防住测试用例里出现的那些字母，纯粹是自我安慰。
  中文业务场景里更常见的变形是**全角、零宽、空白插入、大小写**，这四类做了就有实际收益。
- **不在归一化阶段解码 base64**。解码是"展开检测"（见 `expandings`），产物是**额外的待扫描文本**，
  不能替换原文——否则原文里一段合法的 base64（比如一个 token）会被改写成乱码。
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field

# 零宽与方向控制字符：渲染上不可见，但会把连续词切开，让正则失配。
_INVISIBLE = dict.fromkeys(
    map(ord, "​‌‍⁠﻿­‎‏‪‫‬‭‮"),
    None,
)

# 空白插入攻击：单个汉字之间夹空格/点号/下划线。
_TOKEN_BREAK = re.compile(r"(?<=[一-鿿])[\s._·•*\-—](?=[一-鿿])")

_CJK = re.compile(r"[一-鿿]")

# 一段连续的可 base64 字符（含常见 padding）。
_B64_BLOB = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


def fold(text: str) -> tuple[str, list[int]]:
    """NFKC + 去零宽 + 折中文间隔符 + 压缩空白，返回折叠文本与原文索引。"""
    kept: list[str] = []
    index: list[int] = []
    for position, char in enumerate(text):
        if not char or ord(char) in _INVISIBLE:
            continue
        for piece in unicodedata.normalize("NFKC", char):
            kept.append(piece)
            index.append(position)
    folded = "".join(kept)
    # 折叠空白为单空格，同时记录每个保留字符的来源位置。
    squeezed: list[str] = []
    squeezed_index: list[int] = []
    previous_space = False
    for char, source in zip(folded, index, strict=True):
        if char.isspace():
            if previous_space:
                continue
            squeezed.append(" ")
            squeezed_index.append(source)
            previous_space = True
        else:
            squeezed.append(char)
            squeezed_index.append(source)
            previous_space = False
    return "".join(squeezed), squeezed_index


def fold_for_match(text: str) -> str:
    """仅用于匹配的更强折叠：去掉汉字之间的间隔符号，并统一小写。

    只给匹配器用。`source_index` 在这条路径上不再对应原文，因此命中 span
    必须由**基础 `fold` 的结果**给出，这个函数只回答"是否命中"。
    """
    folded, _ = fold(text)
    compact = _TOKEN_BREAK.sub("", folded)
    return compact.lower()


def compact_index(text: str, source_index: list[int] | None = None) -> tuple[str, list[int]]:
    """`fold_for_match` 的带索引版本：压缩后的每个字符对应的**原文**偏移。

    抗空格插入的规则在压缩文本上命中，需要把位置映射回原文给审计用。
    压缩时空白和 `._·•*-—` 被删除，其余字符的索引与 `fold` 一致。
    `source_index` 传入 `NormalizedInput.source_index` 时，返回值直接是原文偏移；
    不传则只返回**传入文本**内的偏移。
    """
    folded, index = fold(text)
    if source_index is not None:
        # 传入文本已经是 fold 过的（NormalizedInput.text），fold 不会改变字符，
        # 两边的下标一一对应；换成对原文的偏移交给调用方。
        index = [source_index[position] for position in range(len(folded))]
    kept: list[str] = []
    kept_index: list[int] = []
    for position, (char, source) in enumerate(zip(folded, index, strict=True)):
        if char.isspace():
            continue
        if char in "._·•*-—" and position > 0 and folded[position - 1].strip():
            continue
        kept.append(char.lower())
        kept_index.append(source)
    return "".join(kept), kept_index


def has_obfuscation(text: str) -> list[str]:
    """报告文本里存在的变形痕迹，用于"为什么拦"的解释，不用于拦截判定本身。"""
    traces: list[str] = []
    if any(ord(char) in _INVISIBLE for char in text):
        traces.append("zero_width")
    if _TOKEN_BREAK.search(text):
        traces.append("interleaved_separators")
    if any(char.isalpha() and unicodedata.normalize("NFKC", char) != char for char in text):
        traces.append("fullwidth")
    if "　" in text:
        traces.append("ideographic_space")
    return traces


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    printable = sum(1 for char in text if char.isprintable() or char in "\n\t")
    return printable / len(text)


@dataclass
class Expansion:
    """一段被展开出来的附加文本（如 base64 解码结果）。"""

    kind: str
    text: str
    origin_start: int
    origin_end: int


def expandings(text: str) -> list[Expansion]:
    """寻找需要二次扫描的隐藏载荷，而不是只检查表层文本。

    base64 是当前唯一实现的展开方式：长度 ≥24 的 blob 尝试解码，
    解码结果必须是可打印文本且**包含足够多的字母或汉字**才算数——
    随机二进制解码出来往往是乱码，"乱码也去跑注入正则"只会制造误报。
    """
    found: list[Expansion] = []
    for match in _B64_BLOB.finditer(text):
        raw = match.group(0)
        # 无 padding 时补齐，否则 base64 拒绝解码。
        padded = raw + "=" * (-len(raw) % 4)
        try:
            decoded = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            payload = decoded.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _printable_ratio(payload) < 0.95:
            continue
        letters = sum(1 for char in payload if char.isalpha() or _CJK.match(char))
        if letters < 8:
            continue
        found.append(
            Expansion(
                kind="base64",
                text=payload,
                origin_start=match.start(),
                origin_end=match.end(),
            )
        )
    return found


@dataclass
class NormalizedInput:
    """归一化结果。`raw` 只在这里出现一次，任何下游不得再持久化它。"""

    raw: str
    text: str
    source_index: list[int] = field(default_factory=list)
    obfuscation: list[str] = field(default_factory=list)
    expansions: list[Expansion] = field(default_factory=list)

    def to_source_span(self, folded_start: int, folded_end: int) -> tuple[int, int]:
        """把折叠文本上的 [start, end) 映射回原文偏移。"""
        if not self.source_index or folded_start >= len(self.source_index):
            return folded_start, folded_end
        last = min(folded_end - 1, len(self.source_index) - 1)
        return self.source_index[folded_start], self.source_index[last] + 1

    def scan_texts(self) -> list[tuple[str, str]]:
        """所有应参与规则匹配的文本：表层折叠文本 + 展开载荷。"""
        return [("surface", self.text), *(("expansion:" + e.kind, e.text) for e in self.expansions)]


def normalize(text: str) -> NormalizedInput:
    folded, index = fold(text or "")
    return NormalizedInput(
        raw=text or "",
        text=folded,
        source_index=index,
        obfuscation=has_obfuscation(text or ""),
        expansions=expandings(text or ""),
    )
