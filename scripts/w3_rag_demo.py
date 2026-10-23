"""W3 验收脚本：检索 + 版本冲突 + 引用校验，一条命令跑完。

设计成**只依赖语料文件**，不连数据库、不调 API：
W3 的验收点是「检索管线是否可解释、引用是否可验证」，
不是「embedding 模型有多强」。后者用 `python -m eval.run_rag_eval --database-url` 单独测。

用法：
    python -m scripts.w3_rag_demo            # 交互式，逐条打印
    python -m scripts.w3_rag_demo --check    # 只跑断言，退出码 0/1（CI 用）
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rag.chunking import load_corpus  # noqa: E402
from core.rag.citations import verify_citation  # noqa: E402
from core.rag.embedder import HashingEmbedder  # noqa: E402
from core.rag.offline import OfflineIndex  # noqa: E402

POLICIES = ROOT / "data" / "policies"
AS_OF = date(2026, 4, 1)
PROBES = [
    ("实习生账号", "实习生的账号最长多久失效", "access-control@2026.1#003"),
    ("生产权限", "生产环境权限最长多少天", "access-control@2026.1#006"),
    ("住宿标准（版本冲突）", "去上海出差住宿一晚最多能报多少", "travel-expense@2026.1#005"),
    ("报销时限", "出差回来多久之内要报销", "travel-expense@2026.1#010"),
    ("紧急出差", "线上出事来不及提前申请怎么办", "travel-expense@2026.1#004"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不打印细节")
    args = parser.parse_args()

    chunks, docs = load_corpus(POLICIES)
    index = OfflineIndex(chunks, HashingEmbedder())
    failures: list[str] = []

    if not args.check:
        print(f"语料：{len(docs)} 篇文档 / {len(chunks)} 个条款块")
        for doc in docs:
            print(f"  · {doc.title} v{doc.version}（生效 {doc.effective_date}，{doc.source}）")
        print()

    for label, query, expected in PROBES:
        ranked = index.search(query, top_k=5, as_of=AS_OF)
        ids = [hit.chunk_id for hit in ranked.hits]
        ok = expected in ids
        if not ok:
            failures.append(f"{label}: 期望 {expected}，实际 {ids}")
        if args.check:
            continue
        print(f"[{'✓' if ok else '✗'}] {label}  {query}")
        print(f"    检索路由：{ranked.routes}")
        for position, hit in enumerate(ranked.hits[:3], 1):
            print(f"    {position}. {hit.citation}  score={hit.score:.4f}")
        if ranked.superseded:
            print(f"    历史版本（不参与作答）：{[h.chunk_id for h in ranked.superseded]}")
        print()

    # 引用校验：用检索到的第一块做真/假引用对照
    top = index.search("去上海出差住宿一晚最多能报多少", top_k=1, as_of=AS_OF).hits[0]
    sentences = [s for s in top.text.split("。") if len(s) >= 12]
    real_quote = sentences[-1].strip()
    real = verify_citation(top, real_quote)
    fake = verify_citation(top, "一线城市住宿上限 999 元")
    if real.status != "ok":
        failures.append(f"真实引文未通过校验：{real.status}")
    if fake.status == "ok":
        failures.append("编造的数字竟然通过了校验")

    if not args.check:
        print("引用校验：")
        print(f"  真实引文 → {real.status}，原文偏移 [{real.char_start}, {real.char_end}]")
        print(f"  编造数字 → {fake.status}")
        print()

    if failures:
        print("失败项：", *failures, sep="\n  ", file=sys.stderr)
        return 1
    print("W3 验收通过。" if not args.check else "W3 验收通过（check 模式）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
