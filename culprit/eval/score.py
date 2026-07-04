"""Scoring — the ONLY place ground truth is read (anti-leakage, decision 9/15).

Compares a ReplayResult to the run's labels and reports honest per-class N: the
Sentry-visible top-k (N=10) and the SNS-silent top-k (N=8) are reported separately
AND combined (N=18) — M2's 10/10 is never silently diluted (plan decision 15).
Abstention N=3, baseline N=1, and cross-source dedup correctness. Silent-fault
accuracy is whatever it honestly is (a weak number reported honestly beats a
gamed one).
"""

from __future__ import annotations

from pathlib import Path
from statistics import median

import yaml

_LABELS_PATH = Path(__file__).with_name("runbook_labels.yaml")


def load_runbook_labels(path: Path | None = None) -> dict[str, str]:
    """The SCORER-ONLY fault_id -> runbook id answer key (plan decision 12)."""
    data = yaml.safe_load((path or _LABELS_PATH).read_text()) or {}
    return data.get("labels", {})


def _has_sentry(run) -> bool:
    return any("sentry" in p for p in run.fixture_paths)


def score_run(run, replay) -> dict:
    """Classify one replayed run against its ground truth."""
    entry: dict = {
        "run_id": run.run_id,
        "ground_truth": run.ground_truth,
        "produced_incident": replay.produced_incident,
        "verdict": replay.verdict,
        "source": replay.source,
        "incident_count": replay.incident_count,
    }

    # Cross-source dedup: a run with BOTH a Sentry event and an SNS alarm is one
    # outage -> must yield exactly one incident (PRD dedup metric, now cross-source).
    if _has_sentry(run) and run.sns:
        entry["dedup_case"] = True
        entry["dedup_ok"] = replay.incident_count == 1

    if not replay.produced_incident:
        if run.ground_truth == "no_incident":
            entry["class"] = "baseline"
            entry["correct"] = True  # correctly stayed silent (no false positive)
        else:
            # Should not happen in M3 (every fault now has a Sentry or SNS feed).
            entry["class"] = "missed"
        return entry

    recorded_window = {c.sha for c in run.window}
    entry["window_ok"] = set(replay.window) == recorded_window

    if run.ground_truth == "culprit_commit":
        entry["class"] = "culprit"
        # Honest top-k: only count when the pipeline actually RETURNED a culprit.
        # An abstention is "no culprit identified" — it must not score a top-k hit
        # just because the labeled sha sits positionally early in a 0-scored list
        # (that would game the silent-fault number; plan decision 15).
        identified = replay.verdict == "culprit"
        entry["verdict_ok"] = identified
        entry["top1"] = identified and replay.ranked[:1] == [run.culprit_sha]
        entry["top3"] = identified and run.culprit_sha in replay.ranked[:3]
        entry["time_to_brief_s"] = replay.time_to_brief_s
    elif run.ground_truth == "abstain":
        entry["class"] = "abstain"
        entry["correct"] = replay.verdict == "abstain"
        entry["time_to_brief_s"] = replay.time_to_brief_s
    else:  # no_incident but an incident WAS produced -> false positive
        entry["class"] = "baseline"
        entry["correct"] = replay.verdict != "culprit"
        entry["false_positive"] = replay.verdict == "culprit"

    return entry


def _topk(rows: list[dict]) -> dict:
    n = len(rows)
    top1 = sum(1 for e in rows if e.get("top1"))
    top3 = sum(1 for e in rows if e.get("top3"))
    return {
        "n": n,
        "top1": top1,
        "top3": top3,
        "top1_rate": (top1 / n) if n else None,
        "top3_rate": (top3 / n) if n else None,
        "window_ok": sum(1 for e in rows if e.get("window_ok")),
    }


def aggregate(entries: list[dict]) -> dict:
    """Roll per-run entries into headline metrics with honest per-class N."""
    culprit = [e for e in entries if e["class"] == "culprit"]
    sentry_culprit = [e for e in culprit if e.get("source") == "sentry"]
    sns_culprit = [e for e in culprit if e.get("source") == "sns"]
    abstain = [e for e in entries if e["class"] == "abstain"]
    baseline = [e for e in entries if e["class"] == "baseline"]
    missed = [e for e in entries if e["class"] == "missed"]
    dedup = [e for e in entries if e.get("dedup_case")]

    ttb = [
        e["time_to_brief_s"] for e in entries if e.get("time_to_brief_s") is not None
    ]

    return {
        "n_total": len(entries),
        "culprit_sentry": _topk(sentry_culprit),
        "culprit_sns_silent": _topk(sns_culprit),
        "culprit_combined": _topk(culprit),
        "abstain": {
            "n": len(abstain),
            "correct": sum(1 for e in abstain if e.get("correct")),
        },
        "baseline": {
            "n": len(baseline),
            "false_positives": sum(1 for e in baseline if e.get("false_positive")),
        },
        "dedup": {
            "n": len(dedup),
            "correct": sum(1 for e in dedup if e.get("dedup_ok")),
        },
        "missed": {"n": len(missed)},
        "time_to_brief_median_s": median(ttb) if ttb else None,
    }


