"""Postmortem eval (M4) — dry-run completeness over the incident-producing corpus.

Replays every incident-producing run (the M3 replay, unchanged), lets the pipeline
persist ``incidents.diagnosis``, resolves the incident (code faults via the
operator path + the rollback fix-deploy; infra faults via the SNS ``ALARM -> OK``
auto-detect), assembles the postmortem in **dry-run** (no PR pushed), and scores a
structural **completeness checklist** over the rendered Markdown. Deterministic and
LLM-free — the Summary narrative is excluded from the check. N = 21.

Anti-leakage (decision 10): no ground-truth label reaches assembly — the
postmortem is built from persisted incident data, and the completeness check reads
the rendered body. ``run.fix_deploy``/``run.thread``/``run.sns`` are fixture links,
not labels; ``culprit/eval/score.py`` remains the only ground-truth reader.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.config import REPO_ROOT, get_settings
from culprit.discord_read import FixtureThreadReader
from culprit.eval.replay import replay_run, reset_state
from culprit.ingest.sns import resolve_from_alarm_ok
from culprit.models import Deploy, Incident
from culprit.postmortem import draft_postmortem
from culprit.resolution import resolve_incident
from harness.runrecord import load_all_run_records

# The five sections a complete postmortem must carry (plan decision 9).
REQUIRED_CHECKS = (
    "timeline",
    "culprit_or_abstention",
    "impact_method",
    "hypothesis",
    "fix_or_absence",
)


def completeness_checks(body: str) -> dict:
    """Score the required-sections checklist over a rendered postmortem body."""
    low = body.lower()
    return {
        "timeline": "## Timeline" in body,
        "culprit_or_abstention": (
            "Likely code culprit" in body
            or "no code culprit" in low
            or "infrastructural" in low
        ),
        "impact_method": "## Impact" in body and "method:" in body,
        "hypothesis": bool(re.search(r"(?m)^1\. ", body)) or "insufficient" in low,
        "fix_or_absence": (
            "Fixing commit" in body or "infrastructure remediation" in low
        ),
    }


async def _latest_incident(session: AsyncSession) -> Incident:
    return (
        await session.execute(select(Incident).order_by(Incident.id.desc()).limit(1))
    ).scalar_one()


async def _apply_fix_deploy(session: AsyncSession, run) -> None:
    """Apply the rollback: re-deploy base_sha at the fix time (the deploy feed's
    latest entry when the incident cleared), so resolution captures it."""
    env = json.loads((REPO_ROOT / run.fix_deploy).read_text())
    wr = json.loads(env["raw_body"].encode("latin-1"))["workflow_run"]
    fix_sha = wr["head_sha"]
    fix_ts = datetime.fromisoformat(wr["run_started_at"].replace("Z", "+00:00"))
    deploy = (
        await session.execute(select(Deploy).where(Deploy.head_sha == fix_sha))
    ).scalar_one_or_none()
    if deploy is not None:
        deploy.run_started_at = fix_ts  # the rollback re-runs base_sha's deploy
    else:
        session.add(Deploy(head_sha=fix_sha, branch="master", run_started_at=fix_ts))
    await session.commit()


async def _resolve_via_alarm_ok(
    session: AsyncSession, run, received_at
) -> Incident | None:
    """Resolve through the real SNS ALARM->OK auto-detect (source='sns_ok')."""
    env = json.loads((REPO_ROOT / run.sns).read_text())
    notif = json.loads(env["raw_body"].encode("latin-1"))
    alarm_name = json.loads(notif["Message"])["AlarmName"]
    ok = {"Message": json.dumps({"AlarmName": alarm_name, "NewStateValue": "OK"})}
    return await resolve_from_alarm_ok(session, ok, received_at)


async def _replay_resolve_draft(session, run, *, github, narrative=None):
    """Replay -> resolve -> dry-run draft for one incident run. Returns (incident, row)."""
    await reset_state(session)
    session.expunge_all()  # drop stale identities after TRUNCATE ... RESTART IDENTITY
    replay = await replay_run(session, run, github=github)
    if not replay.produced_incident:
        return None, None
    incident = await _latest_incident(session)

    resolved_via = None
    if run.fix_deploy:  # code fault: rollback + operator resolve
        await _apply_fix_deploy(session, run)
        await resolve_incident(session, incident, source="manual")
    elif run.sns:  # infra fault: SNS ALARM->OK auto-detect
        when = (incident.opened_at or datetime.now()) + timedelta(minutes=10)
        resolved_via = await _resolve_via_alarm_ok(session, run, when)
    else:
        await resolve_incident(session, incident, source="manual")

    thread = await FixtureThreadReader(
        REPO_ROOT / run.thread if run.thread else None
    ).read()
    row = await draft_postmortem(
        session,
        incident,
        repo=get_settings().github_repo,
        narrative=narrative,
        thread=thread,
    )
    return incident, (row, resolved_via)


async def evaluate_postmortem_completeness(session, *, github, runs=None) -> dict:
    """Dry-run completeness (N=21) + fixing-commit capture + SNS-OK resolution."""
    runs = runs if runs is not None else load_all_run_records()
    rows = []
    fix_n = fix_ok = sns_n = sns_ok = 0
    for run in runs:
        if run.ground_truth == "no_incident":
            continue  # the baseline produces no incident -> no postmortem (correct)
        incident, out = await _replay_resolve_draft(session, run, github=github)
        if incident is None:
            continue
        row, resolved_via = out

        if run.fix_deploy:
            fix_n += 1
            if incident.fixing_sha == run.base_sha:
                fix_ok += 1
        elif run.sns:
            sns_n += 1
            if resolved_via is not None and incident.resolution_source == "sns_ok":
                sns_ok += 1

        checks = completeness_checks(row.body)
        rows.append(
            {
                "run_id": run.run_id,
                "checks": checks,
                "complete": all(checks.values()),
                "fixing_sha": incident.fixing_sha,
            }
        )

    return {
        "n": len(rows),
        "complete": sum(1 for r in rows if r["complete"]),
        "fix_captured": {"n": fix_n, "correct": fix_ok},
        "sns_ok_resolved": {"n": sns_n, "correct": sns_ok},
        "rows": rows,
    }


async def evaluate_live_pr(session, *, github, writer, run=None) -> dict:
    """GATED: open ONE real postmortem PR to the fork (a sandbox branch), via the
    live GitHubAppWriter. Composes the tested ``publish_postmortem`` path — the
    caller is responsible for closing the PR + deleting the branch afterwards.
    """
    from culprit.postmortem import publish_postmortem

    run = run or next(r for r in load_all_run_records() if r.fault_class == "code")
    incident, out = await _replay_resolve_draft(session, run, github=github)
    if incident is None:
        return {"opened": False, "url": None, "number": None}
    published = await publish_postmortem(
        session, incident, writer=writer, repo=get_settings().github_repo
    )
    return {
        "opened": published.state == "opened",
        "url": published.pr_url,
        "number": published.pr_number,
        "branch": published.branch,
    }
