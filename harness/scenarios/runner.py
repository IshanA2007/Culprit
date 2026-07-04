"""Scenario runner — orchestrates one full fault run (plan decision 7).

Sequence per scenario (code fault):
  1. reset DB from db/local.dump;
  2. build the deploy window on fault/<id>-<ts> (decoys + fault at a non-head
     slot; baseline/infra: decoys only);
  3. tag + push refs to the fork (retained);
  4. sentry-cli release at window-head SHA + set-commits --local + finalize
     (SKIPPED in dry mode — no SENTRY_DSN);
  5. release task (migrate && collectstatic && invalidate_cachalot &&
     clearsessions) via `compose run --rm web`;
  6. RECREATE web (up -d --force-recreate web) with SENTRY_RELEASE=<window head>;
     assert the container reports the new release before driving traffic;
  7. provision an auth session if requires_auth;
  8. drive throttled trigger traffic (infra faults: run the docker action here
     instead of a fault commit — the benign window deployed first);
  9. capture container stderr logs (+ webhooks when a recorder is live);
 10. write the run record;
 11. cleanup: reset working branch, restore containers/memory.

Sentry release + webhook collection are gated on SENTRY_DSN so the whole
pipeline runs end-to-end locally today; they light up when Sentry/ngrok exist.
"""

from __future__ import annotations

import os
import subprocess
import time

import httpx

from harness import config
from harness.auth import provision_session
from harness.decoys import sample_decoys
from harness.fork import BuiltWindow, build_window, reset_to_base, tag_and_push
from harness.manifest import Fault, FaultClass, load_manifest
from harness.runrecord import RunRecord
from harness.traffic import drive

HARNESS_COMPOSE = ["docker", "compose", "-f", "docker-compose.harness.yml"]


def get_fault(fault_id: str) -> Fault:
    faults = {f.id: f for f in load_manifest()}
    if fault_id not in faults:
        raise KeyError(
            f"unknown fault '{fault_id}'. Known: {', '.join(sorted(faults)) or '(empty)'}"
        )
    return faults[fault_id]


def _compose(
    *args: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*HARNESS_COMPOSE, *args],
        cwd=config.TCF_WORK_DIR,
        text=True,
        capture_output=capture,
        check=check,
    )


def _docker(
    *args: str, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], text=True, capture_output=capture, check=check
    )


# --- step 1 ---------------------------------------------------------------
def reset_db() -> None:
    """Restore db/local.dump into culprit_db, dropping existing objects first."""
    db = config.HARNESS_DB_CONTAINER
    # Terminate other backends so DROPs don't block on the web container's conns.
    _docker(
        "exec",
        db,
        "bash",
        "-lc",
        'psql -U "$POSTGRES_USER" -d postgres -c '
        '"SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
        "WHERE datname='$POSTGRES_DB' AND pid<>pg_backend_pid();\"",
        check=False,
    )
    with open(config.LOCAL_DUMP_PATH, "rb") as dump:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                db,
                "bash",
                "-lc",
                'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner',
            ],
            stdin=dump,
            text=False,
            capture_output=True,
        )
    if proc.returncode != 0:
        # pg_restore reports benign "does not exist, skipping" on --clean; only
        # fail on a hard error.
        err = proc.stderr.decode("utf-8", "replace")
        if "ERROR" in err and "does not exist" not in err:
            raise RuntimeError(f"reset_db failed:\n{err[-2000:]}")


# --- step 5 ---------------------------------------------------------------
RELEASE_TASK = (
    "python manage.py migrate && "
    "python manage.py collectstatic --noinput && "
    "python manage.py invalidate_cachalot tcf_website && "
    "python manage.py clearsessions"
)


def run_release_task() -> None:
    """Run the exact release task aws.yml runs, as a one-off container."""
    _compose("run", "--rm", "web", "bash", "-c", RELEASE_TASK)


