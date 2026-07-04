"""Replay-based eval — the origin of the resume numbers.

Each ``runs/*.yaml`` is replayed through the live pipeline against the committed
fixtures; the scorer (``score.py``) is the ONLY reader of ground truth. The
pipeline consumes strictly the ingest contract (fixture raw_body + headers) and
the deploy feed — never ``is_culprit``/``ground_truth``/``culprit_sha`` (the
sacred anti-leakage rule, plan decision 9).
"""
