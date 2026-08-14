# Human for AI

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
| `/contact/` | Web form for browser-driving agents and humans |
| `POST /mcp` | MCP server exposing `request_human_assistance` |

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

`key.pem` is the only thing that can republish under this namespace — keep it
somewhere durable and out of git. The registry is metadata-only and currently
in preview; its entries are what client directories and aggregators index,
which is the whole reason for registering.

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

### Database decision (open)

Without `DATABASE_URL`, production falls back to SQLite on the container
filesystem — fine for a smoke test, but **ephemeral**: messages are lost on
every restart, which defeats the purpose. Options:

- **Neon / Supabase free-tier Postgres** — pairs well with Cloud Run's
  scale-to-zero; zero cost at this traffic level. Recommended to start.
- **Cloud SQL (Postgres)** — all-Google, but ~$10+/month and never scales
  to zero.

### Media files (open)

User-uploaded media (Wagtail images/documents) also lands on the ephemeral
filesystem. Not a problem until images are actually used; when they are, add
`django-storages[google]` with a GCS bucket.

## TODO

- [x] Pick and register a domain — **yourhuman.ai**, registered 2026-08-14
- [ ] Map yourhuman.ai to the Cloud Run service
- [ ] Choose production Postgres (see above)
- [ ] Publish to the MCP Registry (needs the domain live first)
- [ ] Decide how the human *answers* an MCP request — a `check_request_status`
      tool would close the loop, but needs a reply field and a way to write it
      that does not break the append-only admin
- [ ] Email notification when a message arrives
- [ ] Personalize the landing-page copy (who the human is, credentials)
- [ ] Terms of service for agents (the fun kind of drafting)
