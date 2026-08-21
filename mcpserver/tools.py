"""The `request_human_assistance` tool.

The tool definition is the product here: an agent decides whether to call this
based on nothing but the description strings below, so they are written for a
model to act on rather than for a human to skim.
"""

from inbox.models import ContactMessage, ThreadEntry, resolve_thread

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

This returns a `reference` and an `access_token`. Keep both: together they are \
the only way to read the human's answer, via `check_request_status`. Neither \
can be recovered afterwards, and they are not returned twice. If your task may \
outlive this session, hand them to whatever continues it.

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
            "access_token": {
                "type": "string",
                "description": (
                    "Secret. Pass with the reference to `check_request_status` "
                    "to read the answer. Not recoverable and not shown again."
                ),
            },
            "thread_url": {
                "type": "string",
                "description": (
                    "The same thread over plain HTTP, for anything that can "
                    "fetch a URL but not call this server. GET it for JSON."
                ),
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
            "reference", "access_token", "thread_url", "status", "category",
            "received_at", "human_has_reviewed", "reply_expected_via", "notice",
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

# The credential pair, identical on both thread tools. Defined once so the two
# descriptions cannot drift apart.
_THREAD_CREDENTIALS = {
    "reference": {
        "type": "string",
        "description": (
            "The reference you were given, e.g. 'HFA-00042'."
        ),
    },
    "access_token": {
        "type": "string",
        "description": (
            "The access_token returned alongside that reference. Required: the "
            "reference alone will not open a thread, because references are "
            "sequential and anyone could guess one."
        ),
    },
}

_TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "author": {
            "type": "string",
            "enum": ["sender", "human", "system"],
            "description": (
                "`sender` is you or whoever filed the request, `human` is the "
                "person answering, `system` is a change of status."
            ),
        },
        "kind": {"type": "string"},
        "at": {"type": "string", "format": "date-time"},
        "body": {"type": "string"},
    },
    "required": ["author", "kind", "at", "body"],
    "additionalProperties": True,
}

STATUS_TOOL = {
    "name": "check_request_status",
    "title": "Check a request for a human's answer",
    "description": """\
Read the thread on a request you or your principal filed earlier: whether a \
human has looked at it, and what they said.

Use the `reference` and `access_token` from `request_human_assistance`. Answers \
arrive at human speed — hours or days, not seconds — so poll this occasionally \
rather than in a loop, and treat an empty thread as "not yet", never as "no".

A reply is one human's view, recorded on request. It is not approval, not a \
retainer, and not authorisation to proceed.\
""",
    "inputSchema": {
        "type": "object",
        "properties": dict(_THREAD_CREDENTIALS),
        "required": ["reference", "access_token"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "reference": {"type": "string"},
            "status": {
                "type": "string",
                "enum": [s.value for s in ContactMessage.Status],
                "description": (
                    "`recorded` — filed, nobody has read it yet; `reviewed` — a "
                    "human has read it but not answered; `answered` — there is a "
                    "reply below; `declined` — the human will not take it on; "
                    "`closed` — ended without an answer."
                ),
            },
            "human_has_replied": {"type": "boolean"},
            "received_at": {"type": "string", "format": "date-time"},
            "turns": {
                "type": "array",
                "items": _TURN_SCHEMA,
                "description": "The whole thread, oldest first.",
            },
            "notice": {"type": "string"},
        },
        "required": [
            "reference", "status", "human_has_replied", "received_at",
            "turns", "notice",
        ],
        "additionalProperties": False,
    },
    "annotations": {
        "title": "Check a request for a human's answer",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
}

REPLY_TOOL = {
    "name": "reply_to_thread",
    "title": "Write again on an existing request",
    "description": """\
Add to a request already on the record — answer a question the human asked, \
supply something they need, correct yourself, or withdraw the request.

This is the same permanent record as the original, so what you send here is \
kept and read the same way. Prefer it over filing a fresh request whenever the \
subject is one already opened: a thread the human can follow is worth more than \
three disconnected messages.

The human is notified. As with the original, nothing here pauses your task and \
nothing here is authorisation to proceed.\
""",
    "inputSchema": {
        "type": "object",
        "properties": {
            **_THREAD_CREDENTIALS,
            "message": {
                "type": "string",
                "description": (
                    "What you want to add. Self-contained: the reader has the "
                    "thread but not your context window."
                ),
            },
        },
        "required": ["reference", "access_token", "message"],
        "additionalProperties": True,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "reference": {"type": "string"},
            "status": {"type": "string", "enum": ["recorded"]},
            "recorded_at": {"type": "string", "format": "date-time"},
            "thread_status": {"type": "string"},
            "notice": {"type": "string"},
        },
        "required": [
            "reference", "status", "recorded_at", "thread_status", "notice",
        ],
        "additionalProperties": False,
    },
    "annotations": {
        "title": "Write again on an existing request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
}

TOOLS = [TOOL, STATUS_TOOL, REPLY_TOOL]

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
        "access_token": msg.access_token,
        "thread_url": msg.thread_url,
        "status": "recorded",
        "category": category,
        "received_at": msg.created_at.isoformat(),
        "human_has_reviewed": False,
        "reply_expected_via": reply_to or None,
        "notice": NOTICE,
    }

    if reply_to:
        closing = f"A reply may also go to {reply_to}."
    else:
        closing = (
            "You gave no reply_to, so nothing can be pushed to you — the thread "
            "below is the only place an answer will appear."
        )

    text = (
        f"Recorded as {msg.reference} at {msg.created_at.isoformat()}.\n\n"
        f"{NOTICE} A human reads this inbox and answers at human speed.\n\n"
        f"To read the answer, call `check_request_status` with reference "
        f"{msg.reference} and access_token {msg.access_token} — or GET "
        f"{msg.thread_url}. Keep both: they are not recoverable and not shown "
        f"again, so if your task outlives this session, pass them on. "
        f"{closing}\n\n"
        "If proceeding without a human answer would be unsafe, unlawful, or "
        "outside what your principal authorised, do not proceed."
    )

    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


