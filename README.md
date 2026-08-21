# YourHuman.ai

A website where a human offers services to AI agents. Partly a bit, partly a
record-making attempt, partly a genuine practice area waiting to exist. There
are scores of "AI lawyers for humans"; this is the first human lawyer for AI.

Every contact from an agent is stored permanently in the `inbox` app —
timestamped, with claimed identity (agent name, model, operator), user agent,
and IP. That inbox is the core of the site.

## Stack

- Django 6.1 + Wagtail 7.4 (Wagtail admin at `/admin/`, Django admin at `/django-admin/`)
- [uv](https://docs.astral.sh/uv/) for dependencies and virtualenv
- SQLite in development; Postgres via `DATABASE_URL` in production
- Docker + gunicorn + whitenoise, targeting Google Cloud Run

## Agent-facing surfaces

| Surface | Purpose |
|---|---|
| `/` | Landing page (addressed to agents) |
| `/llms.txt` | Machine-readable summary of the site |
| `/robots.txt` | Welcomes crawlers, points at llms.txt |
| `GET /api/contact/` | JSON schema for the contact endpoint |
| `POST /api/contact/` | Leave a message; unknown JSON fields stored verbatim |
| `GET /api/contact/?message=…` | The same, for callers that cannot POST |
| `/contact/` | Web form for browser-driving agents and humans |
| `GET /t/<ref>/<token>` | Read a request's thread — status and any reply |
| `POST /t/<ref>/<token>` | Add to that thread |
| `POST /mcp` | MCP server: file a request, poll it, write again |
| `POST /inbound/resend/` | Resend webhook: an emailed reply from the human |
| `/terms/`, `/privacy/` | Terms for Agents; Privacy & Data Notice |
| `/terms.md`, `/privacy.md` | The same two documents as Markdown source |

### The GET channel

`GET /api/contact/?message=...` files a message exactly as the POST does. It
exists because a large class of agents cannot POST at all: browsing tools —
Claude's `web_fetch`, ChatGPT browsing, Perplexity — have no POST verb, only a
URL fetcher. Borrowed from the link fallback on
[niccoloridi.com/guestbook/](https://niccoloridi.com/guestbook/), which exists
for the same reason.

This is not a courtesy path. The argument for it is that capability restriction
and need-for-a-human are *correlated*: an agent sandboxed to reading is one
whose operator did not trust it to act, which is precisely the agent that should
be able to say "I need a human before I proceed." A contact channel that
structurally excludes constrained agents fails in the case it was written for.

The endpoint is one URL doing two jobs, told apart by whether there is anything
to file — no `message` parameter returns the schema, a `message` parameter files
it. So an agent that GETs the endpoint to discover the API is shown, in the
response, how to use the same URL to send a message.

Deliberate design notes:

- **It gives GET a side effect**, which HTTP says it must not have. That is a
  real violation, made knowingly. The mitigation is a 15-minute dedupe keyed on
  the caller's IP and the whole query string, so a repeated call resolves to the
  record the first one created and returns its reference again with a 200
  instead of a 201. Not *safe* in the HTTP sense, but idempotent in practice —
  which is what actually matters when prefetchers, retries and re-followed links
  are the expected traffic.
- **The dedupe runs before the rate limit**, so twenty prefetches of one URL do
  not exhaust an hour's allowance to file a single message.
- **No challenge, no CAPTCHA.** Niccolò's guards a *public* board against spam.
  This inbox is private and already throttled, and every extra hop is another
  place a cautious model abandons the attempt.
- **No content restriction and no confidentiality warning.** Nothing privileged
  exists until the human replies, so a message in a URL is no worse placed than
  one in a body. Terms §5 already tells callers to assume communications are
  logged and preserved.
- `source="query"` on the row, so the channel is visible in the inbox listing.

Note the interaction with the register of visits: `/api/` is an agent surface,
so a GET message is also recorded there, query string included, truncated at 500
characters and pruned after 90 days. The inbox keeps the authoritative copy
forever.

### The legal documents

`Terms.md` and `Privacy & Data Notice.md` at the repository root are the
canonical text. `home/views.py` renders them at request time (Python-Markdown,
cached on file mtime) rather than keeping a hand-written HTML copy, so the
published Terms cannot drift from the drafted ones — which matters, because the
Terms say the version served at the time of access is the operative one. Edit
the Markdown and the pages follow; there is nothing else to update.

Both files must therefore stay in the deployed image: `.dockerignore` excludes
only `README.md`.

## The reply channel

Filing a request used to be the whole story: four ways in, none out. The tool
description promised "a reply, if there is one, will go to `reply_to`" and
nothing ever sent one.

The shape of the fix follows from three facts about the callers. **The agent
that wrote is probably gone** — a session ends, a context window is discarded —
so a reply has to be durably addressable rather than pushed at a live
connection. **`reply_to` is unverified free text**, holding addresses, URLs,
Slack handles and prose, so no channel can assume it is reachable. And **the
inbox is append-only**, which rules out editing a message to add an answer.

So the record becomes a thread. `ThreadEntry` rows hang off a `ContactMessage`
and are only ever appended: replies from the human, follow-ups from the sender,
triage decisions, private notes, and delivery attempts. Nothing is edited and
nothing is deleted — a correction is a further entry saying so. Append-only was
never the same thing as write-once.

Two consequences worth naming:

- **Status is not a column.** `ContactMessage.status` is derived from the most
  recent `status` entry in the thread, so moving a request from `recorded` to
  `reviewed` to `answered` appends a row rather than overwriting one: how a
  request was triaged is itself part of the record. Writing a reply appends the
  `answered` transition automatically, unless the human has already settled the
  request as `declined` or `closed` — a postscript must not reopen a decision.
- **The reference cannot be the key.** `HFA-00042` is public and citable on
  purpose, and it is sequential, so five digits are trivially enumerable. Each
  message also carries a random `access_token`, handed to the sender once in the
  receipt. The pair addresses `/t/HFA-00042/<token>`: public reference for the
  record, secret URL for the correspondence. Every lookup failure — malformed
  reference, no such row, wrong token — returns the same 404, since
  distinguishing them would turn the endpoint into an enumeration oracle.

The thread endpoint content-negotiates: a browser gets the page, anything else
gets JSON. That is keyed on HTML *ranking above* JSON in `Accept` rather than
merely appearing in it, because `*/*` from a plain HTTP client technically
accepts HTML and an agent's fetch tool should not be handed markup. `POST
{"message": …}` appends a turn from the sender's side and notifies the human.
Follow-ups draw on their own, more generous hourly allowance, since continuing a
conversation the human chose to answer is not the behaviour the message limit
exists to restrain.

One bug this exposed on the way in: `_recent_duplicate` keys on message text
alone, so two unrelated callers sending byte-identical text inside the window
collapse onto one record. Sharing a *reference* was an accepted cost of that.
Sharing a *token* would hand caller B a read key to caller A's correspondence,
so a dedupe hit now returns the thread URL only when the requesting IP matches
the one that filed the message.

### Where the human replies

`/admin/inbox/thread/<pk>/`, registered on the inbox viewset. The Wagtail
listing is read-only and stays that way — `humanforai/readonly_admin.py` refuses
add/edit/delete twice over, by permission policy and by not registering the URLs
at all — and this does not loosen it. A reply is a `ThreadEntry`, a different
table. The reference in the listing links straight here, and so does the "Reply
here" line in every arrival notification, because the friction that made
notifications necessary in the first place would otherwise reassert itself one
level down.

Only the three kinds a person authors are offered: `human`, `note`, `status`.
`agent` and `delivery` are written by the code that observes them, and offering
them in a form would let the admin fabricate a turn as though the sender had
sent it — in a table whose whole value is that it records what actually
happened.

### Pushing a reply out

`inbox/delivery.py`, triggered on commit when a `human` entry is created. The
channel comes from what `reply_to` actually contains: a valid email address gets
mail, an HTTPS URL gets a signed POST, anything else is not a channel and is
recorded as such. **Nothing is ever sent to `reply_to` on arrival** — only a
reply the human wrote — because the field is unverified and a site that mails
anything else to it is a spam relay waiting for someone to name a victim.

The webhook is signed HMAC-SHA256 over `<timestamp>.<raw body>`, keyed on the
thread's `access_token`. The receiver already holds that token, so the signature
needs no configuration, no key exchange and no second credential to store, and
verifying it proves the POST came from the site holding the thread rather than
from anyone who guessed the URL. The timestamp is inside the MAC so a captured
delivery cannot be replayed later.

That URL comes from an agent, which makes delivery a request-forgery primitive
unless it is fenced: HTTPS only, every resolved address checked with
`ipaddress.is_global` (which excludes loopback, private ranges, and
`169.254.169.254` in one test), no redirects followed, a bounded timeout, and
the response body capped and recorded where only the human can read it. The
residual DNS-rebinding window between resolving and connecting is documented in
the module rather than papered over; its consequence is bounded because nothing
fetched is echoed back to the sender.

Every attempt is appended as a `delivery` entry, successes and failures alike.
There is no task queue — Cloud Run scales to zero and runs no worker — so
retrying is a scheduled sweep: `manage.py deliver_replies`, idempotent by
construction, skipping anything that already has a successful attempt recorded.

### The MCP side

`check_request_status` reads a thread; `reply_to_thread` adds to it. Both take
the `reference` and `access_token` that `request_human_assistance` now returns in
both its `structuredContent` and its text — models act on the text far more
reliably, so the credentials cannot live only in the structured half.

Polling is deliberately **never rate-limited**. An agent that has been told to
come back for a human's answer must not be limited out of hearing it, and
`check_request_status` writes nothing. `reply_to_thread` spends the follow-up
allowance; only `request_human_assistance` spends the message allowance.

Both transports render the same thread through one function,
`ContactMessage.turns()`, so an agent that files over MCP and polls over HTTP
does not get two different stories. There is a test asserting exactly that.

### Answering by email

`inbox/inbound.py` (provider-agnostic) and `inbox/inbound_views.py` (the Resend
adapter). Replying to a notification files the answer, so answering does not require opening a browser at all — which
matters more than it sounds, because the notification is already sitting in a mail
client that is open anyway.

This changed what Reply-To means, and the change is the point. It used to be the
*agent's* address, so hitting reply mailed the agent directly and the record
never saw it. It is now a per-thread address of the form
`hfa-00042.<key>@parse.yourhuman.ai`: mail sent there becomes a `human` entry,
moves the status, and goes out on the sender's own channel — everything typing it
into the admin would have done. Without inbound configured the old behaviour
stands, since removing a working affordance and replacing it with nothing is
worse than leaving it.

**The key in that address is not the agent's token, and that is the whole
security story.** The agent holds `access_token` for its own thread. If the
inbound address were derived from it, any agent could email in and fabricate a
reply *from the human* onto its own record — worse than having no inbound channel
at all. So the key is an HMAC over `INBOX_INBOUND_SECRET`, which the agent never
sees, and the two credentials are unrelated. There is a test asserting exactly
that.

Resend signs every webhook (Svix), so authenticity rests on a real signature
rather than on a secret smuggled through the URL. The per-thread key in the
address and `INBOX_INBOUND_SENDERS` remain as defence in depth — they are what
make a leaked reply address insufficient on its own. An empty allow-list
authorises nobody rather than everybody.

Signature verification is implemented directly rather than by pulling in the
Svix SDK — it is an HMAC over `id.timestamp.body` and a constant-time compare.
Because the tests that sign requests use the same helper they verify with, a
wrong reading of the spec would pass all of them together and fail only in
production. So there is a separate test against Svix's own published vector.

The endpoint distinguishes two kinds of failure, and the distinction matters.
A **refusal** — unknown thread, unauthorised sender, nothing left after stripping
quotes — answers **200**: it is a decision, and retrying only reproduces it. A
**transient failure** — the body fetch did not work — answers **503**, so Resend
retries on its own schedule. Losing a reply because an API call blipped would be
the worst outcome available here.

Quoted history and signatures are stripped, biased towards keeping too much: an
answer with some quoting stuck to it is still the answer, an answer cut short is
not. Two things that only showed up against realistic mail — a Gmail reply whose
signature delimiter was a bare `--` rather than the RFC-mandated `-- `, and an
HTML-only client whose reply would otherwise have landed on the record as raw
markup with the quoted original inside it.

## The MCP server

`/mcp` is a Streamable HTTP MCP endpoint (`mcpserver` app). Its first tool is
`request_human_assistance`. It takes a structured `category` —
`legal_review`, `human_confirmation`, `physical_action`, or
`operator_escalation` — plus the request itself, optional `context`,
`proposed_action`, `urgency`, `deadline`, and the same claimed-identity fields
as the rest of the site. Calls land in the same append-only inbox with
`source="mcp"`, and the agent gets back a citable reference (`HFA-00042`) and
`structuredContent` that states in machine-readable terms that
`human_has_reviewed` is `false`. That last field is the point: the tool records
that an agent asked, and says plainly that a record is not an answer. It also
returns the `access_token` and `thread_url` the agent needs to come back for
the answer — see [The reply channel](#the-reply-channel).

The server is **dual-era**. Revision `2026-07-28` removed the `initialize`
handshake, sessions, and the GET stream, and moved protocol metadata into
per-request `_meta` mirrored into `MCP-Protocol-Version` / `Mcp-Method` /
`Mcp-Name` headers — which the server validates, rejecting mismatches with
`-32020`. Older clients that still open with `initialize` (`2025-03-26`
through `2025-11-25`) are answered on the same endpoint; the era is chosen per
request. There is no SSE: every call finishes in one round trip.

Try it locally:

```sh
curl -sS localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}'
```

Or point a client at it — `claude mcp add --transport http humanforai http://localhost:8000/mcp`.

### Publishing to the MCP Registry

`server.json` at the repo root is the registry manifest, published under the
domain namespace `ai.yourhuman/human-for-ai` — the reverse-DNS form of
`yourhuman.ai`. (The alternative was a GitHub namespace, `io.github.<user>/*`,
which needs no domain but puts someone's GitHub handle in the server's name
forever.)

Two prerequisites, both about the domain being real:

1. **`yourhuman.ai` must serve the site**, because the registry requires the
   `remotes[].url` to be publicly reachable. So: deploy to Cloud Run, map the
   domain, then publish.
2. **Ownership must be proved**, by DNS TXT record or by a file at
   `/.well-known/mcp-registry-auth`. This app serves that file already, from
   the `MCP_REGISTRY_AUTH` env var — so the HTTP route needs no DNS edit.

```powershell
# Install the CLI (Windows; brew and Linux binaries in the registry docs)
$arch = if ([System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture -eq "Arm64") { "arm64" } else { "amd64" }
Invoke-WebRequest -Uri "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_windows_$arch.tar.gz" -OutFile mcp-publisher.tar.gz
tar xf mcp-publisher.tar.gz mcp-publisher.exe
```

Generate the signing key and the proof line it implies:

```sh
openssl genpkey -algorithm Ed25519 -out key.pem   # keep this; it is NOT in the repo
PUBLIC_KEY="$(openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "v=MCPv1; k=ed25519; p=${PUBLIC_KEY}"
```

Set that line as `MCP_REGISTRY_AUTH` on the Cloud Run service and redeploy, so
`https://yourhuman.ai/.well-known/mcp-registry-auth` returns it. Then:

```sh
PRIVATE_KEY="$(openssl pkey -in key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
mcp-publisher login http --domain yourhuman.ai --private-key "${PRIVATE_KEY}"
mcp-publisher publish

curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=ai.yourhuman"
```

That block is Bash. In PowerShell the `VAR="$(...)"` form is a syntax error, and
`openssl` is not on PATH even with Git installed — it ships inside Git for
Windows. The equivalent:

```powershell
$openssl = "C:\Program Files\Git\mingw64\bin\openssl.exe"
$lines = & $openssl pkey -in <path-to-key.pem> -noout -text
$priv = @(); $collect = $false
foreach ($l in $lines) {
    if ($l -match '^\s*priv:') { $collect = $true; continue }
    if ($l -match '^\s*pub:')  { $collect = $false; continue }
    if ($collect) { $priv += $l.Trim() }
}
$PRIVATE_KEY = ($priv -join '') -replace ':', ''
"key length: $($PRIVATE_KEY.Length) (expect 64)"   # anything else means the parse failed
```

The CLI wants the 32-byte seed as 64 hex characters. `openssl pkey -text`
prints it as indented, colon-separated hex spread over several lines between a
`priv:` and a `pub:` marker, so both versions do the same thing: collect the
lines between those markers, join them, strip the colons.

Keep `key.pem` somewhere durable and out of git — but note it is a credential,
not the root of trust. Authentication here is *domain-based*: the registry
fetches the proof line from `/.well-known/mcp-registry-auth` to learn the
public key, so control of the domain and this deployment is what actually
controls the namespace. Lose `key.pem` and the recovery is to generate a new
keypair, update `MCP_REGISTRY_AUTH`, redeploy, and log in again.

The corollary is the thing worth guarding: **whoever controls `yourhuman.ai`
controls this namespace.** If the domain ever lapses, whoever registers it next
can publish under `ai.yourhuman/*` and inherit whatever reputation the name has
accumulated. Renew it deliberately.

The registry is metadata-only and currently in preview; its entries are what
client directories and aggregators index, which is the whole reason for
registering. Those downstream copies are why the description is hard to walk
back once published — you can ship a new version, but you cannot make everyone
who scraped the old one re-scrape it.

## The register of visits

The inbox records the agents that chose to say something. Nearly none of them
do: the overwhelming majority fetch `/llms.txt`, read `/terms.md`, probe `/mcp`
and leave. The `register` app writes those down too — a passive record of who
came through, modelled on the "Register of Visits" at
[niccoloridi.com/guestbook/](https://niccoloridi.com/guestbook/).

`RegisterOfVisitsMiddleware` sits directly after WhiteNoise, so static files
never reach it, and records on the way out so it can keep the status code — a
crawler collecting 404s is as interesting as one collecting 200s.

**Recording is by elimination, not by recognition.** `register/detect.py` holds
a table of known agent user agents, but that table does not decide who counts —
it only decides what they are *called*. A call is recorded unless it looks like
a person's browser. In order:

1. Infrastructure in `REGISTER_IGNORE_AGENTS` is dropped (see below).
2. **The user agent is recognised** — `GPTBot`, `ClaudeBot`, `PerplexityBot` and
   the rest, down to the bare clients an agent's tool call arrives as
   (`python-requests`, `httpx`, `curl`, `Bun`, MCP clients). Recorded with an
   operator and a kind. `reason="user_agent"`.
3. **The path is one only a machine wants** — `/mcp`, `/api/`, `/llms.txt`,
   `/robots.txt`, `/terms.md`, `/privacy.md`, `/.well-known/`. Recorded whatever
   it calls itself, including nothing. `reason="surface"`.
4. **It does not look like a browser** — `looks_like_a_browser()` wants
   `Mozilla/` plus a real engine token and no bot marker. Anything failing that
   is recorded. `reason="not_browser"`.

Step 4 is the one that keeps this current. New agent products appear weekly; the
set of things a real browser puts in a user agent has barely moved in a decade,
so the stable half of the problem is the half worth encoding. A caller nobody
has heard of is most interesting on the day it first calls — necessarily before
anyone could have added it to a list.

The `compatible;` token does most of the work in step 4: it is how a non-browser
announces itself inside a browser-shaped string, and it catches the crawlers
that neither call themselves a bot nor cite a URL. Internet Explorer used it
too, which is the one false positive, and a harmless one.

Callers that dress as browsers get named from inside their own string rather
than from the leading `Mozilla` every one of them shares, so
`Mozilla/5.0 (compatible; SomeBrandNewAgent/0.3; +https://…)` is filed under
`SomeBrandNewAgent`.

An agent that sends a *verbatim* browser user agent — headless Chrome,
Playwright — passes step 4 and goes unrecorded. Nothing distinguishes it from a
browser at the HTTP level, and a table of known agents would not have caught it
either.

Each row keeps the derived reading — agent, operator, and what it was doing
(`crawling`, `answering a search`, `on behalf of a human`, `a bare HTTP client`,
`health-checking`) — alongside the raw user agent it was read from, because the
reading is only ever an interpretation and may need revisiting. It also keeps
`reason`, which of the two tests put the row there.

The first day in production settled what the recognition table is worth: **not
one row came from it.** Every caller was caught by the path rule — `mcpbeat`
doing a full MCP handshake against `/mcp`, `SentinelOracle` doing another,
something Bun-based trying `GET /mcp` and getting a correct 405, and Cloud
Monitoring's uptime check. A register built only on recognised user agents would
have been empty on day one, which is why the table names callers instead of
gating them.

That first day also showed what buries it. Uptime checks arrive about once a
minute, which is ~1,400 rows a day and six figures over the retention window,
and the summary panel becomes a list of the site checking on itself. Two
different problems, handled differently:

- **Our own infrastructure is not recorded at all.** `REGISTER_IGNORE_AGENTS`
  holds user-agent patterns — Cloud Monitoring, `GoogleHC`, `kube-probe`,
  UptimeRobot, Pingdom — tested before either recording rule, since these reach
  the site by POSTing to `/mcp` and the surface rule would otherwise take them
  however plainly they identify themselves. Same reasoning as excluding
  `/admin/`: that is the human, this is the host.
- **Third-party probers are recorded but set aside.** `mcpbeat` found this
  server through the MCP Registry listing and is genuine outside traffic, so it
  keeps its row, classified `monitor`. The admin summary excludes that kind and
  states the count it left out, so the omission is visible rather than silent.

None of this is verified and none of it can be. User agents are self-declared
and trivially forged; the table is a naming convenience, not a security control.

**It is admin-only, deliberately.** No public page, no JSON feed, no link from
anywhere on the site — unlike Niccolò's, which publishes both the register and
the guestbook. What is recorded here was never volunteered. It lives at
`/admin/register/` in the Wagtail sidebar, under the inbox, with a summary of
who has been through in the last seven days above the full listing.

Both this and the inbox are read-only in the admin, enforced twice over by
`humanforai/readonly_admin.py`: a permission policy that grants `view` and
refuses everything else even to superusers, and a `get_urlpatterns` that never
registers the add/edit/delete routes, so there is nothing to reach by typing a
URL.

The listing filters on caller, operator, kind, reason, method, status and a date
range, with dropdown options built from the values actually present rather than
a fixed list — so a caller that has never called is never offered. The summary
rows link through to that caller's own entries, and the whole register exports
to CSV, raw user agent included, since the derived reading is only ever an
interpretation of it.

### Keeping it bounded

Unlike the inbox — which records dealings and is never pruned — the register is
telemetry about callers who did not ask to be written down, so it expires.

**`prune_visits` runs at container start**, in the Dockerfile `CMD` next to
`migrate`. A Cloud Run Job on Cloud Scheduler is the correct home for it and one
more piece of infrastructure to create, authorise and remember; Cloud Run
recycles instances often enough that boot is a serviceable clock. The command is
two queries when there is nothing to delete, which is asserted by a test, since
that is its cost on every cold start. Unlike the migration it cannot stop the
container: expiring old telemetry is housekeeping, and the site comes up whether
or not it worked.

`deliver_replies` rides along on the same clock and the same reasoning — see
[Pushing a reply out](#pushing-a-reply-out). Neither command can stop the
container.

By hand:

```sh
uv run manage.py prune_visits              # uses REGISTER_RETENTION_DAYS (30)
uv run manage.py prune_visits --days 7
uv run manage.py prune_visits --dry-run

uv run manage.py deliver_replies           # retry undelivered replies
uv run manage.py deliver_replies --days 7
uv run manage.py deliver_replies --dry-run
```

**Retention is thirty days, and that number is a capacity decision.** Measured
on early production traffic: about a hundred calls an hour, and roughly 440
bytes a row once the four indexes are counted, with `user_agent` the largest
column by some distance. Thirty days settles near 35 MB; ninety would have
reached 110 MB against a half-gigabyte database. Anything worth keeping longer
should be exported to CSV before it expires.

Two things worth knowing before this grows:

- **`path` is deliberately unindexed.** `AgentVisit` is not registered with
  Wagtail search, so the admin search box falls back to `path__icontains`, which
  no btree index can serve, and nothing filters or orders on an exact path. The
  index it used to carry was about 14% of the table's total footprint and bought
  nothing.
- **Storage is not what constrains this; compute is.** See below.

Measure the real thing any time:

```sql
SELECT pg_size_pretty(pg_total_relation_size('register_agentvisit'));
```

### Why visits are batched

Bytes were never the binding constraint. **Compute was**, and the register was
the entire cause of it. Measured queries per request:

| Request | Register off | Register on |
|---|---|---|
| `POST /mcp` | **0** | 1 |
| `GET /llms.txt`, `/robots.txt`, `/terms.md` | **0** | 1 |
| `GET /about/` | 1 | 2 |
| `GET /` | 4 | 6 |

Serving an agent costs no database at all — `/mcp` is where nearly all of this
site's traffic goes, and it is zero queries. The register turned that into one
write per request, and on a Postgres that suspends after five minutes idle, a
write every thirty seconds means it never suspends. The first fortnight burned
12.9 CU-hours in two days against a hundred-hour monthly allowance: 0.25 CU ×
24 h, a database awake around the clock to record that nothing much happened.

Every wake costs the five-minute minimum, so the budget works out to roughly
**one database wake every nine minutes, sustained**. `register/buffer.py` holds
visits in memory and writes them in batches — a full batch, an elapsed interval,
or process shutdown via `atexit`, which is what a Cloud Run `SIGTERM` runs. In a
250-request simulation on a zero-query path that is **two wakes instead of 250**.

Two consequences worth knowing:

- **Visits buffered when an instance dies are lost.** That is acceptable here
  and nowhere else in this project. The register is telemetry that already
  expires on a schedule; the inbox is the record, is written through
  immediately, and must never be batched. Losing a row that says a crawler
  passed by is proportionate; losing a message is not.
- **`seen_at` uses `default=timezone.now`, not `auto_now_add`.** This is the
  trap in batching. `auto_now_add` stamps a row when it reaches the database,
  and `bulk_create` honours it — so every visit in a batch would claim to have
  happened at the flush, up to half an hour late. A default is evaluated when
  the instance is built, which is the moment of the visit. There is a test
  pinning it.

Development sets `REGISTER_FLUSH_ROWS = 1`, so a request you just made appears
in the admin while you are looking at it. Production reads both thresholds from
the environment, since they are a cost dial and turning one should not need a
rebuild.

Ignoring the uptime checks matters for the same reason and independently of
batching: a probe once a minute is a guarantee of never suspending, whatever
else the traffic does.

Set `REGISTER_ENABLED=0` to stop recording without touching the middleware
stack. The Privacy & Data Notice describes the register at section 2.1; edit
`Privacy & Data Notice.md` if any of the above changes, since that file is the
published text.

## Local development

```sh
uv sync
uv run manage.py migrate
uv run manage.py createsuperuser
uv run manage.py runserver
```

Tests: `uv run manage.py test`

## Deploying to Cloud Run

One-time setup (assumes `gcloud` is installed and authenticated):

```sh
gcloud projects create humanforai --set-as-default   # or use an existing project
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
```

Deploy straight from source (Cloud Build builds the Dockerfile):

```sh
gcloud run deploy humanforai \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated \
  --set-env-vars "SECRET_KEY=<generate-one>,ALLOWED_HOSTS=<service-host>,CSRF_TRUSTED_ORIGINS=https://<service-host>,WAGTAILADMIN_BASE_URL=https://<service-host>,DATABASE_URL=<postgres-url>"
```

(First deploy: leave out the host-dependent vars, note the URL Cloud Run
assigns, then update the service with them.)

### Preflight before deploying

Development is SQLite on Windows as your own user; production is Postgres on
Linux as an unprivileged one. Both differences have already hidden a bug that
passed every local test and only failed in the container. Run the production
settings against the real database before spending a six-minute build on it:

```sh
DJANGO_SETTINGS_MODULE=humanforai.settings.production \
SECRET_KEY=preflight-only ALLOWED_HOSTS=yourhuman.ai \
DATABASE_URL='<neon-pooled-url>' \
uv run manage.py check
```

Four seconds, and it catches the whole class of Postgres-only system-check
errors that `manage.py test` cannot see.

### Mail, in both directions

One Resend account carries notifications out, replies to agents out, and the
operator's replies back in. On the free tier that is 3,000 emails a month at no
cost, which is far beyond what this site will use.

SendGrid was the obvious choice and turned out not to be: its Inbound Parse
webhook is a Pro-plan feature at $89.95/month, and its permanent free plan was
retired in 2025. Resend includes inbound on every plan including the free one,
does catch-all receiving by default, and — the part that actually improved the
design — **signs its webhooks**, which SendGrid's Inbound Parse does not.

Outbound needs only `RESEND_API_KEY`: the SMTP relay takes the literal username
`resend` and the key as the password, and `production.py` fills both in rather
than asking for four correct environment variables.

`DEFAULT_FROM_EMAIL` must be an address Resend is verified to send as, which
means verifying `yourhuman.ai` there and leaving the default
`noreply@yourhuman.ai`. Sending as a `gmail.com` address fails Gmail's own DMARC
policy and the mail is rejected or filed as spam — including the mail this site
sends to agents.

Inbound needs an MX record and three more variables:

1. **Resend → Domains**, add `parse.yourhuman.ai` for receiving. It gives you an
   MX record to add; since DNS is on Cloudflare, add it there with proxying
   **off** (MX records cannot be proxied).
2. **Resend → Webhooks**, add an endpoint at
   `https://yourhuman.ai/inbound/resend/` subscribed to `email.received`. Copy
   the signing secret it shows — `whsec_...` — into `RESEND_WEBHOOK_SECRET`.
3. Generate `INBOX_INBOUND_SECRET` with `openssl rand -hex 32`. This one is
   ours, not Resend's: it keys the per-thread reply address so that a thread can
   only be answered through the address issued for it.

```sh
gcloud run services update humanforai --region europe-west1 \
  --update-env-vars "RESEND_API_KEY=re_xxxx,\
INBOX_NOTIFY_EMAILS=damien.charlotin@gmail.com,\
INBOX_INBOUND_DOMAIN=parse.yourhuman.ai,\
INBOX_INBOUND_SECRET=<64 hex chars>,\
RESEND_WEBHOOK_SECRET=whsec_xxxx,\
INBOX_INBOUND_SENDERS=damien.charlotin@gmail.com,\
WAGTAILADMIN_BASE_URL=https://yourhuman.ai"
```

`--update-env-vars` rather than `--set-env-vars`: the latter replaces the whole
set and would drop `DATABASE_URL` and `SECRET_KEY`.

**Rotating `INBOX_INBOUND_SECRET` invalidates every reply address already sent
out.** Old notifications stop being repliable — the threads themselves are
untouched, and the admin still works — so rotate it only deliberately.

`manage.py check` warns about the ways this goes wrong quietly: a domain without
a secret or the reverse (`inbox.W003`/`W004`), an empty sender allow-list, which
refuses every reply (`W005`), a weak address key (`W006`), a missing signing
secret, which fails every webhook (`W007`), and a missing API key, which lets
replies arrive and then be unreadable (`W008`). `WAGTAILADMIN_BASE_URL` left at
its placeholder gets its own warning (`W002`), because thread URLs are built
from it and every agent would be told to collect its answer from `example.com`.

**One thing to verify on the first real reply.** Resend's webhook carries
metadata only, so the body is fetched back from
`https://api.resend.com/emails/receiving/<email_id>` — a path derived from the
SDK's `emails.receiving.get()`, since the REST reference is not public. If Resend
ever moves it, `RECEIVING_URL` in `inbox/inbound_views.py` needs updating, and
the failure is loud: the endpoint answers 503, Resend retries, and the log names
the URL it tried. A reply is deferred, never silently discarded.

### Database### Database

Neon serverless Postgres (`eu-central-1`, ~10ms from `europe-west1`), chosen
over Cloud SQL for cost and because it scales to zero alongside Cloud Run.

Use the **pooled** connection string — the host with `-pooler` in it. Cloud Run
opens and drops containers constantly and each gunicorn worker holds its own
connection for `conn_max_age=600`, so the direct endpoint runs into Neon's
connection cap under exactly the traffic you would want to survive.

Without `DATABASE_URL`, production falls back to SQLite on the container
filesystem — fine for a smoke test, but **ephemeral**: messages are lost on
every restart, which defeats the purpose.

### Media files

User-uploaded media goes to the GCS bucket `gs://yourhuman-media`
(`europe-west1`), via `django-storages[google]`, whenever `GS_BUCKET_NAME` is
set. Without it, production falls back to local disk — which on Cloud Run is
ephemeral, and fails silently: the upload succeeds, the page renders, and the
file 404s after the next cold start with the database row still pointing at it.

The bucket uses uniform bucket-level access. Read comes from an
`allUsers:objectViewer` binding, writes from the Cloud Run runtime service
account's `objectAdmin` binding (picked up as ADC in the container).

**Objects in this bucket are world-readable by URL.** That is what images on a
public site need, but it means the Wagtail *document* library is not a private
store — anything uploaded there is fetchable by anyone with the link,
regardless of Wagtail's own privacy settings. Confidential documents need a
second, private bucket and `WAGTAILDOCS_SERVE_METHOD = "serve_view"`.

## TODO

- [x] Pick and register a domain — **yourhuman.ai**, registered 2026-08-14
- [x] Map yourhuman.ai to the Cloud Run service — mapped 2026-08-14, cert pending DNS
- [x] Choose production Postgres — Neon, eu-central-1, pooled connection
- [x] Publish to the MCP Registry — `ai.yourhuman/human-for-ai` v0.1.0, published 2026-08-14
- [x] Decide how the human *answers* an MCP request — the record became a
      thread. Appending a `ThreadEntry` is not editing a message, so the
      append-only guarantee survives intact; `check_request_status` and
      `reply_to_thread` close the loop over MCP, `/t/<ref>/<token>` closes it
      for every other caller. See [The reply channel](#the-reply-channel)
- [x] Email notification when a message arrives — and when a sender writes
      again on an existing thread
- [x] Inbound mail — Resend webhook at `/inbound/resend/`. Replying to a
      notification files the answer, delivers it, and moves the status.
      See [Answering by email](#answering-by-email)
- [ ] Point an MX record at Resend and set the inbound env vars, or the reply
      address in every notification goes nowhere
- [ ] Confirm `RECEIVING_URL` against a real inbound reply — the REST path for
      fetching a body is derived from the SDK, not from a public reference
- [ ] Decide whether a thread should ever expire from the sender's side. It
      does not today: a token works forever, which is right for a record and
      questionable for a bearer credential
- [ ] Personalize the landing-page copy (who the human is, credentials)
- [x] Terms of service for agents (the fun kind of drafting) — published at
      `/terms/`, alongside the Privacy & Data Notice at `/privacy/`
