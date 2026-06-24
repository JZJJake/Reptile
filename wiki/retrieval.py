"""
Vector-space retrieval for the wiki — CJK-aware TF-IDF + cosine.

The pipeline's page-selection used to be single-keyword overlap counting, which
at scale picks too many weakly-related pages and misses the strongly-related
ones (no term weighting, no length normalization). This module replaces that
with a proper sparse vector retriever:

  - CJK-aware tokenization: ASCII words + Chinese character bigrams, so Chinese
    (which is not space-delimited) is matched on 2-char shingles, not whole
    sentences.
  - TF-IDF weighting: rare, discriminative terms dominate; ubiquitous ones are
    damped — the core of "semantic-ish" relevance without a neural model.
  - Cosine similarity with length normalization: a long synthesis page no longer
    outranks a focused entity page just by being long.

It is dependency-free and deterministic (offline-testable). The `VectorIndex`
interface is intentionally narrow (`build(docs)` / `search(query)`), so a neural
embedding backend can be dropped in later behind the same surface without
touching callers — the TF-IDF backend is the zero-config default that works with
no extra dependency and no embeddings API.
"""

import re
import math
from collections import Counter

_ASCII_RE = re.compile(r'[a-z0-9]+')


def tokenize(text: str) -> list[str]:
    """Tokens for the vector space: lowercased ASCII words (len ≥ 2) + Chinese
    character bigrams. Bigrams give Chinese useful precision (single chars are
    too ambiguous; whole sentences never match)."""
    if not text:
        return []
    lower = text.lower()
    toks = [w for w in _ASCII_RE.findall(lower) if len(w) >= 2]
    cjk = [ch for ch in text if '一' <= ch <= '鿿']
    toks.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return toks


def _weighted_vec(tf: Counter, idf: dict) -> dict:
    """Sublinear-TF × IDF sparse vector."""
    return {t: (1.0 + math.log(c)) * idf.get(t, 0.0) for t, c in tf.items()}


def _norm(vec: dict) -> float:
    return math.sqrt(sum(v * v for v in vec.values())) or 1.0


class VectorIndex:
    """In-memory TF-IDF cosine index over a {path: text} mapping.

    Built per query from the wiki files (small, and no worse than the previous
    keyword scorer which also read every page). Callers can cache by file mtime
    if a domain grows large enough to matter."""

    def __init__(self):
        self.paths: list[str] = []
        self.idf: dict = {}
        self._vecs: list[dict] = []
        self._norms: list[float] = []

    @classmethod
    def build(cls, docs: dict) -> "VectorIndex":
        idx = cls()
        idx.paths = list(docs.keys())
        tfs = [Counter(tokenize(docs[p])) for p in idx.paths]
        df: Counter = Counter()
        for tf in tfs:
            df.update(tf.keys())
        n = max(1, len(idx.paths))
        # Smoothed IDF (never zero, so a term present everywhere still counts a little).
        idx.idf = {t: math.log((n + 1) / (d + 1)) + 1.0 for t, d in df.items()}
        for tf in tfs:
            vec = _weighted_vec(tf, idx.idf)
            idx._vecs.append(vec)
            idx._norms.append(_norm(vec))
        return idx

    def search(self, query: str, top_k: int = 6, min_score: float = 0.0
               ) -> list[tuple[str, float]]:
        """Return [(path, cosine_score)] for the top_k most relevant docs."""
        qvec = _weighted_vec(Counter(tokenize(query)), self.idf)
        if not qvec:
            return []
        qnorm = _norm(qvec)
        scored: list[tuple[float, str]] = []
        for i, path in enumerate(self.paths):
            dvec = self._vecs[i]
            # Iterate the smaller dict for the dot product.
            if len(qvec) <= len(dvec):
                dot = sum(w * dvec.get(t, 0.0) for t, w in qvec.items())
            else:
                dot = sum(w * qvec.get(t, 0.0) for t, w in dvec.items())
            if dot <= 0:
                continue
            score = dot / (qnorm * self._norms[i])
            if score > min_score:
                scored.append((score, path))
        # Sort by score desc, then path asc for deterministic ties.
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(p, s) for s, p in scored[:top_k]]
