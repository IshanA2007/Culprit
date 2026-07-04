"""Record the full M1 corpus (plan Task 8).

Runs every scenario sequentially (shared web/db/redis forbid parallelism):
each code fault at a 1-commit and a 4-commit window (culprit at head-for-size-1,
middle-for-size-4 — varied position, off-head for multi-commit), each infra
fault on a 3-commit benign window, and the baseline. Infra faults run LAST so a
flaky container restart can't cascade into the code runs. Continues on error and
prints a summary; each run is crash-safe and independent.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime

from harness.manifest import FaultClass, load_manifest
from harness.scenarios.runner import run_scenario


@dataclass
class Case:
    fault_id: str
    size: int
    position: str


def build_plan() -> list[Case]:
    """Code faults first (2 window sizes each), then baseline, then infra."""
    faults = load_manifest()
    code, infra, baseline = [], [], []
    for f in faults:
        if f.fault_class is FaultClass.CODE:
            code.append(Case(f.id, 1, "sole"))
            code.append(Case(f.id, 4, "middle"))
        elif f.fault_class is FaultClass.INFRA:
            infra.append(Case(f.id, 3, "n/a"))
        else:
            baseline.append(Case(f.id, 4, "n/a"))
    return code + baseline + infra


def record_all() -> list[dict]:
    plan = build_plan()
    results: list[dict] = []
    print(f"recording {len(plan)} cases", flush=True)
    for i, case in enumerate(plan, 1):
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        label = f"{case.fault_id} w{case.size}"
        try:
            rr = run_scenario(
                case.fault_id,
                size=case.size,
                culprit_position=case.position,
                timestamp=ts,
                seed=1000 + i,
                epoch=time.time(),
            )
            fx = len(rr.fixture_paths)
            results.append(
                {"case": label, "status": "ok", "run_id": rr.run_id, "fixtures": fx}
            )
            print(f"[{i}/{len(plan)}] OK   {label}  fixtures={fx}", flush=True)
        except Exception as exc:  # keep going; each run is independent + crash-safe
            results.append({"case": label, "status": "FAIL", "error": str(exc)[:300]})
            print(f"[{i}/{len(plan)}] FAIL {label}: {exc}", flush=True)
            traceback.print_exc()
        time.sleep(2)

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n=== corpus recording complete: {ok}/{len(plan)} ok ===", flush=True)
    for r in results:
        if r["status"] != "ok":
            print(f"  FAIL {r['case']}: {r.get('error')}", flush=True)
    return results
