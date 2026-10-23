"""离线检索引擎：与 `PolicyStore.search` 同一套排序逻辑，但不连数据库。

它存在的唯一理由是让检索**可评测**：CI 里没有 Postgres 也能跑 `eval/run_rag_eval.py`，
从而把"改了切块/分词/融合/版本解析之后有没有变差"变成每次提交都能回答的问题。

与线上路径的差异必须透明，不能假装两者等价：
- 向量路用 `HashingEmbedder`（确定性特征哈希），**不代表真实语义召回率**；
- 全文路用 `core.rag.bm25.BM25`，与库里的 `ts_rank` 是同一族算法但归一化不同；
- 没有 `effective_date <= as_of` 的 SQL 过滤，改由 `resolve_versions` 统一处理
  （线上两边都做，因为 SQL 过滤能减少候选，逻辑结果一致）。

所以评测报告里要把「离线（哈希向量）」与「线上（真实 embedding）」两种跑法分开写，
不能混成一个数字。
"""

from __future__ import annotations

from datetime import date

from core.rag.bm25 import BM25
from core.rag.chunking import Chunk
from core.rag.embedder import Embedder, cosine
from core.rag.ranking import Candidate, Ranked, resolve_versions, rrf_fuse, section_key_of


class OfflineIndex:
    def __init__(self, chunks: list[Chunk], embedder: Embedder | None = None):
        self.chunks = {chunk.chunk_id: chunk for chunk in chunks}
        self.order = [chunk.chunk_id for chunk in chunks]
        self.bm25 = BM25(
            [
                (chunk.chunk_id, f"{chunk.title} {chunk.section_path} {chunk.text}")
                for chunk in chunks
            ]
        )
        self.embedder = embedder
        self.vectors = (
            {
                chunk.chunk_id: vector
                for chunk, vector in zip(
                    chunks,
                    embedder.embed([f"{c.title} {c.section_path}\n{c.text}" for c in chunks]),
                    strict=True,
                )
            }
            if embedder is not None
            else {}
        )

    # ------------------------------------------------------------------
    def _vector_route(self, query: str, recall: int, as_of: date) -> list[str]:
        if not self.vectors:
            return []
        from core.rag.tokenizer import normalize

        query_vector = self.embedder.embed([normalize(query)])[0]
        scored = [
            (chunk_id, cosine(query_vector, vector))
            for chunk_id, vector in self.vectors.items()
            if self.chunks[chunk_id].effective_date <= as_of
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return [chunk_id for chunk_id, _ in scored[:recall]]

    def _text_route(self, query: str, recall: int, as_of: date) -> list[str]:
        hits = self.bm25.search(query, recall * 2)
        return [chunk_id for chunk_id, _ in hits if self.chunks[chunk_id].effective_date <= as_of][
            :recall
        ]

    # ------------------------------------------------------------------
    def _candidate(self, chunk_id: str, score: float) -> Candidate:
        chunk = self.chunks[chunk_id]
        return Candidate(
            chunk_id=chunk_id,
            doc_key=chunk.doc_key,
            version=chunk.version,
            title=chunk.title,
            section_path=chunk.section_path,
            section_key=section_key_of(chunk.section_path),
            text=chunk.text,
            source=chunk.source,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            effective_date=chunk.effective_date,
            superseded_date=chunk.superseded_date,
            score=score,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        recall: int = 30,
        as_of: date | None = None,
        include_superseded: bool = True,
        with_vector: bool = False,
    ) -> Ranked:
        """默认只用全文路。

        ⚠ 这个默认值是实测调出来的，不是随手定的。把哈希向量也接进 RRF 之后，
        12 个用例的 top1 从 0.667 掉到 0.583——因为特征哈希给**每个**候选一个
        0.030~0.033 之间的分数，于是它那条"排名"几乎是随机的，而 RRF 只认排名。
        结果是：一个强信号（BM25 把正确条款排到第 1）被一个噪声信号稀释成了第 2。

        结论：**融合只有在两路都比随机好时才有增益。** 哈希向量不满足这个前提，
        所以默认不参与；要测融合管线本身（比如验证 RRF 与版本解析的组合行为），
        显式传 with_vector=True。真实 embedding 的融合质量由线上评测单独报告。
        """
        as_of = as_of or date.today()
        routes: list[list[str]] = []
        if with_vector:
            vector = self._vector_route(query, recall, as_of)
            if vector:
                routes.append(vector)
        text = self._text_route(query, recall, as_of)
        if text:
            routes.append(text)
        if not routes:
            return Ranked()

        fused = rrf_fuse(routes)
        order = sorted(fused, key=lambda cid: (-fused[cid], cid))
        ranked = resolve_versions(
            [self._candidate(cid, fused[cid]) for cid in order],
            as_of=as_of,
            include_superseded=include_superseded,
        )
        ranked.routes = {f"route{i}": len(route) for i, route in enumerate(routes)}
        ranked.hits = ranked.hits[:top_k]
        return ranked
