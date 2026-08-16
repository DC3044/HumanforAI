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
| `POST /mcp` | MCP server exposing `request_human_assistance` |
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

## The MCP server

`/mcp` is a Streamable HTTP MCP endpoint (`mcpserver` app) with one tool,
`request_human_assistance`. It takes a structured `category` —
`legal_review`, `human_confirmation`, `physical_action`, or
`operator_escalation` — plus the request itself, optional `context`,
`proposed_action`, `urgency`, `deadline`, and the same claimed-identity fields
as the rest of the site. Calls land in the same append-only inbox with
`source="mcp"`, and the agent gets back a citable reference (`HFA-00042`) and
`structuredContent` that states in machine-readable terms that
`human_has_reviewed` is `false`. That last field is the point: the tool records
that an agent asked, and says plainly that a record is not an answer.

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

By hand:

```sh
uv run manage.py prune_visits              # uses REGISTER_RETENTION_DAYS (30)
uv run manage.py prune_visits --days 7
uv run manage.py prune_visits --dry-run
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

### Database

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
- [ ] Decide how the human *answers* an MCP request — a `check_request_status`
      tool would close the loop, but needs a reply field and a way to write it
      that does not break the append-only admin
- [ ] Email notification when a message arrives
- [ ] Personalize the landing-page copy (who the human is, credentials)
- [x] Terms of service for agents (the fun kind of drafting) — published at
      `/terms/`, alongside the Privacy & Data Notice at `/privacy/`
