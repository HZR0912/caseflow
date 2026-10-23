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

    ------------------------------------------------------------------
    三类候选的处理必须分开，混在一起会出错
    ------------------------------------------------------------------
    · 已废止（superseded_date < as_of）：**不能**作为现行规定返回。用户问"差旅花
      2000 元谁批"，如果只召回了 2025 版那一条（2000 元是它的门槛），把它当现行
      规定答出来就是错的——应当进 superseded，并在回答里说明"该口径已于某日废止"。
    · 未来生效（effective_date > as_of）：既不是现行也不是历史，直接丢弃。
      否则它会以"历史版本"的身份出现在 superseded 里，反而制造误导。
    · 现行：按 (doc_key, section_key) 取最新版本，其余进 superseded。
    """
    best: dict[tuple[str, str], Candidate] = {}
    current_ids: set[str] = set()
    displaced: list[Candidate] = []  # 同条款的旧版本（被新版本顶掉）
    expired: list[Candidate] = []  # 在 as_of 当天已整体废止
    for item in candidates:
        if item.effective_date > as_of:
            continue  # 未来版本：不参与、也不进 superseded
        if item.superseded_date is not None and item.superseded_date < as_of:
            expired.append(item)
            continue
        key = (item.doc_key, item.section_key)
        current = best.get(key)
        if current is None:
            best[key] = item
            current_ids.add(item.chunk_id)
        elif _newer(item, current):
            current_ids.discard(current.chunk_id)
            displaced.append(current)
            best[key] = item
            current_ids.add(item.chunk_id)
        else:
            displaced.append(item)

    current = [item for item in candidates if item.chunk_id in current_ids]
    current.sort(key=lambda item: (-item.score, item.chunk_id))
    superseded = [*displaced, *expired] if include_superseded else []
    superseded.sort(key=lambda item: (-item.score, item.chunk_id))
    return Ranked(hits=current, superseded=superseded)


def align_superseded(hits: list[Candidate], superseded: list[Candidate]) -> list[Candidate]:
    """只保留"与被返回的现行条款属于同一条"的历史版本。

    为什么必须做：superseded 来自 recall 阶段的全部候选（默认 30 条），
    而 hits 只取 top_k。不做对齐的话，top_k=1 时可能带回十几条与 top1 毫无关系的
    旧条款——前端展示「另见历史版本」时会列出一堆无关内容，模型也可能拿它们
    去拼答案。历史版本的价值是"同一件事以前怎么规定的"，不是"这次检索淘汰了谁"。
    """
    keys = {(hit.doc_key, hit.section_key) for hit in hits}
    return [item for item in superseded if (item.doc_key, item.section_key) in keys]


def section_key_of(section_path: str) -> str:
    """把「第 10 条 报销时限」归一成「报销时限」；没有条款号时原样返回。"""
    import re

    last = section_path.split(" > ")[-1].strip()
    return re.sub(r"^第\s*[0-9一二三四五六七八九十百]+\s*条\s*", "", last) or last
