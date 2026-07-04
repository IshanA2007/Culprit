"""SQLAlchemy models — Postgres is the single source of truth (plan decision 2).

Five tables: ``deploys``, ``incidents``, ``signals``, ``evidence``, ``jobs``.
Self-describing and flat like ``harness/runrecord.py``; every table keeps a jsonb
``raw``/``payload`` column so the full inbound payload survives for M3/audit even
when a later parser wants a field we didn't extract today.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Voyage embedding width (voyage-3.5 default). The incidents.embedding column and
# the embedder must agree on this (plan decision 13).
EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    """Declarative base shared by every Culprit ORM model."""


class Deploy(Base):
    """One ``workflow_run`` ("AWS Deployment") — the deploy timeline.

    NOT a trigger (HANDOFF §4); it keeps SHA+timestamp per deploy current so the
    window can be reconstructed as ``compare(previous_head_sha, head_sha)``.
    """

    __tablename__ = "deploys"

    id: Mapped[int] = mapped_column(primary_key=True)
    head_sha: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    previous_head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conclusion: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class Incident(Base):
    """A deduped outage — exactly one per outage (PRD Risk); the brief's subject."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | resolved
    release: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_key: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deploy_id: Mapped[int | None] = mapped_column(
        ForeignKey("deploys.id"), nullable=True
    )
    verdict: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )  # culprit|abstain
    ranked: Mapped[list] = mapped_column(JSONB, default=list)  # [{sha, score, reason}]
    # Ranked hypotheses + offered runbook + impact snapshot (plan decision 14 —
    # the M4 postmortem input). Additive; no signals-schema change (decision 1).
    diagnosis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # pgvector embedding of title+frames for "similar past incident" search
    # (plan decision 13). Additive; nullable when the embedder is inert.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )
    brief_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Resolution state (M4 plan decision 1/3). Additive; set by resolve_incident.
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The deploy that shipped the fix (most recent after opened_at) — or NULL when
    # the fix was infra remediation with no code change (the fix-side abstention).
    fixing_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # How resolution was detected: discord | sns_ok | manual | sentry_quiet.
    resolution_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    deploy: Mapped[Deploy | None] = relationship()
    signals: Mapped[list[Signal]] = relationship(back_populates="incident")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="incident")


class Signal(Base):
    """One inbound alert (Sentry ``event_alert`` or ``issue``) -> a Signal row.

    ``dedup_key`` (Sentry event/issue id) is unique so replays and duplicate
    webhook deliveries never double-insert (idempotency, plan decision 3).
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # "sentry"
    kind: Mapped[str] = mapped_column(String(32))  # event_alert | issue
    dedup_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    release: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    frames: Mapped[list] = mapped_column(
        JSONB, default=list
    )  # [{file, lineno, function}]
    count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    users: Mapped[int | None] = mapped_column(Integer, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True
    )
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    incident: Mapped[Incident | None] = relationship(back_populates="signals")


class Evidence(Base):
    """A cited artifact gathered for an incident: a commit diff or a per-frame blame."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # diff | blame
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    cited: Mapped[bool] = mapped_column(Boolean, default=False)

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class Job(Base):
    """The async loop's durable queue — doubles as a replay log (HANDOFF §5)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Postmortem(Base):
    """The drafted postmortem for a resolved incident (M4 plan decision 1).

    A sibling of ``evidence``/``jobs`` — one row per incident (unique
    ``incident_id``) so Culprit opens exactly one PR per outage (idempotency, the
    "one brief per outage" stance applied to the write path). ``body`` is the
    rendered Markdown; ``pr_url``/``pr_number`` fill in once the GitHub App opens
    the PR (``state`` drafted -> opened). Culprit drafts; humans merge.
    """

    __tablename__ = "postmortems"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id"), unique=True, index=True
    )
    slug: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(String(255))
    branch: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="drafted")  # drafted|opened
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    incident: Mapped[Incident] = relationship()
