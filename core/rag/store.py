"""制度检索：向量 + 全文 → RRF → 版本解析 → 精排。

--------------------------------------------------------------------------
为什么两路都要
--------------------------------------------------------------------------
制度问答的查询有两类，失效方向完全不同：

· 「实习生的账号最长多久失效」——没有专有名词，靠语义。全文路容易只命中
  「实习生」一个词，排序很浅。
· 「第 7 条生产环境权限最长多少天」——高度依赖字面与数字。向量路会把
  「90 天」和「3 个工作日」压到相近位置，全文路不会。

两路失效方向不重叠，融合才有意义。

--------------------------------------------------------------------------
版本解析在排序层，不在 SQL 层
--------------------------------------------------------------------------
放到 `core/rag/ranking.py` 是因为离线评测也要用它。版本冲突的正确性是 W3 最该被
回归覆盖的部分，如果它只活在 SQL 路径里，CI 里那 104 项单测就覆盖不到它。

--------------------------------------------------------------------------
RRF 而不是分数加权
--------------------------------------------------------------------------
余弦相似度有界在 [-1, 1]，`ts_rank` 无界且随语料变化，两者不可比；归一化系数换语料
就要重调。RRF 只用排名：Σ 1/(k + rank)，k=60。
"""

from __future__ import annotations

from datetime import date

import psycopg

from core.rag.chunking import Chunk
from core.rag.embedder import Embedder
from core.rag.ranking import Candidate, Ranked, resolve_versions, rrf_fuse, section_key_of
from core.rag.tokenizer import normalize, to_tsquery_text, tokenize


class PolicyStore:
    def __init__(
        self, database_url: str, embedder: Embedder | None = None, table: str = "policy_chunks"
    ):
        self.url = database_url
        self.embedder = embedder
        self.table = table

    # ------------------------------------------------------------------
    def _connect(self):
        conn = psycopg.connect(self.url, connect_timeout=5)
        try:
            from pgvector.psycopg import register_vector

            register_vector(conn)
        except Exception:
            pass
        return conn

    # ------------------------------------------------------------------
    def ingest(self, chunks: list[Chunk], *, batch: int = 32, embed: bool = True) -> int:
        """幂等写入（同 chunk_id 覆盖）。返回写入条数。"""
        vectors: list[list[float] | None] = [None] * len(chunks)
        if embed and chunks and self.embedder is not None:
            for start in range(0, len(chunks), batch):
                window = chunks[start : start + batch]
                texts = [f"{c.title} {c.section_path}\n{c.text}" for c in window]
                vectors[start : start + batch] = self.embedder.embed(texts)

        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(
                f"""
                    INSERT INTO {self.table}
                        (chunk_id, doc_key, version, title, section_path, section_key, text,
                         char_start, char_end, effective_date, superseded_date, source,
                         tokens, embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        section_path = EXCLUDED.section_path,
                        section_key = EXCLUDED.section_key,
                        text = EXCLUDED.text,
                        tokens = EXCLUDED.tokens,
                        embedding = COALESCE(EXCLUDED.embedding, {self.table}.embedding),
                        effective_date = EXCLUDED.effective_date,
                        superseded_date = EXCLUDED.superseded_date
                    """,
                [
                    (
                        c.chunk_id,
                        c.doc_key,
                        c.version,
                        c.title,
                        c.section_path,
                        section_key_of(c.section_path),
                        c.text,
                        c.char_start,
                        c.char_end,
                        c.effective_date,
                        c.superseded_date,
                        c.source,
                        to_tsquery_text(tokenize(f"{c.title} {c.section_path} {c.text}")),
                        vectors[i],
                    )
                    for i, c in enumerate(chunks)
                ],
            )
        return len(chunks)

    # ------------------------------------------------------------------
    def _vector_route(self, query: str, recall: int, as_of: date) -> list[str]:
        if self.embedder is None:
            return []
        vector = self.embedder.embed([normalize(query)])[0]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT chunk_id FROM {self.table}
                WHERE embedding IS NOT NULL AND effective_date <= %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (as_of, vector, recall),
            ).fetchall()
        return [row[0] for row in rows]

    def _text_route(self, query: str, recall: int, as_of: date) -> list[str]:
        tokens = tokenize(query)
        if not tokens:
            return []
        query_text = to_tsquery_text(tokens)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT chunk_id FROM {self.table}
                WHERE tsv @@ plainto_tsquery('simple', %s) AND effective_date <= %s
                ORDER BY ts_rank(tsv, plainto_tsquery('simple', %s)) DESC
                LIMIT %s
                """,
                (query_text, as_of, query_text, recall),
            ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        recall: int = 30,
        as_of: date | None = None,
        include_superseded: bool = True,
    ) -> Ranked:
        as_of = as_of or date.today()
        routes: list[list[str]] = []
        for route in (self._vector_route, self._text_route):
            try:
                found = route(query, recall, as_of)
            except Exception:
                found = []
            if found:
                routes.append(found)
        if not routes:
            return Ranked()

        fused = rrf_fuse(routes)
        order = sorted(fused, key=lambda cid: (-fused[cid], cid))
        candidates = self._load(order, fused)
        ranked = resolve_versions(candidates, as_of=as_of, include_superseded=include_superseded)
        ranked.routes = {f"route{i}": len(route) for i, route in enumerate(routes)}
        ranked.hits = ranked.hits[:top_k]
        return ranked

    # ------------------------------------------------------------------
    def _load(self, chunk_ids: list[str], scores: dict[str, float]) -> list[Candidate]:
        if not chunk_ids:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT chunk_id, doc_key, version, title, section_path, section_key, text,
                       source, char_start, char_end, effective_date, superseded_date
                FROM {self.table} WHERE chunk_id = ANY(%s)
                """,
                (list(chunk_ids),),
            ).fetchall()
        by_id = {row[0]: row for row in rows}
        return [
            Candidate(
                chunk_id=cid,
                doc_key=row[1],
                version=row[2],
                title=row[3],
                section_path=row[4],
                section_key=row[5],
                text=row[6],
                source=row[7],
                char_start=row[8],
                char_end=row[9],
                effective_date=row[10],
                superseded_date=row[11],
                score=round(scores[cid], 6),
            )
            for cid in chunk_ids
            if (row := by_id.get(cid)) is not None
        ]
