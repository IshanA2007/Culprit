"""Pool of plausible benign decoy commits.

The scenario runner interleaves the culprit fault commit with these decoys to
build a realistic multi-commit deploy window. Decoys are *believable*: small,
behavior-preserving edits to real ``tcf_website`` files with realistic commit
messages (docstrings, type hints, comments, extracted constants) — NOT
README/whitespace churn — so "blame the newest commit" and "blame the release"
are genuinely losing strategies an eval can't cheat with.

Each decoy is a git patch verified to apply AND revert cleanly against the
``culprit-harness`` base, stored under ``harness/faults/decoys/``. A sidecar
``decoys.yaml`` maps each id to its realistic commit message.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import yaml

from harness.config import FAULTS_DIR

DECOYS_DIR = FAULTS_DIR / "decoys"
DECOYS_MANIFEST = DECOYS_DIR / "decoys.yaml"


@dataclass(frozen=True)
class Decoy:
    id: str
    message: str
    patch_path: Path


def load_decoys() -> list[Decoy]:
    """Load the decoy pool from decoys.yaml + the .patch files beside it."""
    if not DECOYS_MANIFEST.exists():
        return []
    data = yaml.safe_load(DECOYS_MANIFEST.read_text()) or {}
    decoys = []
    for d in data.get("decoys", []):
        patch = DECOYS_DIR / f"{d['id']}.patch"
        if patch.exists():
            decoys.append(Decoy(id=d["id"], message=d["message"], patch_path=patch))
    return decoys


def sample_decoys(n: int, *, seed: int | None = None) -> list[Decoy]:
    """Pick ``n`` distinct decoys. Deterministic when ``seed`` is given."""
    if n <= 0:
        return []
    pool = load_decoys()
    if n > len(pool):
        raise ValueError(
            f"requested {n} decoys but pool has {len(pool)}; "
            "add more benign edits under harness/faults/decoys/"
        )
    rng = random.Random(seed)
    return rng.sample(pool, n)
