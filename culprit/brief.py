"""The Discord brief — a living message per incident (HANDOFF §4).

Renders the incident to Markdown: the ranked culprit (or the abstention), the
cited evidence (stack-frame files + the suspect diff), a deterministic impact line
(exact failed-request count + a hedged unique-user estimate, methodology stated),
and a resolve affordance. The message is posted once and *edited* as new signals
join — never re-posted, so there is exactly one brief per outage (PRD Risk).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass
class BriefContext:
    title: str
    verdict: str  # "culprit" | "abstain"
    abstain_kind: str | None
    reason: str
    ranked: list[dict] = field(default_factory=list)  # [{sha, score, reason}]
    rationale: str | None = None
    release: str | None = None
    count: int | None = None
    users: int | None = None
    frames: list[dict] = field(default_factory=list)
    repo: str = ""
    resolved: bool = False


def _impact_line(count: int | None, users: int | None) -> str:
    reqs = count if count is not None else 0
    line = f"**Impact:** ~{reqs} failed request(s)"
    if users:
        line += f" · ≈{users} unique user(s) affected (Sentry userCount, an estimate)"
    return line


def _frames_line(frames: list[dict]) -> str:
    cited = ", ".join(
        f"`{f.get('file')}:{f.get('lineno')}`" for f in frames if f.get("file")
    )
    return f"**Stack frames:** {cited}" if cited else ""


def render_brief(ctx: BriefContext) -> dict:
    """Render a BriefContext into a Discord webhook payload ({'content': ...})."""
    lines: list[str] = []
    header = "✅ **Resolved** — " if ctx.resolved else ""

    if ctx.verdict == "culprit" and ctx.ranked:
        top = ctx.ranked[0]
        sha = top["sha"]
        lines.append(f"{header}🚨 **Incident: {ctx.title}**")
        lines.append(f"**Likely culprit:** `{sha[:8]}`")
        if ctx.rationale:
            lines.append(ctx.rationale)
        else:
            lines.append(top.get("reason", ""))
        frames = _frames_line(ctx.frames)
        if frames:
            lines.append(frames)
        if len(ctx.ranked) > 1:
            ranked = " · ".join(f"`{c['sha'][:8]}`({c['score']})" for c in ctx.ranked)
            lines.append(f"**Ranked suspects:** {ranked}")
        if ctx.repo:
            lines.append(f"🔗 https://github.com/{ctx.repo}/commit/{sha}")
    else:
        lines.append(f"{header}⚠️ **Incident: {ctx.title}**")
        lines.append(f"**{ctx.reason}**" if ctx.reason else "**No code culprit.**")
        if ctx.rationale:
            lines.append(ctx.rationale)

    lines.append(_impact_line(ctx.count, ctx.users))
    if ctx.release:
        lines.append(f"**Release:** `{ctx.release[:8]}`")
    if not ctx.resolved:
        lines.append("_Resolve: react ✅ or reply `resolve`._")

    return {"content": "\n".join(line for line in lines if line)}


class DiscordClient:
    """Posts and edits a webhook message. ``enabled`` is False without a URL."""

    def __init__(
        self, webhook_url: str | None, *, client: httpx.AsyncClient | None = None
    ):
        self.webhook_url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def post(self, payload: dict) -> str | None:
        """Post a new message; return its id (for later edits). None if disabled."""
        if not self.webhook_url:
            return None
        resp = await self._client.post(
            self.webhook_url, params={"wait": "true"}, json=payload
        )
        resp.raise_for_status()
        return resp.json().get("id")

    async def edit(self, message_id: str, payload: dict) -> None:
        """Edit the living message in place (never re-post)."""
        if not self.webhook_url:
            return
        resp = await self._client.patch(
            f"{self.webhook_url}/messages/{message_id}", json=payload
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