def _topk_lines(title: str, k: dict) -> list[str]:
    r1 = f"  ({k['top1_rate']:.0%})" if k["top1_rate"] is not None else ""
    r3 = f"  ({k['top3_rate']:.0%})" if k["top3_rate"] is not None else ""
    return [
        f"{title}  N={k['n']}",
        f"  top-1 : {k['top1']}/{k['n']}{r1}",
        f"  top-3 : {k['top3']}/{k['n']}{r3}",
    ]


def format_report(agg: dict, entries: list[dict]) -> str:
    """Render the eval report table (the M3 numbers, honestly denominated)."""
    a = agg["abstain"]
    b = agg["baseline"]
    d = agg["dedup"]
    lines = [
        "Culprit — Milestone 3 eval (replay over the full 22-run corpus)",
        "=" * 64,
        f"Runs replayed: {agg['n_total']}",
        "",
        *_topk_lines("Culprit — Sentry-visible code faults", agg["culprit_sentry"]),
        f"  window reconstructed == recorded : {agg['culprit_sentry']['window_ok']}"
        f"/{agg['culprit_sentry']['n']}",
        "",
        *_topk_lines("Culprit — SNS-silent code faults", agg["culprit_sns_silent"]),
        "  (frameless: log-frames + alarm-class diff affinity; the honest number)",
        "",
        *_topk_lines("Culprit — combined", agg["culprit_combined"]),
        "",
        f"Abstention (infra faults)  N={a['n']}",
        f"  correct 'No code culprit — looks infrastructural' : {a['correct']}/{a['n']}",
        "",
        f"Baseline (benign deploy)  N={b['n']}",
        f"  false positives : {b['false_positives']}/{b['n']}",
        "",
        f"Cross-source dedup (Sentry + SNS -> 1 incident)  N={d['n']}",
        f"  exactly one incident : {d['correct']}/{d['n']}",
        "",
        (
            f"Median time-to-brief (compute): {agg['time_to_brief_median_s']:.2f}s"
            if agg["time_to_brief_median_s"] is not None
            else "Median time-to-brief: n/a"
        ),
    ]
    if agg["missed"]["n"]:
        lines.append(f"MISSED (should not happen): {agg['missed']['n']}")
    lines.append("=" * 64)
    return "\n".join(lines)


def format_postmortem_section(pm: dict | None) -> str:
    """Render the M4 postmortem-completeness section (deterministic, dry-run)."""
    if pm is None:
        return ""
    fc, so = pm["fix_captured"], pm["sns_ok_resolved"]
    pct = f"  ({pm['complete'] / pm['n']:.0%})" if pm["n"] else ""
    return "\n".join(
        [
            "",
            f"Postmortem completeness (dry-run, deterministic)  N={pm['n']}",
            f"  complete drafts (timeline·culprit/abstain·impact+method·"
            f"hypothesis·fix-or-absence) : {pm['complete']}/{pm['n']}{pct}",
            f"  fixing commit captured (code faults)   : {fc['correct']}/{fc['n']}",
            f"  resolved via SNS ALARM->OK (infra)     : {so['correct']}/{so['n']}",
            "=" * 64,
        ]
    )


def format_gated_sections(runbook: dict | None, similar: dict | None) -> str:
    """Render the gated (LLM/Voyage) eval sections, each with its own N."""
    lines: list[str] = []
    if runbook is not None:
        lines += [
            "",
            f"Runbook precision (GATED, LLM; temp-0, ids-constrained)  N={runbook['n']}",
            f"  correct runbook offered : {runbook['correct']}/{runbook['n']}"
            + (f"  ({runbook['correct'] / runbook['n']:.0%})" if runbook["n"] else ""),
        ]
    if similar is not None:
        lines += [
            "",
            f"Similar-incident retrieval (GATED, pgvector/Voyage)  N={similar['n']}",
            f"  w1/w4 sibling retrieved : {similar['correct']}/{similar['n']}"
            + (f"  ({similar['correct'] / similar['n']:.0%})" if similar["n"] else ""),
        ]
    if lines:
        lines.append("=" * 64)
    return "\n".join(lines)
