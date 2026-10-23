"""W3 检索管线：切块、分词、融合、版本解析、引用校验。

这组测试**不需要 Postgres、不需要 API key**，因此能进 CI 的 lint job，
每次提交都跑。线上路径（`PolicyStore`）的正确性另由集成测试覆盖。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from core.rag.bm25 import BM25
from core.rag.chunking import load_corpus, parse_document
from core.rag.citations import check_answer, verify_citation
from core.rag.embedder import HashingEmbedder, cosine
from core.rag.offline import OfflineIndex
from core.rag.ranking import Candidate, resolve_versions, rrf_fuse, section_key_of
from core.rag.tokenizer import normalize, to_tsquery_text, tokenize

POLICIES = Path(__file__).resolve().parents[1] / "data" / "policies"
AS_OF = date(2026, 4, 1)
EARLY = date(2025, 6, 1)


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(POLICIES)


# ---------------------------------------------------------------- chunking
def test_chunk_slices_reproduce_source_exactly(corpus):
    """char_start/char_end 必须能逐字还原——这是引用校验的地基。"""
    chunks, docs = corpus
    by_doc = {(doc.doc_key, doc.version): doc for doc in docs}
    for chunk in chunks:
        doc = by_doc[chunk.doc_key, chunk.version]
        assert doc.body[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.text.strip() == chunk.text


def test_chunks_are_clause_level_not_article_level(corpus):
    chunks, _ = corpus
    travel = [c for c in chunks if c.doc_key == "travel-expense"]
    # 两个版本 × 十余条；如果退回按章切，总数会掉到个位数
    assert len(travel) > 20
    assert all(c.section_path.startswith("第") for c in travel)


def test_document_requires_front_matter(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("# 没有 front matter\n", encoding="utf-8")
    with pytest.raises(ValueError, match="front matter"):
        parse_document(path)


def test_document_requires_fields(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("---\ndoc_key: x\n---\n正文\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少字段"):
        parse_document(path)


def test_version_is_per_chunk(corpus):
    chunks, _ = corpus
    versions = {c.version for c in chunks if c.doc_key == "travel-expense"}
    assert versions == {"2025.2", "2026.1"}


# --------------------------------------------------------------- tokenizer
def test_tokenizer_is_deterministic():
    text = "实习生的账号最长多久失效？90 天"
    assert tokenize(text) == tokenize(text)
    assert "90" in tokenize(text)


def test_tokenizer_keeps_latin_and_numbers_whole():
    tokens = tokenize("Grus japonensis 与 3000 元")
    assert "grus" in tokens and "japonensis" in tokens and "3000" in tokens


def test_tokenizer_folds_fullwidth_digits():
    assert tokenize("３０００") == tokenize("3000")


def test_normalize_collapses_whitespace():
    assert normalize("a  \n b") == "a b"


def test_tsquery_text_dedupes_in_order():
    assert to_tsquery_text(["a", "b", "a"]) == "a b"


# ------------------------------------------------------------------- bm25
def test_bm25_ranks_exact_term_first(corpus):
    chunks, _ = corpus
    index = BM25([(c.chunk_id, f"{c.title} {c.section_path} {c.text}") for c in chunks])
    hits = index.search("实习生 账号 有效期", 5)
    assert hits[0][0].startswith("access-control")
    assert "003" in hits[0][0]


def test_bm25_returns_nothing_for_empty_query(corpus):
    chunks, _ = corpus
    index = BM25([(c.chunk_id, c.text) for c in chunks])
    assert index.search("") == []


# -------------------------------------------------------------------- rrf
def test_rrf_rewards_agreement():
    """两路都排第一的 a 得分最高；b、c 互换第 2/第 3，得分相同（RRF 只看名次）。"""
    fused = rrf_fuse([["a", "b", "c"], ["a", "c", "b"]])
    assert fused["a"] > fused["b"]
    assert fused["b"] == pytest.approx(fused["c"])
    # a 在两路都是 #1，因此等于两倍的单路 #1 得分
    assert fused["a"] == pytest.approx(2 / 61)


def test_rrf_ignores_score_scale():
    """RRF 只看排名：两个量纲完全不同的路照样能融合。"""
    assert rrf_fuse([["x"], ["x"]])["x"] == pytest.approx(2 / 61)


# --------------------------------------------------------- version resolve
def _candidate(chunk_id, doc_key, version, path, effective, score=1.0):
    return Candidate(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version=version,
        title=doc_key,
        section_path=path,
        section_key=section_key_of(path),
        text="",
        source="",
        char_start=0,
        char_end=0,
        effective_date=effective,
        score=score,
    )


def test_resolve_keeps_newest_version_of_same_clause():
    items = [
        _candidate("t@2025#005", "t", "2025.2", "第 6 条 住宿标准", date(2025, 3, 1)),
        _candidate("t@2026#005", "t", "2026.1", "第 6 条 住宿标准", date(2026, 1, 1)),
    ]
    ranked = resolve_versions(items, as_of=AS_OF)
    assert [h.chunk_id for h in ranked.hits] == ["t@2026#005"]
    assert [h.chunk_id for h in ranked.superseded] == ["t@2025#005"]


def test_resolve_follows_as_of_date():
    items = [
        _candidate("t@2025#005", "t", "2025.2", "第 6 条 住宿标准", date(2025, 3, 1)),
        _candidate("t@2026#005", "t", "2026.1", "第 6 条 住宿标准", date(2026, 1, 1)),
    ]
    ranked = resolve_versions(items, as_of=EARLY)
    assert [h.chunk_id for h in ranked.hits] == ["t@2025#005"]


def test_resolve_does_not_collapse_different_clauses():
    """回归：用 doc_key 去重的初版会把同文档的其它条款一起压掉。"""
    items = [
        _candidate("t@2026#003", "t", "2026.1", "第 4 条 审批层级", date(2026, 1, 1)),
        _candidate("t@2026#010", "t", "2026.1", "第 11 条 报销时限", date(2026, 1, 1)),
    ]
    ranked = resolve_versions(items, as_of=AS_OF)
    assert len(ranked.hits) == 2


def test_resolve_survives_clause_renumbering():
    """回归：2026 版插入新条款后，报销时限从第 10 条变成第 11 条。

    用 section_path 当对齐键会把两版当成两条不同规定，同时留在结果里，
    于是模型会把 15 个工作日和 30 个工作日都说一遍。
    """
    items = [
        _candidate("t@2025#009", "t", "2025.2", "第 10 条 报销时限", date(2025, 3, 1)),
        _candidate("t@2026#010", "t", "2026.1", "第 11 条 报销时限", date(2026, 1, 1)),
    ]
    ranked = resolve_versions(items, as_of=AS_OF)
    assert [h.chunk_id for h in ranked.hits] == ["t@2026#010"]
    assert ranked.superseded[0].chunk_id == "t@2025#009"


def test_resolve_excludes_future_versions():
    items = [_candidate("t@2027#001", "t", "2027.1", "第 1 条 目的", date(2027, 1, 1))]
    assert resolve_versions(items, as_of=AS_OF).hits == []


def test_section_key_strips_clause_number():
    assert section_key_of("第 12 条 操作留痕") == "操作留痕"
    assert section_key_of("第五章 报销") == "第五章 报销"


# ---------------------------------------------------------------- embedder
def test_hashing_embedder_is_deterministic_and_normalised():
    same = HashingEmbedder().embed(["住宿标准 600 元"])[0]
    assert same == HashingEmbedder().embed(["住宿标准 600 元"])[0]
    assert abs(sum(v * v for v in same) - 1.0) < 1e-6


def test_hashing_embedder_prefers_shared_tokens():
    embedder = HashingEmbedder()
    query, close, far = embedder.embed(
        ["住宿标准 上限", "住宿标准 上限 600 元", "离职当天账号停用"]
    )
    assert cosine(query, close) > cosine(query, far)


# ----------------------------------------------------------------- offline
def test_offline_index_finds_expected_chunk(corpus):
    chunks, _ = corpus
    index = OfflineIndex(chunks, HashingEmbedder())
    ranked = index.search("生产环境权限最长多少天", top_k=5, as_of=AS_OF)
    assert "access-control@2026.1#006" in [h.chunk_id for h in ranked.hits]


def test_offline_index_version_conflict_goes_to_superseded(corpus):
    chunks, _ = corpus
    index = OfflineIndex(chunks, HashingEmbedder())
    ranked = index.search("去上海出差住宿一晚最多能报多少", top_k=5, as_of=AS_OF)
    ids = [h.chunk_id for h in ranked.hits]
    assert "travel-expense@2026.1#005" in ids
    assert "travel-expense@2026.1#005" not in [
        h.chunk_id for h in ranked.superseded if h.version == "2026.1"
    ]


def test_offline_index_text_only_still_works(corpus):
    chunks, _ = corpus
    ranked = OfflineIndex(chunks, None).search("报销时限", top_k=5, as_of=AS_OF)
    assert ranked.hits
    assert all("2026.1" in h.chunk_id for h in ranked.hits)


# ---------------------------------------------------------------- citations
@pytest.fixture(scope="module")
def lodging_chunk(corpus):
    chunks, _ = corpus
    return next(c for c in chunks if c.chunk_id == "travel-expense@2026.1#005")


def test_real_quote_verifies_and_returns_offsets(lodging_chunk):
    quote = "超出部分由员工自理，特殊情况须事前书面说明并经部门负责人批准"
    verdict = verify_citation(lodging_chunk, quote)
    assert verdict.status == "ok"
    assert (
        lodging_chunk.text[
            verdict.char_start - lodging_chunk.char_start : verdict.char_end
            - lodging_chunk.char_start
        ]
        == quote
    )


def test_fabricated_number_is_rejected(lodging_chunk):
    verdict = verify_citation(lodging_chunk, "一线城市住宿上限 999 元，凭票实报实销")
    assert verdict.status == "quote_not_found"


def test_whitespace_and_punctuation_width_tolerated(lodging_chunk):
    """全角括号/冒号/数字混用、多余空格与换行不应导致误杀。

    注意这里**只改宽度不改字符**：顿号「、」不是半角逗号的宽度变体，
    把它换成「,」属于改写语义，校验器应当拒绝。见下一个用例。
    """
    quote = "一线城市(北京、上海、广州、深圳): 上限 600 元\n"
    assert verify_citation(lodging_chunk, quote).status == "ok"


def test_reworded_punctuation_is_not_accepted(lodging_chunk):
    """把顿号换成逗号是改写，不是排版差异——必须判为找不到。"""
    quote = "一线城市（北京,上海,广州,深圳）：上限 600 元。"
    assert verify_citation(lodging_chunk, quote).status == "quote_not_found"


def test_short_quote_is_not_a_citation(lodging_chunk):
    assert verify_citation(lodging_chunk, "上限").status == "quote_too_short"


def test_check_answer_rejects_unknown_index(lodging_chunk):
    report = check_answer("按 [1] 规定，另见 [9]。", {1: lodging_chunk})
    assert not report.ok
    assert [v.status for v in report.verdicts] == ["ok", "unknown_chunk"]


def test_check_answer_checks_every_quote(lodging_chunk):
    report = check_answer(
        "[1][1]",
        {1: lodging_chunk},
        {1: "一线城市（北京、上海、广州、深圳）：上限 600 元"},
    )
    assert report.ok and report.valid_ratio == 1.0
