"""Task 5 — pgvector similar-incident search (plan decision 13).

Two independently-testable layers:

* The **pgvector nearest-neighbor query** (``find_similar``) is exercised with
  synthetic vectors — deterministic, needs no Voyage key.
* The **Voyage embedding round-trip** is live-gated on ``VOYAGE_API_KEY`` (absent
  -> inert + skip; ask the user for the key before skipping live validation).
"""

from __future__ import annotations

import pytest

from culprit.config import get_settings
from culprit.models import EMBEDDING_DIM, Incident
from culprit.similar import VoyageEmbedder, find_similar, incident_text

VOYAGE = get_settings().voyage_api_key
requires_voyage = pytest.mark.skipif(not VOYAGE, reason="VOYAGE_API_KEY not configured")


def _unit_vec(*hot: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    for i in hot:
        v[i] = 1.0
    return v


def test_embedder_inert_without_key():
    assert VoyageEmbedder(api_key=None).enabled is False


async def test_embedder_embed_returns_none_without_key():
    assert await VoyageEmbedder(api_key=None).embed(["hello"]) is None


def test_incident_text_includes_title_and_frame_files():
    text = incident_text("NoReverseMatch: boom", [{"file": "a.py"}, {"file": "b.py"}])
    assert "NoReverseMatch" in text
    assert "a.py" in text and "b.py" in text


async def test_nearest_neighbor_retrieves_the_closest_incident(db_session):
    near = Incident(status="resolved", correlation_key="A", embedding=_unit_vec(0, 1))
    far = Incident(
        status="resolved", correlation_key="B", embedding=_unit_vec(500, 600)
    )
    current = Incident(status="open", correlation_key="Q", embedding=_unit_vec(0, 1))
    db_session.add_all([near, far, current])
    await db_session.commit()

    matches = await find_similar(
        db_session, _unit_vec(0, 1), exclude_id=current.id, limit=2
    )
    assert matches, "expected at least one neighbor"
    assert matches[0]["id"] == near.id  # the identical-vector incident is closest
    assert current.id not in {m["id"] for m in matches}  # never matches itself


async def test_nearest_neighbor_skips_incidents_without_embedding(db_session):
    no_emb = Incident(status="resolved", correlation_key="none")
    with_emb = Incident(
        status="resolved", correlation_key="has", embedding=_unit_vec(3)
    )
    db_session.add_all([no_emb, with_emb])
    await db_session.commit()
    matches = await find_similar(db_session, _unit_vec(3), exclude_id=-1, limit=5)
    ids = {m["id"] for m in matches}
    assert with_emb.id in ids
    assert no_emb.id not in ids


@requires_voyage
async def test_voyage_embeddings_power_similar_incident_retrieval(db_session):
    # ONE batch embed call for the whole suite (free-tier rate-limit friendly):
    # validates the round-trip width AND that the near-duplicate is retrieved.
    embedder = VoyageEmbedder(VOYAGE)
    prior_text = incident_text("ConnectionError: redis is down", [{"file": "cache.py"}])
    current_text = incident_text(
        "ConnectionError: redis connection refused", [{"file": "cache.py"}]
    )
    far_text = incident_text(
        "NoReverseMatch: instructor_detail route missing", [{"file": "urls.py"}]
    )
    vecs = await embedder.embed([prior_text, current_text, far_text])
    assert vecs and len(vecs) == 3
    assert len(vecs[0]) == EMBEDDING_DIM  # round-trip: correct embedding width

    prior = Incident(
        status="resolved",
        correlation_key="ConnectionError: redis is down",
        embedding=vecs[0],
    )
    far = Incident(
        status="resolved",
        correlation_key="NoReverseMatch: instructor_detail route missing",
        embedding=vecs[2],
    )
    db_session.add_all([prior, far])
    await db_session.commit()

    # the near-duplicate (current) retrieves its sibling ahead of the unrelated one
    matches = await find_similar(db_session, vecs[1], exclude_id=-1, limit=3)
    assert matches and matches[0]["id"] == prior.id
