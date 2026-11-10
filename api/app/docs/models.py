"""The embedding model and the reranker, loaded once per process. Tests and CI use small fakes with the same shape."""

import hashlib
import math
import re

from app.config import settings

DIM = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # bge-small-en-v1.5's query instruction
_embedder = None
_reranker = None


def _words(text: str) -> list[str]:
    return [w[:6] for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2]  # 6-letter prefix: a crude stemmer


class FakeEmbedder:
    """Hashes words into a 384-number vector: texts sharing words point the same way. Good enough for tests."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for word in _words(text):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class FakeReranker:
    def rerank(self, query: str, docs: list[str]) -> list[float]:
        q = set(_words(query))
        return [float(len(q & set(_words(d)))) for d in docs]


class Embedder:
    def __init__(self):
        from fastembed import TextEmbedding

        self.model = TextEmbedding(settings.embed_model, cache_dir=settings.model_cache_dir)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.embed(texts)]


class Reranker:
    def __init__(self):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.model = TextCrossEncoder(settings.rerank_model, cache_dir=settings.model_cache_dir)

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        return list(self.model.rerank(query, docs))


def embedder():
    global _embedder
    if _embedder is None:
        _embedder = FakeEmbedder() if settings.fake_models else Embedder()
    return _embedder


def reranker():
    global _reranker
    if _reranker is None:
        _reranker = FakeReranker() if settings.fake_models else Reranker()
    return _reranker


def embed_query(text: str) -> list[float]:
    return embedder().embed([QUERY_PREFIX + text])[0]


def vector_literal(vec: list[float]) -> str:
    """pgvector reads '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
