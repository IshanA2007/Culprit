"""Similar past-incident search — pgvector nearest-neighbor (plan decision 13).

On incident open, Culprit embeds the incident's title+frames text (via the Voyage
REST API — Anthropic's recommended embeddings provider — over httpx, no new SDK
dep) and finds the nearest prior incidents by cosine distance, citing them in the
brief ("similar to incident #N"). ``VOYAGE_API_KEY`` gates the embedder: absent ->
inert and the live tests skip. The nearest-neighbor query itself is pure pgvector
and is testable with synthetic vectors (no key required).
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.models import EMBEDDING_DIM, Incident

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-3.5"  # 1024-dim default (must match EMBEDDING_DIM)


def incident_text(title: str, frames: list[dict]) -> str:
    """The text embedded for similarity: the error title + its stack-frame files."""
    files = sorted({f.get("file") for f in frames if f.get("file")})
    return f"{title}\n{' '.join(files)}".strip()


class VoyageEmbedder:
    """Async Voyage embeddings over httpx. ``enabled`` is False without a key."""

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = VOYAGE_MODEL,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed ``texts`` -> vectors; None when inert (no key) or given nothing."""
        if not self.api_key or not texts:
            return None
        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            for attempt in range(3):  # bounded backoff on 429 (providers rate-limit)
                resp = await client.post(
                    VOYAGE_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "input": texts,
                        "input_type": "document",
                    },
                )
                if resp.status_code == 429 and attempt < 2:
                    wait = float(resp.headers.get("retry-after", 2 * (attempt + 1)))
                    await asyncio.sleep(min(wait, 15.0))  # bounded backoff
                    continue
                resp.raise_for_status()
                data = resp.json()
                return [row["embedding"] for row in data["data"]]
        finally:
            if self._client is None:
                await client.aclose()
        return None  # unreachable: the loop returns or raises


class SimilarIncidentSearch:
    """Embeds an incident and (via ``find_similar``) cites nearest prior incidents."""

    def __init__(self, embedder: VoyageEmbedder):
        self.embedder = embedder

    @property
    def enabled(self) -> bool:
        return self.embedder.enabled

    async def embed_incident(
        self, title: str, frames: list[dict]
    ) -> list[float] | None:
        vecs = await self.embedder.embed([incident_text(title, frames)])
        return vecs[0] if vecs else None


async def find_similar(
    session: AsyncSession,
    embedding: list[float],
    *,
    exclude_id: int,
    limit: int = 3,
    status: str | None = None,
) -> list[dict]:
    """Nearest prior incidents by cosine distance (incidents with an embedding).

    Excludes ``exclude_id`` (an incident never matches itself) and any incident
    without an embedding. ``status`` optionally restricts to e.g. resolved
    incidents; None searches all prior incidents.
    """
    if embedding is None or len(embedding) != EMBEDDING_DIM:
        return []
    dist = Incident.embedding.cosine_distance(embedding).label("dist")
    stmt = (
        select(Incident.id, Incident.correlation_key, dist)
        .where(Incident.embedding.is_not(None), Incident.id != exclude_id)
        .order_by(dist)
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(Incident.status == status)
    rows = (await session.execute(stmt)).all()
    return [
        {"id": r.id, "title": r.correlation_key, "distance": float(r.dist)}
        for r in rows
    ]
