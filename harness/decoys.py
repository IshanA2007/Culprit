"""Pool of plausible benign decoy commits.

The scenario runner interleaves the culprit fault commit with these decoys to
build a realistic multi-commit deploy window. Decoys must be *believable*: real
edits to real ``tcf_website`` files with realistic commit messages — NOT
README/whitespace churn — so that "blame the newest commit" and "blame the
release" are genuinely wrong strategies an eval can't cheat with.

Each decoy is a small, self-contained, behavior-preserving edit that applies
cleanly to the harness base and passes tCF's own lint (djlint/ruff).

STATUS: skeleton. The concrete decoy edits are authored against the real tCF
tree at the pinned base SHA (plan Task 7 / decision 2). Filled in once the
working clone exists.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Decoy:
    """A benign edit: apply ``diff`` (unified) with ``message`` as the commit."""

    id: str
    message: str
    diff: str


# Populated in Task 7 against the pinned base SHA.
DECOY_POOL: list[Decoy] = []


def sample_decoys(n: int, *, seed: int | None = None) -> list[Decoy]:
    """Pick ``n`` distinct decoys. Deterministic when ``seed`` is given."""
    if n <= 0:
        return []
    if n > len(DECOY_POOL):
        raise ValueError(
            f"requested {n} decoys but pool has {len(DECOY_POOL)}; "
            "add more benign edits to DECOY_POOL (Task 7)"
        )
    rng = random.Random(seed)
    return rng.sample(DECOY_POOL, n)
