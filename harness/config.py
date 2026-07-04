"""Paths and constants for the harness.

Single source of truth for where things live so every module agrees. Paths are
resolved relative to the Culprit repo root (the parent of the ``harness``
package), not the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

# Culprit repo root = parent of the harness package directory.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The harness owns a full (non-shallow) checkout of theCourseForum2 that it
# thrashes with fault commits. It lives inside the Culprit repo but is
# gitignored (never committed — that would nest tCF's history and code, and
# violate the two-repo design in the plan's decision 1).
TCF_WORK_DIR = Path(
    os.environ.get(
        "CULPRIT_TCF_WORK_DIR", REPO_ROOT / ".harness-work" / "theCourseForum2"
    )
)

# The user's personal, pristine tCF clone — used only as a fast local source to
# clone the working copy from. Never mutated by the harness.
TCF_SOURCE_DIR = Path(
    os.environ.get("CULPRIT_TCF_SOURCE_DIR", Path.home() / "theCourseForum2")
)

# Recorded corpus + run records (committed deliverables).
FIXTURES_DIR = REPO_ROOT / "fixtures"
RUNS_DIR = REPO_ROOT / "runs"

# Fault definitions (patches + manifest).
FAULTS_DIR = REPO_ROOT / "harness" / "faults"
MANIFEST_PATH = FAULTS_DIR / "manifest.yaml"

# The long-lived branch on the fork that carries the harness run-profile changes
# (harness compose file, harness settings module, Sentry SDK). Fault windows are
# branched off this.
HARNESS_BRANCH = "culprit-harness"

# Harness Docker containers (docker-compose.harness.yml).
HARNESS_WEB_CONTAINER = os.environ.get("CULPRIT_WEB_CONTAINER", "culprit_web")
HARNESS_DB_CONTAINER = os.environ.get("CULPRIT_DB_CONTAINER", "culprit_db")
HARNESS_REDIS_CONTAINER = os.environ.get("CULPRIT_REDIS_CONTAINER", "culprit_redis")
LOCAL_DUMP_PATH = REPO_ROOT / "db" / "local.dump"

# Host port the harness web container publishes (maps to container :80, gunicorn).
HARNESS_HTTP_PORT = int(os.environ.get("CULPRIT_HARNESS_PORT", "8000"))