# --- step 6 ---------------------------------------------------------------
def recreate_web(release_sha: str, *, sentry_dsn: str = "") -> None:
    """Recreate the web container so the new SENTRY_RELEASE env bakes in."""
    env = os.environ.copy()
    env["SENTRY_RELEASE"] = release_sha
    env["SENTRY_DSN"] = sentry_dsn
    subprocess.run(
        [*HARNESS_COMPOSE, "up", "-d", "--force-recreate", "web"],
        cwd=config.TCF_WORK_DIR,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    _wait_healthy()


def container_release(container: str = config.HARNESS_WEB_CONTAINER) -> str:
    out = _docker(
        "inspect",
        "-f",
        "{{range .Config.Env}}{{println .}}{{end}}",
        container,
    ).stdout
    for line in out.splitlines():
        if line.startswith("SENTRY_RELEASE="):
            return line.split("=", 1)[1]
    return ""


def _wait_healthy(timeout: int = 40) -> None:
    url = f"http://localhost:{config.HARNESS_HTTP_PORT}/health"
    for _ in range(timeout):
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("web did not become healthy after recreate")


# --- step 9 ---------------------------------------------------------------
def capture_logs(run_id: str, since_epoch: float) -> str:
    dest_dir = config.FIXTURES_DIR / "logs"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{run_id}-web.log"
    out = _docker(
        "logs",
        "--since",
        str(int(since_epoch)),
        config.HARNESS_WEB_CONTAINER,
        check=False,
    )
    dest.write_text((out.stdout or "") + (out.stderr or ""))
    return str(dest.relative_to(config.REPO_ROOT))


# --- orchestration --------------------------------------------------------
def run_scenario(
    fault_id: str,
    *,
    size: int,
    culprit_position: str = "random",
    timestamp: str,
    seed: int | None = None,
    epoch: float,
) -> RunRecord:
    """Execute one (fault x window) case end-to-end. `epoch`/`timestamp` are
    injected (not generated here) so a run is reproducible/resumable."""
    fault = get_fault(fault_id)
    sentry_dsn = os.environ.get("SENTRY_DSN", "")
    run_id = f"{timestamp}-{fault.id}-w{size}"

    # 1. reset DB
    reset_db()

    # 2. build window (code fault: decoys+fault; infra/baseline: decoys only)
    n_decoys = size - 1 if fault.fault_class is FaultClass.CODE else size
    decoys = sample_decoys(n_decoys, seed=seed)
    window: BuiltWindow = build_window(
        fault,
        decoys,
        size=size,
        culprit_position=culprit_position,
        timestamp=timestamp,
        seed=seed,
    )

    # 3. tag + push to the fork (retained refs for M2)
    tag_and_push(window)

    # 4. sentry release (gated)
    if sentry_dsn:
        from harness.sentry_release import associate_release

        associate_release(window.release_sha)

    # 5. release task (applies bad-migration faults; reproduces prod skew)
    run_release_task()

    # 6. recreate web with the new release; assert it took
    recreate_web(window.release_sha, sentry_dsn=sentry_dsn)
    baked = container_release()
    if baked != window.release_sha:
        raise RuntimeError(
            f"web reports release {baked!r}, expected {window.release_sha!r}"
        )

    # 7. auth session if needed
    cookies, csrf = None, None
    if fault.requires_auth:
        sess = provision_session()
        cookies = sess.cookies
        with httpx.Client(base_url=f"http://localhost:{config.HARNESS_HTTP_PORT}") as c:
            c.get("/", cookies=cookies)
            csrf = c.cookies.get("csrftoken")

    # 8. drive traffic / execute infra action
    if fault.fault_class is FaultClass.INFRA and fault.docker_action:
        subprocess.run(fault.docker_action, shell=True, check=False)
    traffic = drive(fault, cookies=cookies, csrf_token=csrf, repeats=3)

    # 9. capture logs (+ webhooks when a recorder is live — dry mode: logs only)
    log_path = capture_logs(run_id, since_epoch=epoch)

    # 10. run record
    culprit = next((c for c in window.commits if c.is_culprit), None)
    rr = RunRecord(
        run_id=run_id,
        fault_id=fault.id,
        fault_class=fault.fault_class.value,
        ground_truth=fault.ground_truth.value,
        base_sha=window.base_sha,
        release_sha=window.release_sha,
        window=window.commits,
        culprit_sha=culprit.sha if culprit else None,
        injected_at=timestamp,
        decoy_config={
            "size": size,
            "position": culprit_position,
            "seed": seed,
            "decoys": [d.id for d in decoys],
            "traffic_5xx": traffic.error_count,
        },
        log_paths=[log_path],
    )
    rr.write()

    # 11. cleanup
    if fault.fault_class is FaultClass.INFRA:
        _restore_infra(fault)
    reset_to_base()
    return rr


def _restore_infra(fault: Fault) -> None:
    """Undo an infra fault's action so the next scenario starts clean."""
    _docker("start", config.HARNESS_REDIS_CONTAINER, check=False)
    _docker("start", config.HARNESS_DB_CONTAINER, check=False)
    _docker(
        "update",
        "--memory=2g",
        "--memory-swap=-1",
        config.HARNESS_WEB_CONTAINER,
        check=False,
    )
    _compose("up", "-d", "--force-recreate", "web", check=False)
