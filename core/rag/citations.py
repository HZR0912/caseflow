"""引用可验证：把"模型说它引用了某段"变成"程序能证明这段真的存在"。

MigraAgent 的教训写在这里：那套系统的引用编号 `[1][2]` 是**提示词要求模型自己写的**，
从未被程序校验过，结果出现过"资料里没有的地名 + 编号照样标上"的假引用。带假出处的
编造比没有出处更危险——用户看到编号会以为有据可查。

本模块的职责就是堵住这条路：
1. 引用必须指向一个**真实存在的 chunk_id**；
2. quote 必须在 chunk 原文里**逐字出现**（归一化空白与全半角后）；
3. quote 太短的（< MIN_QUOTE）不算引用——「的」「第」这种片段能匹配上毫无意义；
4. 校验成功后**重算 quote 的真实偏移**，交给前端高亮原文，而不是让模型自己报位置。

调用方拿到 `CiteVerdict` 后如何处置是策略问题，本模块只给事实，不给建议。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from core.rag.tokenizer import normalize

MIN_QUOTE = 8

Status = Literal["ok", "unknown_chunk", "quote_not_found", "quote_too_short"]


@dataclass
class CiteVerdict:
    chunk_id: str
    status: Status
    quote: str = ""
    char_start: int | None = None  # quote 在源文件正文中的绝对偏移
    char_end: int | None = None
    matched_text: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _squeeze(text: str) -> tuple[str, list[int]]:
    """两侧文本用**同一套**归一化：去空白 + 逐字符 NFKC 折叠。

    踩过的坑：最初只对 quote 调了 `normalize()`（它会做 NFKC 折叠，把 `，` 变成 `,`、
    `：` 变成 `:`），而原文侧只去了空白。结果一句完全正确的引文被判成
    `quote_not_found`——**校验器和被校验对象用了两套不同的规范化**。
    这种 bug 的危险之处在于方向是"误杀"：真实引用被拒会让人误以为模型在编造，
    进而去改提示词，真正的问题（校验器）反而被掩盖。

    所以：两边都走 `_squeeze`，且索引映射回**原始未折叠**的字符位置，
    这样返回的 char_start / char_end 可以直接拿去前端高亮。
    """
    kept: list[str] = []
    index: list[int] = []
    for position, char in enumerate(text):
        if char.isspace():
            continue
        folded = unicodedata.normalize("NFKC", char)
        for piece in folded:  # NFKC 可能把 1 个字符展开成多个
            kept.append(piece)
            index.append(position)
    return "".join(kept), index


def verify_citation(chunk, quote: str) -> CiteVerdict:
    """`chunk` 需要提供 `chunk_id`、`text`、`char_start` 三个属性（见 chunking.Chunk）。"""
    if len(normalize(quote or "")) < MIN_QUOTE:
        return CiteVerdict(chunk_id=chunk.chunk_id, status="quote_too_short", quote=quote)

    haystack, index = _squeeze(chunk.text)
    needle = _squeeze(quote)[0]
    position = haystack.find(needle)
    if position < 0:
        return CiteVerdict(chunk_id=chunk.chunk_id, status="quote_not_found", quote=quote)

    start = index[position]
    end = index[position + len(needle) - 1] + 1
    return CiteVerdict(
        chunk_id=chunk.chunk_id,
        status="ok",
        quote=quote,
        char_start=chunk.char_start + start,
        char_end=chunk.char_start + end,
        matched_text=chunk.text[start:end],
    )


@dataclass
class CitationReport:
    verdicts: list[CiteVerdict]

    @property
    def ok(self) -> bool:
        return bool(self.verdicts) and all(v.ok for v in self.verdicts)

    @property
    def valid_ratio(self) -> float:
        if not self.verdicts:
            return 0.0
        return sum(v.ok for v in self.verdicts) / len(self.verdicts)


CITE_PATTERN = re.compile(r"\[(\d+)\]")


def check_answer(
    answer: str, chunks_by_index: dict[int, object], quotes_by_index: dict[int, str] | None = None
) -> CitationReport:
    """校验一段回答里的全部 `[n]` 引用。

    quotes_by_index 给定时逐条核对原文；未给定时只校验编号指向的 chunk 是否存在
    （"宽松模式"，用于回答正文本身没有逐字引用、但编号必须真实的场景）。
    """
    verdicts: list[CiteVerdict] = []
    for raw in CITE_PATTERN.findall(answer or ""):
        number = int(raw)
        chunk = chunks_by_index.get(number)
        if chunk is None:
            verdicts.append(CiteVerdict(chunk_id=f"[{number}]", status="unknown_chunk"))
            continue
        quote = (quotes_by_index or {}).get(number)
        if quote is None:
            verdicts.append(CiteVerdict(chunk_id=chunk.chunk_id, status="ok"))
        else:
            verdicts.append(verify_citation(chunk, quote))
    return CitationReport(verdicts)
