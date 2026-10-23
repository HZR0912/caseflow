"""W3 检索评测：recall@k、MRR、版本冲突命中率、引用可验证率。

设计原则：**默认零外部依赖**。不带参数时用离线索引（哈希向量 + 本地 BM25），
CI 的 lint job 就能跑；带 `--database-url` 时改走真实的 Postgres + 真实 embedding。

三种跑法必须在报告里分开写，否则数字会骗人：

    offline-text      BM25（默认；CI 与本地零依赖口径）
    offline-hybrid    哈希向量 + BM25 + RRF（只测管线，数字**没有**语义含义）
    online-hybrid     真实 embedding + Postgres（测语义召回质量）

用法：
    python -m eval.run_rag_eval                      # 离线全文路（默认，可进 CI）
    python -m eval.run_rag_eval --with-vector        # 把哈希向量也接进融合（只验证管线）
    python -m eval.run_rag_eval --database-url ...   # 线上路径（真实 embedding + Postgres）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.rag.chunking import load_corpus  # noqa: E402

CASES = ROOT / "eval" / "rag_cases.json"
POLICIES = ROOT / "data" / "policies"


def load_cases() -> tuple[date, list[dict]]:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    return date.fromisoformat(payload["as_of"]), payload["cases"]


def evaluate(searcher, as_of: date, cases: list[dict], *, top_k: int = 5) -> dict:
    rows = []
    for case in cases:
        ranked = searcher(case["query"], top_k=top_k, as_of=as_of)
        found = [hit.chunk_id for hit in ranked.hits]
        superseded = [hit.chunk_id for hit in ranked.superseded]
        expect = case["expect"]

        hit = any(chunk_id in found for chunk_id in expect)
        rank = next((index + 1 for index, chunk_id in enumerate(found) if chunk_id in expect), None)
        superseded_ok = None
        if case.get("superseded_expect"):
            superseded_ok = all(chunk_id in superseded for chunk_id in case["superseded_expect"])
        rows.append(
            {
                "id": case["id"],
                "query": case["query"],
                "hit": hit,
                "rank": rank,
                "top1": found[0] if found else None,
                "superseded_ok": superseded_ok,
                "expected": expect,
            }
        )
    return summarise(rows)


def summarise(rows: list[dict]) -> dict:
    total = len(rows)
    hits = sum(row["hit"] for row in rows)
    rr = sum(1.0 / row["rank"] for row in rows if row["rank"])
    versioned = [row for row in rows if row["superseded_ok"] is not None]
    return {
        "n": total,
        "recall": round(hits / total, 4) if total else 0.0,
        "mrr": round(rr / total, 4) if total else 0.0,
        "top1": round(sum(row["rank"] == 1 for row in rows) / total, 4) if total else 0.0,
        "version_conflict": (
            round(sum(bool(row["superseded_ok"]) for row in versioned) / len(versioned), 4)
            if versioned
            else None
        ),
        "rows": rows,
    }


def citation_examples(index) -> dict:
    """用真实制度文本验证引用校验器：真引用通过、编造的引用被拒。"""
    from core.rag.citations import verify_citation

    chunks = list(index.chunks.values())
    target = next(c for c in chunks if "住宿标准" in c.section_path and "2026.1" in c.version)
    # 取一句不含换行、不含列表符号的完整句子——这类句子才是模型会逐字引用的形态
    sentences = [
        piece.strip()
        for piece in target.text.replace("\n", "").replace("- ", "").split("。")
        if len(piece.strip()) >= 12 and "：" not in piece
    ]
    quote = sentences[-1] if sentences else target.text[:20]

    good = verify_citation(target, quote)
    fabricated = verify_citation(target, "一线城市住宿上限 999 元，凭票实报实销")
    truncated = verify_citation(target, "上限")

    return {
        "real_quote": {"status": good.status, "offset": [good.char_start, good.char_end]},
        "fabricated_number": {"status": fabricated.status},
        "too_short": {"status": truncated.status},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="W3 检索评测")
    parser.add_argument("--database-url", default=None, help="给定则走真实 Postgres")
    parser.add_argument(
        "--with-vector",
        action="store_true",
        help="把哈希向量也接进融合（只验证管线，数字无语义含义）",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", default=None, help="把结果写到该路径")
    args = parser.parse_args()

    as_of, cases = load_cases()
    chunks, _ = load_corpus(POLICIES)

    if args.database_url:
        from core.rag.embedder import make_embedder
        from core.rag.store import PolicyStore

        store = PolicyStore(args.database_url, make_embedder())
        store.ingest(chunks)
        mode = "online-hybrid"

        def search(query, *, top_k, as_of):
            return store.search(query, top_k=top_k, as_of=as_of)
    else:
        from core.rag.embedder import HashingEmbedder
        from core.rag.offline import OfflineIndex

        index = OfflineIndex(chunks, HashingEmbedder())
        # 哈希向量是噪声级信号，默认不参与融合（见 OfflineIndex.search 的注释）。
        # --with-vector 只用于验证"融合管线本身能跑"，它的 recall 数字没有语义含义。
        mode = "offline-hybrid(noise)" if args.with_vector else "offline-text"

        def search(query, *, top_k, as_of):
            return index.search(query, top_k=top_k, as_of=as_of, with_vector=args.with_vector)

    report = evaluate(search, as_of, cases, top_k=args.top_k)
    report["mode"] = mode
    report["chunks"] = len(chunks)

    print(f"模式: {mode}   语料块: {len(chunks)}   用例: {report['n']}")
    print(
        f"recall@{args.top_k}: {report['recall']:.3f}   MRR: {report['mrr']:.3f}   "
        f"top1: {report['top1']:.3f}"
    )
    if report["version_conflict"] is not None:
        print(f"版本冲突解析: {report['version_conflict']:.3f}")
    print()
    for row in report["rows"]:
        flag = "✓" if row["hit"] else "✗"
        rank = row["rank"] or "-"
        version = (
            ""
            if row["superseded_ok"] is None
            else ("  版本✓" if row["superseded_ok"] else "  版本✗")
        )
        print(f"  {flag} #{rank:<3} {row['id']:<24}{version}  {row['query']}")
        if not row["hit"]:
            print(f"        期望 {row['expected']}  实际 top1 {row['top1']}")

    from core.rag.offline import OfflineIndex

    if not args.database_url:
        citations = citation_examples(OfflineIndex(chunks, None))
        print(f"\n引用校验: {json.dumps(citations, ensure_ascii=False)}")
        report["citations"] = citations

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n已写入 {args.json}")

    # 门禁：没有版本冲突解析失败、且 recall 不低于 0.75 才算通过
    failures = [row["id"] for row in report["rows"] if not row["hit"]]
    version_failures = [row["id"] for row in report["rows"] if row["superseded_ok"] is False]
    if failures:
        print(f"\n未命中用例: {failures}")
    return 0 if report["recall"] >= 0.75 and not version_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
