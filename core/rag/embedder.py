"""向量化：一家线上、一家离线兜底。

为什么必须有离线实现：
评测和 CI 不能依赖外部 API 的可用性与计费。但**检索必须可测**——否则
"向量召回率多少"这类问题就只能在有网、有 key 的机器上回答。

`HashingEmbedder` 是确定性的特征哈希（hashing trick），表达能力远不如真实 embedding，
但有两个性质足够支撑测试：同一个文本永远得到同一个向量；共享 token 的文本余弦相似度更高。
它测的是**管线正确性**（融合、过滤、排序、引用），不是语义质量。语义质量由线上模型
在 `eval/` 里单独测，两者不要混为一谈。

这个区分必须在文档里说清楚，否则容易变成"用哈希假装自己做了向量检索"。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

import httpx

DIM = 1024


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """确定性特征哈希；仅用于离线测试与无 key 时的降级。"""

    name = "hashing"

    def __init__(self, dim: int = DIM):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        from .tokenizer import tokenize

        vector = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class DashScopeEmbedder:
    """text-embedding-v3，兼容 OpenAI 的 /embeddings 端点。"""

    def __init__(
        self, api_key: str, base_url: str, model: str = "text-embedding-v3", timeout: float = 30.0
    ):
        self.name = f"dashscope:{model}"
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._client.post(
            "/embeddings", json={"model": self.model, "input": texts, "dimensions": DIM}
        )
        response.raise_for_status()
        data = response.json()["data"]
        # 服务不保证按输入顺序返回，按 index 显式排序
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]

    def close(self) -> None:
        self._client.close()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def make_embedder(
    api_key: str | None = None, base_url: str | None = None, model: str = "text-embedding-v3"
) -> Embedder:
    """有 key 用线上模型；没有则退到确定性哈希，并在 stderr 明说。

    静默降级是最糟的选择——离线跑出来的 recall 会被误读成语义质量。
    所以这里必须发出声音，且打上 `hashing` 的名字，让报告里看得见。
    """
    import os
    import sys

    key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
    if not key:
        print(
            "[warn] 未配置 DASHSCOPE_API_KEY，向量路降级为确定性哈希（不代表真实语义）",
            file=sys.stderr,
        )
        return HashingEmbedder()
    return DashScopeEmbedder(
        key,
        base_url
        or os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        model=model,
    )
