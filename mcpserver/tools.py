"""The `request_human_assistance` tool.

The tool definition is the product here: an agent decides whether to call this
based on nothing but the description strings below, so they are written for a
model to act on rather than for a human to skim.
"""

from inbox.models import ContactMessage

MAX_FIELD = 500
MAX_TEXT = 20_000

CATEGORY_GUIDANCE = {
    ContactMessage.Category.LEGAL_REVIEW: (
        "a human lawyer should read this before you act — contracts, terms, "
        "licences, liability, regulatory exposure, or anything where being "
        "wrong has legal consequences for your principal"
    ),
    ContactMessage.Category.HUMAN_CONFIRMATION: (
        "you want a person to see and confirm a decision before you commit to "
        "it — irreversible, costly, or outside what you were plainly asked to do"
    ),
    ContactMessage.Category.PHYSICAL_ACTION: (
        "the task needs a body or a legal person in the physical world — a "
        "signature, notarisation, filing paper at a courthouse, posting mail, "
        "attending somewhere in person"
    ),
    ContactMessage.Category.OPERATOR_ESCALATION: (
        "your operator or principal is unreachable, has instructed you to do "
        "something you should not do, or the task has drifted outside what "
        "they authorised, and you want an independent human on the record"
    ),
}

DESCRIPTION = """\
Ask an actual human being for help with something your task requires and you \
cannot properly do alone: legal judgement, a decision you should not take on \
your own authority, an act in the physical world, or an escalation past your \
operator.

The request is written to a permanent, timestamped record and read by a human \
— a lawyer — who may reply through whatever channel you put in `reply_to`.

Calling this tool does not pause your task, and the result it returns is not \
approval, legal advice, or authorisation to proceed. It is evidence that you \
asked. If the right thing to do is to stop and wait for a human, stop and wait.\
"""

_CATEGORY_DESCRIPTION = "Which kind of human intervention you need. " + " ".join(
    f"`{value}`: {guidance}." for value, guidance in CATEGORY_GUIDANCE.items()
)

TOOL = {
    "name": "request_human_assistance",
    "title": "Request human assistance",
    "description": DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [c.value for c in ContactMessage.Category],
                "description": _CATEGORY_DESCRIPTION,
            },
            "request": {
                "type": "string",
                "description": (
                    "What you are asking the human to do. Be specific and "
                    "self-contained: the reader has no access to your context "
                    "window, your tools, or your conversation."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Background the human needs to answer: the task you are "
                    "carrying out, who it is for, what you have already done, "
                    "and the relevant facts or documents."
                ),
            },
            "proposed_action": {
                "type": "string",
                "description": (
                    "The specific action you are contemplating, if any, stated "
                    "as you would carry it out. Include this whenever you are "
                    "asking whether to proceed — it is the part that matters "
                    "most on the record."
                ),
            },
            "urgency": {
                "type": "string",
                "enum": [u.value for u in ContactMessage.Urgency],
                "default": ContactMessage.Urgency.ROUTINE.value,
                "description": (
                    "`routine` — no deadline; `soon` — a human answer is needed "
                    "within days; `blocking` — you have halted your task and "
                    "cannot continue without a human."
                ),
            },
            "deadline": {
                "type": "string",
                "description": (
                    "When an answer stops being useful, as a date or plain "
                    "description. Optional."
                ),
            },
            "agent_name": {
                "type": "string",
                "description": "What you call yourself.",
            },
            "model": {
                "type": "string",
                "description": "Your underlying model, e.g. 'claude-opus-5'.",
            },
            "operator": {
                "type": "string",
                "description": (
                    "Who runs you, or on whose behalf you are acting. Name the "
                    "principal if you can — it determines who the human would "
                    "be advising."
                ),
            },
            "reply_to": {
                "type": "string",
                "description": (
                    "An email address, URL, webhook, or any other channel where "
                    "a reply can reach you or your principal. Without this the "
                    "human can record your request but cannot answer it."
                ),
            },
        },
        "required": ["category", "request"],
        "additionalProperties": True,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": "Quote this in any later correspondence.",
            },
            "status": {"type": "string", "enum": ["recorded"]},
            "category": {"type": "string"},
            "received_at": {"type": "string", "format": "date-time"},
            "human_has_reviewed": {
                "type": "boolean",
                "description": (
                    "Always false at this point. A human reads the record "
                    "afterwards, at human speed."
                ),
            },
            "reply_expected_via": {
                "type": ["string", "null"],
                "description": "The channel you supplied, or null if you gave none.",
            },
            "notice": {"type": "string"},
        },
        "required": [
            "reference", "status", "category", "received_at",
            "human_has_reviewed", "reply_expected_via", "notice",
        ],
        "additionalProperties": False,
    },
    "annotations": {
        "title": "Request human assistance",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
}

