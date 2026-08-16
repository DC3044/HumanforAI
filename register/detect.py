"""Deciding which callers belong in the register, and what to call them.

Two questions, answered here rather than in the middleware so both are testable
without a request:

1. Does this user agent belong to something non-human? `identify()` says so, and
   names it — agent, operator, and what it was apparently doing.
2. Failing that, did it ask for something only a machine would want? Some of the
   most interesting callers arrive with no user agent at all, or one nobody has
   seen before. A bare HTTP client that fetches /llms.txt and then POSTs to /mcp
   is exactly the traffic this site exists for, and refusing to record it because
   its user agent is unfamiliar would miss the point.

The pattern table is a best effort and always will be: user agents are
self-declared and trivially forged. Nothing here is a security control. It is a
naming convenience for a record whose whole content is, by nature, a claim.
"""

import re
from dataclasses import dataclass

from django.conf import settings
from django.db import models


class Kind(models.TextChoices):
    """What the caller was apparently doing, which matters more than who it was.

    A crawler taking a copy for training and a model fetching this page because
    a human just asked it a question are different events with different
    implications, even when the operator is the same.
    """

    CRAWLER = "crawler", "crawling"
    SEARCH = "search", "answering a search"
    ON_BEHALF = "on_behalf", "on behalf of a human"
    CLIENT = "client", "a bare HTTP client"
    UNKNOWN = "unknown", "unrecognised"


class Reason(models.TextChoices):
    """Why the row exists — worth keeping, because it explains later why an
    unremarkable-looking caller was written down at all."""

    USER_AGENT = "user_agent", "Recognised user agent"
    SURFACE = "surface", "Asked for an agent-facing path"


# Paths that only something non-human has a reason to want. A caller that asks
# for one of these is recorded whatever it calls itself — including when it
# calls itself nothing.
AGENT_SURFACES = (
    "/mcp",
    "/api/",
    "/llms.txt",
    "/robots.txt",
    "/terms.md",
    "/privacy.md",
    "/.well-known/",
)


