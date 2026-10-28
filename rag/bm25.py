"""最小可用的 BM25 实现。

题库只有几百到几千道题，没必要引入 Elasticsearch 或向量数据库。
这里用 Okapi BM25，纯 Python，构建索引在 100ms 量级。

公式：
    score(q, d) = Σ idf(t) * tf(t,d) * (k1 + 1)
                  / (tf(t,d) + k1 * (1 - b + b * |d| / avgdl))

    idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
"""

from __future__ import annotations

import math
from collections import Counter


class BM25:
    def __init__(
        self,
        tokenized_docs: list[list[str]],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.doc_count = len(tokenized_docs)
        self.doc_lengths = [len(tokens) for tokens in tokenized_docs]
        self.avg_length = (
            sum(self.doc_lengths) / self.doc_count if self.doc_count else 0.0
        )

        self.term_freqs: list[Counter[str]] = []
        doc_freq: Counter[str] = Counter()
        for tokens in tokenized_docs:
            tf = Counter(tokens)
            self.term_freqs.append(tf)
            doc_freq.update(tf.keys())

        self.idf = {
            term: math.log(1 + (self.doc_count - df + 0.5) / (df + 0.5))
            for term, df in doc_freq.items()
        }

    def score(self, query_tokens: list[str], doc_index: int) -> float:
        if not query_tokens or self.doc_count == 0:
            return 0.0

        tf = self.term_freqs[doc_index]
        length = self.doc_lengths[doc_index] or 1
        total = 0.0
        for term in query_tokens:
            freq = tf.get(term, 0)
            if not freq:
                continue
            idf = self.idf.get(term, 0.0)
            denominator = freq + self.k1 * (
                1 - self.b + self.b * length / self.avg_length
            )
            total += idf * freq * (self.k1 + 1) / denominator
        return total

    def scores(self, query_tokens: list[str]) -> list[float]:
        return [self.score(query_tokens, i) for i in range(self.doc_count)]
