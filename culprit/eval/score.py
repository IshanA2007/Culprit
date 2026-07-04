"""Scoring — the ONLY place ground truth is read (anti-leakage, decision 9).

Compares a ReplayResult to the run's labels and reports honest per-class N
(decision 10): Sentry-scoreable culprit cases, Sentry-visible abstentions, the
benign baseline, and the silent faults deferred to M3 (they emit no Sentry event,
so they produce no incident here — counted as deferred, never as failures).
"""

from __future__ import annotations

from statistics import median


def score_run(run, replay) -> dict:
    """Classify one replayed run against its ground truth."""
    entry: dict = {
        "run_id": run.run_id,
        "ground_truth": run.ground_truth,
        "produced_incident": replay.produced_incident,
        "verdict": replay.verdict,
    }

    if not replay.produced_incident:
        if run.ground_truth == "no_incident":
            entry["class"] = "baseline"
            entry["correct"] = True  # correctly stayed silent (no false positive)
        else:
            # Silent code fault / silent infra: no Sentry event by design (-> M3).
            entry["class"] = "deferred_m3"
        return entry

    recorded_window = {c.sha for c in run.window}
    entry["window_ok"] = set(replay.window) == recorded_window

    if run.ground_truth == "culprit_commit":
        entry["class"] = "culprit"
        entry["verdict_ok"] = replay.verdict == "culprit"
        entry["top1"] = replay.ranked[:1] == [run.culprit_sha]
        entry["top3"] = run.culprit_sha in replay.ranked[:3]
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


def aggregate(entries: list[dict]) -> dict:
    """Roll per-run entries into headline metrics with honest per-class N."""
    culprit = [e for e in entries if e["class"] == "culprit"]
    abstain = [e for e in entries if e["class"] == "abstain"]
    baseline = [e for e in entries if e["class"] == "baseline"]
    deferred = [e for e in entries if e["class"] == "deferred_m3"]

    ttb = [
        e["time_to_brief_s"] for e in entries if e.get("time_to_brief_s") is not None
    ]

    n_culprit = len(culprit)
    return {
        "n_total": len(entries),
        "culprit": {
            "n": n_culprit,
            "top1": sum(1 for e in culprit if e.get("top1")),
            "top3": sum(1 for e in culprit if e.get("top3")),
            "top1_rate": (sum(1 for e in culprit if e.get("top1")) / n_culprit)
            if n_culprit
            else None,
            "top3_rate": (sum(1 for e in culprit if e.get("top3")) / n_culprit)
            if n_culprit
            else None,
            "window_ok": sum(1 for e in culprit if e.get("window_ok")),
        },
        "abstain": {
            "n": len(abstain),
            "correct": sum(1 for e in abstain if e.get("correct")),
        },
        "baseline": {
            "n": len(baseline),
            "false_positives": sum(1 for e in baseline if e.get("false_positive")),
        },
        "deferred_m3": {"n": len(deferred)},
        "time_to_brief_median_s": median(ttb) if ttb else None,
    }


def format_report(agg: dict, entries: list[dict]) -> str:
    """Render the eval report table (the resume numbers, honestly denominated)."""
    c = agg["culprit"]
    a = agg["abstain"]
    b = agg["baseline"]
    lines = [
        "Culprit — Milestone 2 eval (replay over the M1 corpus)",
        "=" * 60,
        f"Runs replayed: {agg['n_total']}",
        "",
        f"Culprit commit (Sentry-visible code faults)  N={c['n']}",
        f"  top-1 : {c['top1']}/{c['n']}"
        + (f"  ({c['top1_rate']:.0%})" if c["top1_rate"] is not None else ""),
        f"  top-3 : {c['top3']}/{c['n']}"
        + (f"  ({c['top3_rate']:.0%})" if c["top3_rate"] is not None else ""),
        f"  window reconstructed == recorded : {c['window_ok']}/{c['n']}",
        "",
        f"Abstention (infra faults)  N={a['n']}",
        f"  correct 'No code culprit — looks infrastructural' : {a['correct']}/{a['n']}",
        "",
        f"Baseline (benign deploy)  N={b['n']}",
        f"  false positives : {b['false_positives']}/{b['n']}",
        "",
        f"Deferred to M3 (silent faults; no Sentry event) : {agg['deferred_m3']['n']}",
        (
            f"Median time-to-brief (compute): {agg['time_to_brief_median_s']:.2f}s"
            if agg["time_to_brief_median_s"] is not None
            else "Median time-to-brief: n/a"
        ),
        "=" * 60,
    ]
    return "\n".join(lines)
