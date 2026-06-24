"""
Optional neural semantic embeddings for wiki retrieval.

The default retriever (wiki/retrieval.py) is TF-IDF/cosine — lexical, zero-config,
no network. This module adds a *semantic* backend that understands meaning
(synonyms, paraphrase) by calling an OpenAI-compatible `/embeddings` endpoint.
It is OPT-IN and provider-agnostic: set three env vars and it activates; leave
them unset and retrieval transparently stays on TF-IDF.

    WIKI_EMBED_BASE_URL   e.g. https://api.openai.com/v1  (any OpenAI-compatible)
    WIKI_EMBED_API_KEY    the key for that endpoint
    WIKI_EMBED_MODEL      e.g. text-embedding-3-small / bge-m3 / …

Doc vectors are cached on disk per domain (keyed by content hash), so a page is
embedded once and reused across queries until its content changes — each query
then costs a single embedding call (the question). `SemanticIndex` mirrors
`VectorIndex.build()/search()` exactly, so it is a drop-in; callers fall back to
TF-IDF on any failure (missing lib, network error, bad config).
"""

import os
import json
import math
import hashlib
from pathlib import Path
from typing import Callable, Optional

EMBED_BATCH = 64          # texts per embeddings request
EMBED_TIMEOUT = 60.0


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return ""


def embed_base_url() -> str:
    return _env("WIKI_EMBED_BASE_URL", "EMBED_BASE_URL")


def embed_api_key() -> str:
    return _env("WIKI_EMBED_API_KEY", "EMBED_API_KEY")


def embed_model() -> str:
    return _env("WIKI_EMBED_MODEL", "EMBED_MODEL")


def embeddings_enabled() -> bool:
    """True only when a base URL, key and model are all configured."""
    return bool(embed_base_url() and embed_api_key() and embed_model())


def embed_texts(texts: list, timeout: float = EMBED_TIMEOUT) -> list:
    """Embed a list of texts via an OpenAI-compatible /embeddings endpoint,
    batched. Returns a list of float vectors aligned with `texts`."""
    import httpx
    base = embed_base_url().rstrip("/")
    url = base + "/embeddings"
    headers = {
        "Authorization": f"Bearer {embed_api_key()}",
        "Content-Type": "application/json",
    }
    model = embed_model()
    out: list = []
    with httpx.Client(timeout=timeout) as client:
        for i in range(0, len(texts), EMBED_BATCH):
            chunk = texts[i:i + EMBED_BATCH]
            resp = client.post(url, headers=headers,
                               json={"model": model, "input": chunk})
            resp.raise_for_status()
            data = resp.json()["data"]
            data.sort(key=lambda d: d.get("index", 0))   # preserve input order
            out.extend(d["embedding"] for d in data)
    return out


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _norm(vec: list) -> float:
    return math.sqrt(sum(x * x for x in vec)) or 1.0


class SemanticIndex:
    """Dense-vector cosine index. Same surface as retrieval.VectorIndex."""

    def __init__(self, embed_fn: Optional[Callable] = None):
        self.embed_fn = embed_fn or embed_texts
        self.paths: list = []
        self._vecs: list = []
        self._norms: list = []

    @classmethod
    def build(cls, docs: dict, cache_path: Optional[str] = None,
              embed_fn: Optional[Callable] = None) -> "SemanticIndex":
        idx = cls(embed_fn)
        idx.paths = list(docs.keys())
        hashes = [_hash(docs[p]) for p in idx.paths]

        cache: dict = {}
        if cache_path and Path(cache_path).is_file():
            try:
                cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cache = {}

        # Embed only the docs whose content hash isn't cached yet.
        missing = [(p, h) for p, h in zip(idx.paths, hashes) if h not in cache]
        if missing:
            vecs = idx.embed_fn([docs[p] for p, _ in missing])
            for (_, h), v in zip(missing, vecs):
                cache[h] = v
            if cache_path:
                # Prune to the current set so the cache can't grow without bound.
                cache = {h: cache[h] for h in set(hashes) if h in cache}
                try:
                    Path(cache_path).write_text(
                        json.dumps(cache), encoding="utf-8")
                except OSError:
                    pass

        for h in hashes:
            v = cache.get(h) or []
            idx._vecs.append(v)
            idx._norms.append(_norm(v) if v else 1.0)
        return idx

    def search(self, query: str, top_k: int = 6,
               min_score: float = 0.0) -> list:
        qv = self.embed_fn([query])[0]
        qn = _norm(qv)
        scored = []
        for i, path in enumerate(self.paths):
            v = self._vecs[i]
            if not v:
                continue
            dot = sum(a * b for a, b in zip(qv, v))
            score = dot / (qn * self._norms[i])
            if score > min_score:
                scored.append((score, path))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(p, s) for s, p in scored[:top_k]]
