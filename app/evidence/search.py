"""Web search through Exa. Owner: C. Settings come from docs/research/search-and-embeddings.md."""
from __future__ import annotations

import os
from typing import Optional

import httpx

EXA_URL = "https://api.exa.ai/search"


class ExaSearch:
    name = "exa"

    def __init__(self, api_key: str, client: Optional[httpx.AsyncClient] = None, exclude_domains: Optional[list[str]] = None):
        self.client = client or httpx.AsyncClient()
        self.headers = {"x-api-key": api_key}
        self.exclude_domains = exclude_domains or []

    async def search(self, query: str, kind: str = "auto", results: int = 10, max_chars: int = 4000,
                     timeout: float = 10.0, include_domains: Optional[list[str]] = None) -> list[dict]:
        """Pages as {url, title, text}. kind is "auto" for pre-research and "lite" for the live fallback.
        include_domains limits results to those sites (and their subdomains)."""
        body: dict = {"query": query, "type": kind, "numResults": results, "userLocation": "GB",
                      "contents": {"text": {"maxCharacters": max_chars}}}
        if include_domains:
            body["includeDomains"] = include_domains
        elif self.exclude_domains:
            body["excludeDomains"] = self.exclude_domains
        r = await self.client.post(EXA_URL, headers=self.headers, json=body, timeout=timeout)
        r.raise_for_status()
        return [{"url": x["url"], "title": x.get("title") or "", "text": x.get("text") or ""}
                for x in r.json().get("results", []) if x.get("url") and x.get("text")]


def make_search() -> Optional[ExaSearch]:
    key = os.environ.get("EXA_API_KEY")
    if not key:
        return None
    exclude = [d.strip() for d in os.environ.get("EVIDENCE_EXCLUDE_DOMAINS", "").split(",") if d.strip()]
    return ExaSearch(key, exclude_domains=exclude)