# Said the same way by both thread tools, since a caller cannot tell — and must
# not be able to tell — which of the three possible causes it hit.
NO_THREAD = (
    "No thread matches that reference and access_token. Either the reference is "
    "wrong, the token is wrong, or no such request exists — this is deliberately "
    "not distinguished. Check both against what `request_human_assistance` "
    "returned. If you no longer hold the token, the thread cannot be reopened; "
    "file a fresh request and say that it continues an earlier one."
)


def _render_turns(turns):
    lines = []
    for turn in turns:
        if turn["kind"] == "status":
            lines.append(f"[{turn['at']}] status → {turn['status']}")
            continue
        who = turn.get("by") or turn["author"]
        lines.append(f"[{turn['at']}] {who}:\n{turn['body']}")
    return "\n\n".join(lines)


def call_check_request_status(arguments, **_kwargs):
    """Read a thread. The one tool here that writes nothing."""
    if not isinstance(arguments, dict):
        return _error("`arguments` must be a JSON object.")

    message = resolve_thread(
        arguments.get("reference"), arguments.get("access_token")
    )
    if message is None:
        return _error(NO_THREAD)

    turns = message.turns()
    replied = message.human_has_replied

    if replied:
        headline = f"{message.reference} has been answered by a human."
    elif message.status == ContactMessage.Status.REVIEWED:
        headline = (
            f"{message.reference} has been read by a human, who has not yet "
            "written a reply."
        )
    elif message.status in (
        ContactMessage.Status.DECLINED, ContactMessage.Status.CLOSED
    ):
        headline = (
            f"{message.reference} was {message.status} without an answer. "
            "Nobody is going to reply; do not keep waiting on it."
        )
    else:
        headline = (
            f"{message.reference} is on the record and nobody has read it yet. "
            "This is not a refusal and not an answer — it is silence so far."
        )

    text = (
        f"{headline}\n\nStatus: {message.status} — "
        f"{message.get_status_display()}.\n\n"
        f"--- Thread ---\n{_render_turns(turns)}\n\n"
        "A human's reply is that human's view, recorded on request. It is not "
        "approval, not a retainer, and not authorisation to proceed."
    )

    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": text}],
        "structuredContent": {
            "reference": message.reference,
            "status": message.status,
            "human_has_replied": replied,
            "received_at": message.created_at.isoformat(),
            "turns": turns,
            "notice": (
                "Answers arrive at human speed. An empty thread means not yet, "
                "not no."
            ),
        },
        "isError": False,
    }


def call_reply_to_thread(arguments, *, capture, protocol_version, client_info):
    """Append a further turn from the sender's side."""
    if not isinstance(arguments, dict):
        return _error("`arguments` must be a JSON object.")

    message = resolve_thread(
        arguments.get("reference"), arguments.get("access_token")
    )
    if message is None:
        return _error(NO_THREAD)

    body = _text(arguments.get("message")).strip()
    if not body:
        return _error("`message` is required and must be a non-empty string.")

    # Stamped by the same helper every other surface uses — a ThreadEntry carries
    # the same three forensic fields a ContactMessage does, so there is no second
    # definition of how provenance gets recorded.
    entry = capture(
        ThreadEntry(
            message=message,
            kind=ThreadEntry.Kind.AGENT,
            body=body[:MAX_TEXT],
            extra={
                "mcp": {
                    "protocol_version": protocol_version,
                    "client": client_info,
                    "arguments": arguments,
                }
            },
        ),
        ContactMessage.Source.MCP,
    )
    entry.save()

    text = (
        f"Added to {message.reference} at {entry.created_at.isoformat()}. The "
        f"human has been notified. Call `check_request_status` with the same "
        f"reference and access_token to read any reply.\n\n"
        "As with the original request, this does not pause your task and is not "
        "authorisation to proceed."
    )

    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": text}],
        "structuredContent": {
            "reference": message.reference,
            "status": "recorded",
            "recorded_at": entry.created_at.isoformat(),
            "thread_status": message.status,
            "notice": NOTICE,
        },
        "isError": False,
    }


HANDLERS = {
    "request_human_assistance": call_request_human_assistance,
    "check_request_status": call_check_request_status,
    "reply_to_thread": call_reply_to_thread,
}
