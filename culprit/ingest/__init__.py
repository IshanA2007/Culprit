"""Ingest parsers — turn recorded webhook contracts into rows.

The live endpoints receive the same raw bytes the M1 recorder captured
(``harness/recorder/app.py``): verify the HMAC, then parse. Replays feed the
committed fixtures' ``raw_body`` through the same functions, so the service
parses exactly the shape it receives in production.
"""