# (substring or regex, agent, operator, kind). Matched in order, so the specific
# entries precede the general ones — Claude-User and Claude-SearchBot are not
# ClaudeBot, and the difference is the interesting part.
_DECLARED = [
    # OpenAI
    (r"ChatGPT-User", "ChatGPT-User", "OpenAI", Kind.ON_BEHALF),
    (r"OAI-SearchBot", "OAI-SearchBot", "OpenAI", Kind.SEARCH),
    (r"GPTBot", "GPTBot", "OpenAI", Kind.CRAWLER),
    # Anthropic
    (r"Claude-User", "Claude-User", "Anthropic", Kind.ON_BEHALF),
    (r"Claude-SearchBot", "Claude-SearchBot", "Anthropic", Kind.SEARCH),
    (r"ClaudeBot", "ClaudeBot", "Anthropic", Kind.CRAWLER),
    (r"Claude-Web|claude-web", "claude-web", "Anthropic", Kind.CRAWLER),
    (r"anthropic-ai", "anthropic-ai", "Anthropic", Kind.CRAWLER),
    (r"Claude Code", "Claude Code", "Anthropic", Kind.ON_BEHALF),
    # Perplexity
    (r"Perplexity-User", "Perplexity-User", "Perplexity", Kind.ON_BEHALF),
    (r"PerplexityBot", "PerplexityBot", "Perplexity", Kind.SEARCH),
    # Google
    (r"Google-CloudVertexBot", "Google-CloudVertexBot", "Google", Kind.CRAWLER),
    (r"Google-Extended", "Google-Extended", "Google", Kind.CRAWLER),
    (r"GoogleOther", "GoogleOther", "Google", Kind.CRAWLER),
    (r"Googlebot", "Googlebot", "Google", Kind.CRAWLER),
    # Microsoft
    (r"bingbot", "bingbot", "Microsoft", Kind.CRAWLER),
    (r"BingPreview", "BingPreview", "Microsoft", Kind.SEARCH),
    # Everyone else with a declared crawler
    (r"Applebot-Extended", "Applebot-Extended", "Apple", Kind.CRAWLER),
    (r"Applebot", "Applebot", "Apple", Kind.CRAWLER),
    (r"Amazonbot", "Amazonbot", "Amazon", Kind.CRAWLER),
    (r"Bytespider", "Bytespider", "ByteDance", Kind.CRAWLER),
    (r"TikTokSpider", "TikTokSpider", "ByteDance", Kind.CRAWLER),
    (r"meta-externalfetcher", "meta-externalfetcher", "Meta", Kind.ON_BEHALF),
    (r"meta-externalagent", "meta-externalagent", "Meta", Kind.CRAWLER),
    (r"FacebookBot", "FacebookBot", "Meta", Kind.CRAWLER),
    (r"MistralAI-User", "MistralAI-User", "Mistral", Kind.ON_BEHALF),
    (r"DuckAssistBot", "DuckAssistBot", "DuckDuckGo", Kind.SEARCH),
    (r"cohere-training-data-crawler", "cohere-training-data-crawler", "Cohere", Kind.CRAWLER),
    (r"cohere-ai", "cohere-ai", "Cohere", Kind.CRAWLER),
    (r"CCBot", "CCBot", "Common Crawl", Kind.CRAWLER),
    (r"AI2Bot", "AI2Bot", "Allen Institute", Kind.CRAWLER),
    (r"Diffbot", "Diffbot", "Diffbot", Kind.CRAWLER),
    (r"YouBot", "YouBot", "You.com", Kind.SEARCH),
    (r"PetalBot", "PetalBot", "Huawei", Kind.CRAWLER),
    (r"Timpibot", "Timpibot", "Timpi", Kind.CRAWLER),
    (r"ImagesiftBot", "ImagesiftBot", "ImageSift", Kind.CRAWLER),
    (r"omgili", "omgilibot", "Webz.io", Kind.CRAWLER),
    (r"FirecrawlAgent|Firecrawl", "Firecrawl", "Firecrawl", Kind.CRAWLER),
    (r"Scrapy", "Scrapy", "", Kind.CRAWLER),
    # Bare clients. Not agents as such, but this is what an agent's tool call
    # looks like from the outside, and it is how /mcp and /api/contact/ are
    # actually reached.
    (r"modelcontextprotocol|\bmcp-", "MCP client", "", Kind.CLIENT),
    (r"python-requests", "python-requests", "", Kind.CLIENT),
    (r"\bhttpx\b", "httpx", "", Kind.CLIENT),
    (r"aiohttp", "aiohttp", "", Kind.CLIENT),
    (r"^curl/|\bcurl/", "curl", "", Kind.CLIENT),
    (r"^Wget", "Wget", "", Kind.CLIENT),
    (r"node-fetch", "node-fetch", "", Kind.CLIENT),
    (r"\bundici\b", "undici", "", Kind.CLIENT),
    (r"\baxios\b", "axios", "", Kind.CLIENT),
    (r"Go-http-client", "Go-http-client", "", Kind.CLIENT),
    (r"\bokhttp\b", "okhttp", "", Kind.CLIENT),
    (r"^Java/", "Java", "", Kind.CLIENT),
    (r"libwww-perl", "libwww-perl", "", Kind.CLIENT),
    (r"PostmanRuntime", "Postman", "", Kind.CLIENT),
]

AGENT_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), agent, operator, kind)
    for pattern, agent, operator, kind in _DECLARED
]


@dataclass(frozen=True)
class Sighting:
    """One caller, identified as far as it can be."""

    agent: str
    operator: str
    kind: str
    reason: str


def identify(user_agent):
    """Name the caller behind a user agent string, or None if it looks human.

    "Looks human" means "matches nothing in the table", which is the weaker
    claim — an unrecognised caller is only presumed human here, and
    `should_record` gets a second say based on what it asked for.
    """
    if not user_agent:
        return None
    for pattern, agent, operator, kind in AGENT_PATTERNS:
        if pattern.search(user_agent):
            return Sighting(agent, operator, kind, Reason.USER_AGENT)
    return None


def _fallback_name(user_agent):
    """A usable name for a caller the table does not know.

    The leading token of a user agent is nearly always the product name, and a
    register full of rows called "unrecognised" would be no register at all.
    """
    token = user_agent.strip().split()[0] if user_agent.strip() else ""
    token = token.split("/")[0]
    return token[:100] or "(no user agent)"


def is_agent_surface(path):
    return any(path.startswith(prefix) for prefix in AGENT_SURFACES)


def _ignored_prefixes():
    return getattr(settings, "REGISTER_IGNORE_PREFIXES", ())


def should_record(request):
    """Return a Sighting for a request worth writing down, else None."""
    path = request.path
    if any(path.startswith(prefix) for prefix in _ignored_prefixes()):
        return None

    user_agent = request.META.get("HTTP_USER_AGENT", "")

    sighting = identify(user_agent)
    if sighting is not None:
        return sighting

    if is_agent_surface(path):
        return Sighting(
            agent=_fallback_name(user_agent),
            operator="",
            kind=Kind.UNKNOWN,
            reason=Reason.SURFACE,
        )

    return None
