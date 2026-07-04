"""Anthropic wrapper — the LLM phrases the verdict; it never decides it.

Deterministic scores (``culprit/ranking.py``) are authoritative. Sonnet 5 writes
the human-facing rationale for the brief; Haiku 4.5 does cheap summarization. The
model is told the verdict and evidence and asked only to explain them concisely —
so the eval (which scores the deterministic verdict) is never polluted by LLM
nondeterminism (plan decision 6, Risk table).
"""

from __future__ import annotations

from culprit.ranking import RankingResult
from culprit.runbooks import Runbook, coerce_runbook_id

SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"

_RUNBOOK_SYSTEM = (
    "You are Culprit, an incident-response assistant. You are given an incident "
    "summary and a catalog of remediation runbooks (each 'id — title: summary'). "
    "Choose the SINGLE most relevant runbook for this incident and reply with "
    "ONLY its id, copied exactly. If none of the runbooks fit, reply with the "
    "single word NONE. Never invent an id and never add any other text. Culprit "
    "only OFFERS the runbook to a human — it never executes it."
)

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

    async def select_runbook(
        self, *, context: str, corpus: list[Runbook]
    ) -> str | None:
        """Pick one runbook id for the incident (offer-only; temp 0; ids-constrained).

        v1 retrieval (plan decision 11): titles+summaries in the prompt, Sonnet
        picks. The output is constrained to real corpus ids via
        ``coerce_runbook_id`` — a hallucinated or ``NONE`` reply yields ``None``,
        so the brief never offers a runbook that isn't in the corpus.
        """
        if not self._client or not corpus:
            return None
        catalog = "\n".join(f"- {r.prompt_line()}" for r in corpus)
        valid_ids = {r.id for r in corpus}
        prompt = (
            f"Incident summary:\n{context}\n\n"
            f"Runbook catalog:\n{catalog}\n\n"
            "Reply with exactly one runbook id, or NONE."
        )
        # Sonnet 5 deprecates the temperature param; selection stability comes
        # from the ids-constrained output + the tight single-id instruction.
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=32,
            system=_RUNBOOK_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()
        return coerce_runbook_id(raw, valid_ids)

    async def phrase_diagnosis(self, diagnosis) -> str | None:
        """One-sentence narrative over the deterministic hypotheses (phrasing only).

        The hypotheses, confidences, and evidence citations are authoritative and
        computed deterministically (``culprit/diagnosis.py``); Sonnet only writes a
        readable lead sentence. It must not introduce a new suspect or a verdict.
        """
        if not self._client or not diagnosis.hypotheses:
            return None
        lines = "\n".join(
            f"- [{h.confidence}] {h.statement}" for h in diagnosis.hypotheses
        )
        prompt = (
            "These ranked incident hypotheses are already decided. Write ONE "
            "plain sentence summarizing them for an on-call engineer. Do not add a "
            "new suspect, do not pick a single answer, do not restate every "
            f"hypothesis verbatim.\n\n{lines}"
        )
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=120,
            system=(
                "You are Culprit. You phrase already-decided diagnoses; you never "
                "change or narrow the verdict."
            ),
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
