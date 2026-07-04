"""Anthropic wrapper — the LLM phrases the verdict; it never decides it.

Deterministic scores (``culprit/ranking.py``) are authoritative. Sonnet 5 writes
the human-facing rationale for the brief; Haiku 4.5 does cheap summarization. The
model is told the verdict and evidence and asked only to explain them concisely —
so the eval (which scores the deterministic verdict) is never polluted by LLM
nondeterminism (plan decision 6, Risk table).
"""

from __future__ import annotations

from culprit.ranking import RankingResult

SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"

_RATIONALE_SYSTEM = (
    "You are Culprit, an incident-response assistant for a Django app. You are "
    "given a deterministic verdict and its evidence. Write a concise, factual "
    "rationale (2-3 sentences) a busy on-call engineer can trust. Do NOT change "
    "the verdict or invent a different suspect. If a suspect commit is given, "
    "reference its short SHA and cite the concrete evidence. If the verdict is "
    "abstention, state plainly that no code culprit was found and why."
)


class LLM:
    """Thin async Anthropic client. ``enabled`` is False without an API key."""

    def __init__(
        self, api_key: str | None, *, model: str = SONNET, summarizer: str = HAIKU
    ) -> None:
        self.model = model
        self.summarizer = summarizer
        self._client = None
        if api_key:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def rationale(
        self, result: RankingResult, *, error_title: str, impact: str | None = None
    ) -> str | None:
        """Write the brief's rationale from the deterministic verdict + evidence."""
        if not self._client:
            return None

        if result.verdict == "culprit" and result.ranked:
            top = result.ranked[0]
            evidence = (
                f"Verdict: likely code culprit.\n"
                f"Suspect commit: {top.sha}\n"
                f"Evidence: {top.reason}\n"
                f"Files changed: {', '.join(top.files) or 'n/a'}\n"
                f"Ranked candidates: "
                + "; ".join(f"{c.sha[:8]}(score={c.score})" for c in result.ranked)
            )
        else:
            evidence = f"Verdict: abstain ({result.abstain_kind}).\n{result.reason}"

        prompt = (
            f"Error: {error_title}\n"
            f"{evidence}\n"
            + (f"Impact: {impact}\n" if impact else "")
            + "\nWrite the rationale."
        )
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=300,
            system=_RATIONALE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()

    async def summarize(self, text: str) -> str | None:
        """Cheap Haiku summary (e.g. a long incident timeline)."""
        if not self._client:
            return None
        resp = await self._client.messages.create(
            model=self.summarizer,
            max_tokens=200,
            messages=[
                {"role": "user", "content": f"Summarize in 1-2 sentences:\n\n{text}"}
            ],
        )
        return "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()
