"""制度文档切块。

三条硬要求，全部来自"引用必须可验证"这个目标：

1. **每块记下它在源文件里的字符区间**（char_start / char_end）。引用校验要能
   回到原文做逐字比对，而不是信任模型说"第 3 条这么写的"。
2. **按标题切，不按字数切。** 制度文档的价值单位是"条"，按 500 字硬切会把
   一条规定劈成两半，检索到半条等于没检索到。
3. **版本是块的属性。** 同一个 doc_key 的多版本必须能同时进库，由检索层按
   effective_date 解析——否则"新规定覆盖旧规定"只能靠提示词祈祷。

切块粒度取到 `###`（条）。`##`（章）只作为上下文前缀写进 section_path，
单独成块没有意义："第二章 审批"不含任何可执行的规定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
FRONT_MATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.S)


@dataclass
class PolicyDoc:
    doc_key: str
    title: str
    version: str
    effective_date: date
    owner: str
    source: str
    body: str
    superseded_date: date | None = None

    @property
    def doc_id(self) -> str:
        return f"{self.doc_key}@{self.version}"


@dataclass
class Chunk:
    chunk_id: str
    doc_key: str
    version: str
    title: str
    section_path: str
    text: str
    char_start: int
    char_end: int
    effective_date: date
    source: str
    superseded_date: date | None = None
    meta: dict = field(default_factory=dict)


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def parse_document(path: Path) -> PolicyDoc:
    raw = path.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(raw)
    if not match:
        raise ValueError(f"{path.name}: 缺少 YAML front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    missing = {"doc_key", "title", "version", "effective_date", "owner"} - set(meta)
    if missing:
        raise ValueError(f"{path.name}: front matter 缺少字段 {sorted(missing)}")
    return PolicyDoc(
        doc_key=str(meta["doc_key"]),
        title=str(meta["title"]),
        version=str(meta["version"]),
        effective_date=_as_date(meta["effective_date"]),
        owner=str(meta["owner"]),
        source=path.name,
        body=raw[match.end() :],
        superseded_date=_as_date(meta["superseded_date"]) if meta.get("superseded_date") else None,
    )


def split_document(doc: PolicyDoc) -> list[Chunk]:
    """按 `###` 切块（没有 `###` 时退回 `##`，再退回整篇）。

    返回的 char_start / char_end 是相对 `doc.body` 的偏移，
    `doc.body[start:end]` 必须与 `chunk.text` 逐字相等——这是引用校验的地基。

    ⚠ 边界规则（踩过的坑）：块的终点必须是**下一个任意级别的标题**，而不是下一个
    `###`。最初写成 `stop = 下一个 ### 的位置`，于是每章最后一条会把下一章的
    `## 第 X 章` 标题吞进自己的正文（实测 15/40 块受影响）。这不仅是"多了两行"：
    额外标题里的词会进入该块的检索指纹，导致跨章命中；引用高亮时也会把下一章标题
    一起标上。原先"slice == text"的自洽性测试抓不到它——两边是同一段错误文本。
    所以必须有 `test_chunk_never_contains_another_heading` 这类**语义**断言。
    """
    headings = [
        (m.start(), m.end(), len(m.group(1)), m.group(2).strip())
        for m in HEADING.finditer(doc.body)
    ]
    for level in (3, 2):
        cuts = [h for h in headings if h[2] == level]
        if cuts:
            break
    else:
        cuts = []

    if not cuts:
        return [_chunk(doc, 0, "整篇", "整篇", 0, len(doc.body))]

    starts = [h[0] for h in headings]
    chunks: list[Chunk] = []
    for index, (pos, end, level, title) in enumerate(cuts):
        # 终点：下一个任意级别的标题；没有则到文末
        following = next((s for s in starts if s > pos), len(doc.body))
        # 路径：pos 之前、级别由浅到深**严格递减**的最近祖先链。
        # 不能简单地取"所有级别更浅的标题"——那样同级的「第一章」「第二章」「第三章」
        # 会全部串进路径里（实测出现「第一章 总则 > 第二章 审批 > 第三章 住宿标准 > 第 6 条」）。
        ancestors: list[tuple[int, str]] = []
        for h_pos, _h_end, h_level, h_title in reversed(headings):
            if h_pos >= pos:
                continue
            if h_level < level and all(h_level < kept for kept, _ in ancestors):
                ancestors.append((h_level, h_title))
        ancestors.reverse()
        path = " > ".join([t for _, t in ancestors] + [title])
        chunks.append(_chunk(doc, index, title, path, end, following))
    return chunks


def _chunk(doc: PolicyDoc, index: int, title: str, path: str, start: int, stop: int) -> Chunk:
    raw = doc.body[start:stop]
    lead = len(raw) - len(raw.lstrip())
    start = start + lead
    stop = start + len(raw.strip())
    return Chunk(
        chunk_id=f"{doc.doc_id}#{index:03d}",
        doc_key=doc.doc_key,
        version=doc.version,
        title=doc.title,
        section_path=path,
        text=doc.body[start:stop],
        char_start=start,
        char_end=stop,
        effective_date=doc.effective_date,
        superseded_date=doc.superseded_date,
        source=doc.source,
    )


def load_corpus(directory: Path) -> tuple[list[Chunk], list[PolicyDoc]]:
    docs = [parse_document(p) for p in sorted(directory.glob("*.md"))]
    chunks = [chunk for doc in docs for chunk in split_document(doc)]
    return chunks, docs
