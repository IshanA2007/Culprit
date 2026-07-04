"""Culprit — the AI incident-response service (Milestone 2: core pipeline).

Separate from ``harness/`` (M1 tooling/corpus). This package is the product:
it ingests the recorded webhook contracts, models signals -> incidents,
reconstructs the deploy window, gathers evidence pinned to the deployed SHA,
ranks the culprit commit (or abstains), and posts a Discord brief.
"""
