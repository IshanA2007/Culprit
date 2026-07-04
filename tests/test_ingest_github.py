"""Task 4 — GitHub deploy ingest: workflow_run -> Deploy timeline.

The deploy feed is NOT a trigger (HANDOFF §4); it keeps the SHA+timestamp per
deploy current so the window can be reconstructed later. Every run's deploy
``head_sha`` IS its ``release_sha`` (the verified correlation join).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from culprit.config import REPO_ROOT, get_settings
from culprit.ingest.github import ingest_github, parse_github
from culprit.models import Deploy
from harness.runrecord import load_all_run_records

DEPLOY_DIR = REPO_ROOT / "fixtures" / "github" / "workflow_run"
RUNS = load_all_run_records()

GH_SECRET = get_settings().culprit_gh_webhook_secret
requires_secret = pytest.mark.skipif(
    not GH_SECRET, reason="CULPRIT_GH_WEBHOOK_SECRET not configured"
)


def _fixtures() -> list[Path]:
    """The release deploy fixtures (``run.deploy``) — this module is about the
    release timeline. The M4 rollback fix-deploys (``run.fix_deploy``) also live in
    workflow_run/ but ship base_sha, not release_sha, so they are excluded here."""
    return sorted(REPO_ROOT / r.deploy for r in RUNS if r.deploy)


def _raw(path: Path) -> bytes:
    return json.loads(path.read_text())["raw_body"].encode("latin-1")


def _sig(path: Path) -> str:
    return json.loads(path.read_text())["headers"]["x-hub-signature-256"]


# --- parse (pure) ----------------------------------------------------------


def test_parse_workflow_run_extracts_fields():
    parsed = parse_github(json.loads(_raw(_fixtures()[0])))
    assert parsed is not None
    assert len(parsed.head_sha) == 40
    assert parsed.branch == "master"
    assert parsed.conclusion == "success"
    assert isinstance(parsed.run_started_at, datetime)


def test_parse_ignores_non_deploy_workflow():
    body = json.loads(_raw(_fixtures()[0]))
    body["workflow_run"]["name"] = "Continuous Integration"
    assert parse_github(body) is None


def test_every_run_deploy_head_sha_equals_release_sha():
    """The ingest contract: workflow_run.head_sha == the run's release_sha."""
    assert RUNS
    for run in RUNS:
        assert run.deploy, f"{run.run_id}: no deploy linked"
        body = json.loads((REPO_ROOT / run.deploy).read_text())
        payload = json.loads(body["raw_body"].encode("latin-1"))
        parsed = parse_github(payload)
        assert parsed is not None
        assert parsed.head_sha == run.release_sha, run.run_id


# --- persist ---------------------------------------------------------------


async def test_replay_all_fixtures_creates_one_deploy_each(db_session):
    fixtures = _fixtures()
    assert len(fixtures) == 22
    for path in fixtures:
        deploy = await ingest_github(db_session, _raw(path))
        assert deploy is not None
    release_shas = {r.release_sha for r in RUNS}
    distinct_heads = {parse_github(json.loads(_raw(p))).head_sha for p in fixtures}
    rows = (await db_session.execute(select(Deploy))).scalars().all()
    assert len(rows) == len(distinct_heads)
    for d in rows:
        assert d.head_sha in release_shas


async def test_previous_head_sha_links_prior_deploy(db_session):
    ordered = sorted(
        _fixtures(), key=lambda p: parse_github(json.loads(_raw(p))).run_started_at
    )
    first = await ingest_github(db_session, _raw(ordered[0]))
    second = await ingest_github(db_session, _raw(ordered[1]))
    assert first.previous_head_sha is None
    assert second.previous_head_sha == first.head_sha


async def test_duplicate_delivery_does_not_double_insert(db_session):
    path = _fixtures()[0]
    d1 = await ingest_github(db_session, _raw(path))
    d2 = await ingest_github(db_session, _raw(path))
    assert d1.id == d2.id
    total = (
        await db_session.execute(
            select(func.count(Deploy.id)).where(Deploy.head_sha == d1.head_sha)
        )
    ).scalar_one()
    assert total == 1


# --- endpoint signature (gated) --------------------------------------------


@requires_secret
async def test_valid_signature_returns_200(client):
    path = _fixtures()[0]
    resp = await client.post(
        "/ingest/github",
        content=_raw(path),
        headers={
            "x-hub-signature-256": _sig(path),
            "x-github-event": "workflow_run",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["head_sha"]


@requires_secret
async def test_tampered_body_returns_401(client):
    path = _fixtures()[0]
    resp = await client.post(
        "/ingest/github",
        content=_raw(path) + b" ",
        headers={
            "x-hub-signature-256": _sig(path),
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401


@requires_secret
async def test_missing_signature_returns_401(client):
    path = _fixtures()[0]
    resp = await client.post(
        "/ingest/github",
        content=_raw(path),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401
