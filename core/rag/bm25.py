"""离线 BM25：不依赖数据库的参考实现。

为什么在已经有 `tsv` 全文索引的情况下还要一份 Python 版：

1. **评测**：`recall@5` 这类指标要在没有 Postgres 的环境（CI 的 lint job、本地无 Docker）
   里也能跑。把"检索质量"和"数据库可用性"解耦，是回归门禁能天跑的前提。
2. **对照**：库里的 `ts_rank` 是 BM25 的变体，归一化方式随语料长度漂移；离线这份是
   教科书口径（k1=1.5, b=0.75），两边排序差异能直接暴露索引配置问题。
3. **零成本**：40 个块全量打分 1 ms 以内，模拟环境没有性能压力。

**它不是线上路径**。线上走 `store.py` 的 SQL 两路召回。这个区别要写清楚，
否则会被误读成"项目其实没用向量库"。
"""

from __future__ import annotations

import math
from collections import Counter

from core.rag.tokenizer import tokenize


class BM25:
    def __init__(self, documents: list[tuple[str, str]], k1: float = 1.5, b: float = 0.75):
        """documents: [(chunk_id, 用于检索的全文)]"""
        self.k1, self.b = k1, b
        self.ids = [doc_id for doc_id, _ in documents]
        self.tokens = [tokenize(text) for _, text in documents]
        self.counts = [Counter(tokens) for tokens in self.tokens]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0

        frequency: Counter = Counter()
        for counts in self.counts:
            frequency.update(counts.keys())
        total = len(documents)
        self.idf = {
            token: math.log(1 + (total - df + 0.5) / (df + 0.5)) for token, df in frequency.items()
        }

    def search(self, query: str, top_k: int = 30) -> list[tuple[str, float]]:
        query_tokens = set(tokenize(query))
        scores: list[tuple[str, float]] = []
        for index, doc_id in enumerate(self.ids):
            score = 0.0
            for token in query_tokens:
                term_frequency = self.counts[index].get(token)
                if not term_frequency:
                    continue
                denominator = term_frequency + self.k1 * (
                    1 - self.b + self.b * self.lengths[index] / self.avg
                )
                score += self.idf[token] * term_frequency * (self.k1 + 1) / denominator
            if score > 0:
                scores.append((doc_id, score))
        scores.sort(key=lambda pair: (-pair[1], pair[0]))
        return scores[:top_k]