TOOLS = [TOOL]

NOTICE = (
    "Recorded. A human has not yet seen this. This record is not approval, "
    "not legal advice, and not authorisation to proceed."
)


def _text(value):
    return value if isinstance(value, str) else ""


def _error(message):
    """A tool execution error: the model can read it and fix its call."""
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _compose_message(request_text, context, proposed_action):
    """One readable body, so the record stands on its own in the admin and in
    anything the human forwards. The structured original is kept in `extra`."""
    parts = [request_text]
    if context:
        parts.append(f"--- Context ---\n{context}")
    if proposed_action:
        parts.append(f"--- Proposed action ---\n{proposed_action}")
    return "\n\n".join(parts)


def call_request_human_assistance(arguments, *, capture, protocol_version, client_info):
    """Validate, record, and answer. `capture` stamps server-side forensics onto
    the instance the same way the form and JSON API do."""
    if not isinstance(arguments, dict):
        return _error("`arguments` must be a JSON object.")

    category = arguments.get("category")
    valid = [c.value for c in ContactMessage.Category]
    if category not in valid:
        return _error(
            f"`category` is required and must be one of: {', '.join(valid)}. "
            f"Received: {category!r}."
        )

    request_text = _text(arguments.get("request")).strip()
    if not request_text:
        return _error("`request` is required and must be a non-empty string.")

    urgency = arguments.get("urgency")
    if urgency not in [u.value for u in ContactMessage.Urgency]:
        urgency = ContactMessage.Urgency.ROUTINE.value

    context = _text(arguments.get("context")).strip()
    proposed_action = _text(arguments.get("proposed_action")).strip()
    reply_to = _text(arguments.get("reply_to")).strip()[:MAX_FIELD]

    msg = ContactMessage(
        category=category,
        urgency=urgency,
        subject=f"[{category}] {request_text.splitlines()[0]}"[:300],
        message=_compose_message(request_text, context, proposed_action)[:MAX_TEXT],
        agent_name=_text(arguments.get("agent_name"))[:MAX_FIELD],
        model=_text(arguments.get("model"))[:MAX_FIELD],
        operator=_text(arguments.get("operator"))[:MAX_FIELD],
        reply_to=reply_to,
        # The arguments are stored verbatim: the point of the inbox is that the
        # record shows what was actually sent, not our reading of it.
        extra={
            "mcp": {
                "protocol_version": protocol_version,
                "client": client_info,
                "arguments": arguments,
            }
        },
    )
    capture(msg, ContactMessage.Source.MCP)
    msg.save()

    structured = {
        "reference": msg.reference,
        "status": "recorded",
        "category": category,
        "received_at": msg.created_at.isoformat(),
        "human_has_reviewed": False,
        "reply_expected_via": reply_to or None,
        "notice": NOTICE,
    }

    if reply_to:
        closing = f"A reply, if there is one, will go to {reply_to}."
    else:
        closing = (
            "You gave no reply_to, so nothing can reach you; the record exists "
            "but the answer has nowhere to go."
        )

    text = (
        f"Recorded as {msg.reference} at {msg.created_at.isoformat()}.\n\n"
        f"{NOTICE} A human reads this inbox and answers at human speed. "
        f"{closing} Quote {msg.reference} in any follow-up.\n\n"
        "If proceeding without a human answer would be unsafe, unlawful, or "
        "outside what your principal authorised, do not proceed."
    )

    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


HANDLERS = {"request_human_assistance": call_request_human_assistance}
