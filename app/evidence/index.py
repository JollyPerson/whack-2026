"""Evidence index: chunks, BM25 and optional vectors, merged by rank. Owner: C.

BM25 needs no key or network, so retrieval still works if embeddings don't. Exact flat search is fast enough for
the few thousand chunks a Motion produces. Raw text stays beside each vector so quotes are checked against it.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional, Protocol

import httpx

log = logging.getLogger("evidence.index")

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
RRF_K = 60
_WORD = re.compile(r"[a-z0-9][a-z0-9.,%'-]*")
_STOP = frozenset("a an and are as at be but by for from has have in is it its of on or that the their this to was were will with".split())


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """Local model: no network at query time. Optional, as fastembed has no install path on Python 3.14."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
        self.name = f"fastembed:{model}"
        self.model = TextEmbedding(model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.embed(texts)]


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str = "text-embedding-3-small", timeout: float = 5.0):
        self.name = f"openai:{model}"
        self.model, self.timeout = model, timeout
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 256):
            r = httpx.post("https://api.openai.com/v1/embeddings", headers=self.headers, timeout=self.timeout,
                           json={"model": self.model, "input": texts[i:i + 256]})
            r.raise_for_status()
            out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        return out


def make_embedder() -> Optional[Embedder]:
    """Pick by EVIDENCE_EMBEDDER (fastembed | openai | none). By default: local if installed, then OpenAI, then none."""
    want = os.environ.get("EVIDENCE_EMBEDDER", "auto").lower()
    if want in ("auto", "fastembed"):
        try:
            return FastEmbedEmbedder()
        except Exception as e:
            log.info("fastembed unavailable: %r", e)
    key = os.environ.get("EVIDENCE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if want in ("auto", "openai") and key:
        return OpenAIEmbedder(key)
    return None


def tokens(text: str) -> list[str]:
    return [t.rstrip(".,'-") for t in _WORD.findall(text.lower()) if t not in _STOP]


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):                        # end on a sentence or word so quotes aren't cut mid-way
            stop = text.rfind(". ", start + size // 2, end)
            end = stop + 1 if stop != -1 else max(text.rfind(" ", start + size // 2, end), start + size // 2)
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class EvidenceIndex:
    def __init__(self, chunks: Optional[list[dict]] = None, vectors=None, embedder_name: str = ""):
        self.chunks: list[dict] = chunks or []     # {url, title, text}
        self.vectors = vectors                     # numpy matrix of unit vectors, or None
        self.embedder_name = embedder_name
        self._build_bm25()

    def __len__(self) -> int:
        return len(self.chunks)

    def _build_bm25(self) -> None:
        self._tf = [Counter(tokens(c["text"])) for c in self.chunks]
        self._len = [sum(t.values()) for t in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df = Counter(w for t in self._tf for w in t)
        n = len(self.chunks)
        self._idf = {w: math.log(1 + (n - d + .5) / (d + .5)) for w, d in df.items()}
        self._postings: dict[str, list[int]] = {}
        for i, t in enumerate(self._tf):
            for w in t:
                self._postings.setdefault(w, []).append(i)

    def _bm25_ranked(self, query: str, k1: float = 1.4, b: float = .75) -> list[int]:
        scores: dict[int, float] = {}
        for w in set(tokens(query)):
            idf = self._idf.get(w, 0.0)
            for i in self._postings.get(w, ()):
                tf = self._tf[i][w]
                scores[i] = scores.get(i, 0.0) + idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * self._len[i] / self._avg))
        return sorted(scores, key=lambda i: (-scores[i], i))

    def _vector_ranked(self, query_vector, depth: int) -> list[int]:
        import numpy as np
        q = np.asarray(query_vector, dtype="float32")
        q /= (np.linalg.norm(q) or 1.0)
        return [int(i) for i in np.argsort(-(self.vectors @ q))[:depth]]

    def search(self, query: str, k: int = 5, query_vector=None) -> list[dict]:
        """Top k chunks, with at most two from one page so one long page can't fill the prompt."""
        depth = max(50, k * 10)
        rankings = [self._bm25_ranked(query)[:depth]]
        if query_vector is not None and self.vectors is not None and len(self.chunks):
            rankings.append(self._vector_ranked(query_vector, depth))
        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, i in enumerate(ranking):
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
        out, per_url = [], Counter()
        for i in sorted(fused, key=lambda i: (-fused[i], i)):
            if per_url[self.chunks[i]["url"]] < 2:
                per_url[self.chunks[i]["url"]] += 1
                out.append(self.chunks[i])
            if len(out) == k:
                break
        return out

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".json").write_text(json.dumps({"embedder": self.embedder_name, "chunks": self.chunks}))
        if self.vectors is not None:
            import numpy as np
            np.save(path.with_suffix(".npy"), self.vectors)

    @classmethod
    def load(cls, path: Path) -> "EvidenceIndex":
        data = json.loads(path.with_suffix(".json").read_text())
        vectors = None
        if path.with_suffix(".npy").exists():
            import numpy as np
            vectors = np.load(path.with_suffix(".npy"))
        return cls(data["chunks"], vectors, data.get("embedder", ""))


def build_vectors(chunks: list[dict], embedder: Optional[Embedder]):
    """Unit-length matrix for the chunks. On failure, return None and the index runs on BM25 alone."""
    if embedder is None or not chunks:
        return None
    try:
        import numpy as np
        m = np.asarray(embedder.embed([c["text"] for c in chunks]), dtype="float32")
        return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-9, None)
    except Exception as e:
        log.warning("embedding failed, using BM25 only: %r", e)
        return None
