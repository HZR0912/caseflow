"""中文友好的分词，纯本地、零依赖、确定性。

为什么不用 jieba：
- 依赖多一个包、多一份启动开销，而制度文档的检索**靠的是专有名词和数字**
  （「实习生」「90 天」「3000 元」「生产环境」），这些用二元组切分就能精确命中。
- 二元组的失误方向是可预期的：会切出「生理」「产生」这类跨词碎片，导致少量噪声；
  而 jieba 的失误方向是**未知的**（词典外的新词被切碎），更难排查。
  在"检索结果可解释"比"分词更聪明"更重要的场景下，选可预期的那个。

规则：
- 拉丁词与数字整体保留（`Grus japonensis`、`90`、`3000`），拉丁词再做小写化
- 中文按字符二元组切分；单个汉字保留（「市」「省」在制度里是实义词）
- 全角数字与符号先做 NFKC 折叠，避免「３０００」与「3000」检索不到一起
"""

from __future__ import annotations

import re
import unicodedata

CJK = r"一-鿿"
TOKEN = re.compile(rf"[A-Za-z][A-Za-z\-'.]*|\d+(?:\.\d+)?|[{CJK}]+")


def normalize(text: str) -> str:
    """NFKC 折叠全角字符，压缩空白。中文标点保留（它们是分句边界）。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN.finditer(normalize(text)):
        piece = match.group(0)
        if re.fullmatch(rf"[{CJK}]+", piece):
            tokens.extend(
                piece if len(piece) == 1 else (piece[i : i + 2] for i in range(len(piece) - 1))
            )
        else:
            tokens.append(piece.lower())
    return tokens


def to_tsquery_text(tokens: list[str]) -> str:
    """把 token 列表转成 `tsvector` 可读的形态（空格分隔，simple 配置不词干化）。"""
    return " ".join(dict.fromkeys(tokens))
