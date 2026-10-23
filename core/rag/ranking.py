"""与存储无关的排序逻辑：RRF、版本解析、引用拼装。

抽出来的理由很实际：**同一段逻辑要被两条路径用**。
- 线上走 `store.py`（Postgres 两路召回）
- 评测走 `eval/run_rag_eval.py`（离线 BM25 + 哈希向量，不连库）

如果版本解析只写在 SQL 那条路里，离线评测就测不到它——而"版本冲突处理对不对"
恰恰是 W3 最该被回归覆盖的部分。复制一份等于允许两份实现漂移，所以抽到这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

RRF_K = 60


@dataclass
class Candidate:
    """检索结果的统一形态，存储层与离线评测都产出它。"""

    chunk_id: str
    doc_key: str
    version: str
    title: str
    section_path: str
    section_key: str
    text: str
    source: str
    char_start: int
    char_end: int
    effective_date: date
    superseded_date: date | None = None
    score: float = 0.0
    vector_rank: int | None = None
    text_rank: int | None = None

    @property
    def citation(self) -> str:
        return f"{self.title}（{self.version}）· {self.section_path}"


@dataclass
class Ranked:
    hits: list[Candidate] = field(default_factory=list)
    superseded: list[Candidate] = field(default_factory=list)
    routes: dict[str, int] = field(default_factory=dict)

    def as_context(self) -> str:
        return "\n\n".join(
            f"[{index}] {hit.title}（{hit.version}）· {hit.section_path}\n{hit.text}"
            for index, hit in enumerate(self.hits, 1)
        )


def rrf_fuse(routes: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion：只用排名，免疫两路分数量纲不同。"""
    fused: dict[str, float] = {}
    for route in routes:
        for rank, chunk_id in enumerate(route, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return fused


def version_key(version: str) -> tuple[int, ...]:
    parts = []
    for piece in version.replace("-", ".").split("."):
        parts.append(int(piece) if piece.isdigit() else 0)
    return tuple(parts)


def _newer(a: Candidate, b: Candidate) -> bool:
    return (a.effective_date, version_key(a.version)) > (b.effective_date, version_key(b.version))


def resolve_versions(
    candidates: list[Candidate], *, as_of: date, include_superseded: bool = True
) -> Ranked:
    """同一「文档 + 条款」只保留 as_of 当天有效的最新版本，其余进 superseded。

    去重键是 (doc_key, section_key)，**不是** doc_key，也**不是** section_path。

    · 用 doc_key 会错：同一份办法的 14 条会被压成 1 条，问答时命中的条款被同文档
      的其它条款挤掉，检索看着正常、答案却是错的。
    · 用 section_path 也会错：2026 版在第五章插入了「第 10 条 出差期间加班」，
      导致后面的「报销时限」从第 10 条变成第 11 条。条款号会漂移，条款**标题**不会。

    所以 section_key 取的是去掉「第 N 条」前缀后的标题文本，见 `section_key_of()`。
    """
    best: dict[tuple[str, str], Candidate] = {}
    for item in candidates:
        if item.effective_date > as_of:
            continue  # 未来才生效的版本不参与本次检索
        key = (item.doc_key, item.section_key)
        current = best.get(key)
        if current is None or _newer(item, current):
            best[key] = item

    kept = {item.chunk_id for item in best.values()}
    current = [item for item in candidates if item.chunk_id in kept]
    superseded = (
        [item for item in candidates if item.chunk_id not in kept] if include_superseded else []
    )
    current.sort(key=lambda item: (-item.score, item.chunk_id))
    return Ranked(hits=current, superseded=superseded)


def section_key_of(section_path: str) -> str:
    """把「第 10 条 报销时限」归一成「报销时限」；没有条款号时原样返回。"""
    import re

    last = section_path.split(" > ")[-1].strip()
    return re.sub(r"^第\s*[0-9一二三四五六七八九十百]+\s*条\s*", "", last) or last
