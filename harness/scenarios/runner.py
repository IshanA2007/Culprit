"""Scenario runner — orchestrates one full fault run (plan decision 7).

Sequence per scenario:

  1. reset DB from ``db/local.dump``;
  2. build the deploy window on ``fault/<id>-<ts>`` (decoys + fault at a
     randomized position; baseline/infra: decoys only);
  3. tag + push refs to the fork (retained);
  4. sentry-cli release at window-head SHA, set-commits --local, finalize;
     assert >=1 commit with a patch_set;
  5. run the release task exactly as aws.yml does
     (migrate && collectstatic && invalidate_cachalot && clearsessions);
  6. RECREATE the web container (up -d web) so the new SENTRY_RELEASE takes;
     assert the app reports the new release before driving traffic;
  7. provision an auth session if requires_auth;
  8. drive throttled trigger traffic (infra faults: run the docker action
     instead of steps 2-6's fault commit — the benign window deployed first);
  9. collect webhooks + capture container stderr logs;
 10. write the run record (base SHA, ordered window commits w/ is_culprit,
     decoy config, timestamps, fixture paths, ground truth);
 11. cleanup: reset working branch, restore memory/containers, resolve/delete
     the Sentry issue — refs and tags stay.

STATUS: skeleton wiring the settled module interfaces. Full orchestration is
Task 7 and needs the live profile + Sentry + fork (ask-boundaries).
"""

from __future__ import annotations

from harness.manifest import Fault, load_manifest


def get_fault(fault_id: str) -> Fault:
    faults = {f.id: f for f in load_manifest()}
    if fault_id not in faults:
        raise KeyError(
            f"unknown fault '{fault_id}'. Known: {', '.join(sorted(faults)) or '(manifest empty)'}"
        )
    return faults[fault_id]


def run_scenario(fault_id: str, *, timestamp: str) -> None:
    """Execute the full decision-7 sequence for one fault.

    ``timestamp`` is injected (not generated) so a run is reproducible/resumable.
    """
    fault = get_fault(fault_id)
    raise NotImplementedError(
        f"run_scenario('{fault.id}') is implemented in Task 7 — needs the harness "
        "Docker profile, the Sentry integration, and the fork remote."
    )
