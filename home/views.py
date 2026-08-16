"""The site's legal documents, served from the Markdown at the repository root.

Those .md files are the canonical text: they are what gets drafted, reviewed and
tracked in git. Rendering them on the way out means the published Terms cannot
drift from the agreed ones, which a second, hand-maintained HTML copy eventually
would — and the Terms themselves say the version the service serves at the time
of access is the operative one.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import markdown

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.safestring import mark_safe


@dataclass(frozen=True)
class LegalDocument:
    source: Path
    title: str
    summary: str
    source_url: str


DOCUMENTS = {
    "terms": LegalDocument(
        source=settings.BASE_DIR / "Terms.md",
        title="Terms for Agents",
        summary=(
            "The terms on which an AI agent, or anything else reading this, may "
            "contact the human behind YourHuman.ai."
        ),
        source_url="/terms.md",
    ),
    "privacy": LegalDocument(
        source=settings.BASE_DIR / "Privacy & Data Notice.md",
        title="Privacy & Data Notice",
        summary=(
            "How YourHuman.ai handles the personal data an agent's message may "
            "contain, including data about people who never visited the site."
        ),
        source_url="/privacy.md",
    ),
}


@lru_cache(maxsize=None)
def _read(source: str, mtime: float) -> str:
    # mtime is part of the cache key rather than a value: editing a document
    # invalidates the entry, so a running dev server picks the change up without
    # a restart, while in production the file never changes and this reads once.
    return Path(source).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _render(source: str, mtime: float) -> str:
    return markdown.markdown(
        _read(source, mtime),
        # "extra" for the definition and nested lists the notices use; "smarty"
        # for the curly quotes and dashes the rest of the site sets in print.
        extensions=["extra", "smarty"],
        output_format="html",
    )


def _cached(document: LegalDocument):
    return document, document.source.stat().st_mtime


def legal_page(request, slug):
    """Render one legal document as a page. `slug` comes from urls.py only."""
    document, mtime = _cached(DOCUMENTS[slug])
    return render(
        request,
        "legal.html",
        {
            "title": document.title,
            "summary": document.summary,
            "source_url": document.source_url,
            # Safe because the source is a file in this repository, not user
            # input: whatever it contains, we wrote and reviewed it.
            "body": mark_safe(_render(str(document.source), mtime)),
        },
    )


def legal_source(request, slug):
    """Serve the same document as Markdown, for readers that prefer the source."""
    document, mtime = _cached(DOCUMENTS[slug])
    return HttpResponse(
        _read(str(document.source), mtime),
        content_type="text/markdown; charset=utf-8",
    )
