# Vektral-API smoke tests

Assumes API on `http://127.0.0.1:8788` (Compose or local uvicorn).

**Auth:** Firebase ID token (`Authorization: Bearer <ID_TOKEN>`), **or** local stub:

```bash
export ALLOW_DEV_BEARER=true
export VEKTRAL_USE_MEMORY_STORE=true   # optional; auto-fallback when SA missing + ALLOW_DEV_BEARER
export ID_TOKEN='dev:alice'            # stub identity — never use in production
```

Without SA and without `ALLOW_DEV_BEARER`, auth-gated routes return **503** `firebase_unavailable` (Firebase stub mode).

**On this host (SA installed):** `/healthz` reports `firebase.initialized: true`, `stub_mode: false`, project `spatium-503617`. Then:

| Call | Expected |
|------|----------|
| `GET /auth/me` (no Bearer) | **401** `missing_bearer` |
| `GET /auth/me` + garbage Bearer | **401** `invalid_token` |
| `GET /auth/me` + real Firebase ID token | **200** profile `{ id, email, display_name, … }` |

```bash
curl -sS http://127.0.0.1:8788/healthz | jq .firebase
curl -sS http://127.0.0.1:8788/auth/lobby-config | jq .
export ID_TOKEN='<Firebase ID token from Web sign-in>'
curl -sS -H "Authorization: Bearer $ID_TOKEN" http://127.0.0.1:8788/auth/me | jq .
```

## 1. Health (no auth)

```bash
curl -sS http://127.0.0.1:8788/healthz | jq .
```

## 2. Session (full domain payload)

```bash
# 401 without token
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/api/session

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/session | jq .
# expect profile + github + orgs + workspaces; _meta.jac == false
```

## 3. Workspaces + enter

```bash
# empty list
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces | jq .

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Demo","description":"dashboard","repo_full_name":"acme/demo"}' \
  http://127.0.0.1:8788/api/workspaces | jq .
# save .id → WS_ID

export WS_ID='ws_…'

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID | jq .

curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/enter | jq .
```

Foreign uid cannot read (use `dev:bob` with ALLOW_DEV_BEARER):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer dev:bob" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID
# expect 403 or 404
```

## 3b. Orgs + GitHub status

```bash
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Labs"}' \
  http://127.0.0.1:8788/api/orgs | jq .

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/orgs | jq .

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/github/status | jq .
# disconnected until OAuth; configured=false without GITHUB_CLIENT_*

curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/github/connect | jq .
# url empty + message when not configured
```

Re-fetch session — should include created orgs/workspaces:

```bash
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/session | jq '{orgs, workspaces, github, stub:._meta.stub}'
```

## 3c. Org invites + join codes + ACL

```bash
# Alice creates org + invite for bob@dev.local
curl -sS -H "Authorization: Bearer dev:alice" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Shared Labs"}' \
  http://127.0.0.1:8788/api/orgs | jq .
# save .id → ORG_ID

curl -sS -H "Authorization: Bearer dev:alice" \
  -H 'Content-Type: application/json' \
  -d '{"email":"bob@dev.local","role":"member"}' \
  http://127.0.0.1:8788/api/orgs/$ORG_ID/invites | jq .

curl -sS -H "Authorization: Bearer dev:bob" \
  http://127.0.0.1:8788/api/orgs/invites/pending | jq .

curl -sS -X POST -H "Authorization: Bearer dev:bob" \
  http://127.0.0.1:8788/api/orgs/invites/$INVITE_ID/accept | jq .

# Org workspace — bob can list/enter
curl -sS -H "Authorization: Bearer dev:alice" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Team Board"}' \
  http://127.0.0.1:8788/api/orgs/$ORG_ID/workspaces | jq .

curl -sS -H "Authorization: Bearer dev:bob" \
  http://127.0.0.1:8788/api/workspaces | jq .

# Join code
curl -sS -X POST -H "Authorization: Bearer dev:alice" \
  http://127.0.0.1:8788/api/orgs/$ORG_ID/join-code | jq .
# save .join_code → CODE

curl -sS -H "Authorization: Bearer dev:carol" \
  -H 'Content-Type: application/json' \
  -d "{"code":"$CODE"}" \
  http://127.0.0.1:8788/api/orgs/join | jq .
```

## Pytest (domain)

```bash
cd vektral-api && . .venv/bin/activate
pytest -q tests/test_domain.py tests/test_linear.py tests/test_panes_chat.py
```

Chat panes (`kind=chat`) store transcripts under `GET .../panes/{id}/messages`. Coding jobs gather up to 4 files from Collab `GET /v1/git/{workspace_id}/tree` + `.../file` using the **preview** pane route (Home `/` when the command targeted a chat pane). Reload bumps skip chat panes.

## 4. Panes CRUD (preview + chat)

```bash
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Home","route":"/"}' \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/panes | jq .

# New AI chat pane (toggleable alongside preview panes)
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"New chat","kind":"chat"}' \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/panes | jq .
# save .id → CHAT_PANE_ID

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/panes | jq .
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  "http://127.0.0.1:8788/api/workspaces/$WS_ID/panes?kind=chat" | jq .

curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/panes/$CHAT_PANE_ID/activate | jq .

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/panes/$CHAT_PANE_ID/messages | jq .
```

## 5. Preview orchestration → Collab

Collab preview runner must be up (`PREVIEW_RUNNER_URL`, default `http://127.0.0.1:8790`).
API sends `Authorization: Bearer $PREVIEW_RUNNER_TOKEN` to Collab.

```bash
# Requires GitHub token on the user (OAuth connect) or GITHUB_TOKEN fallback env
curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/preview/start | jq .

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/preview/status | jq .

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/preview/logs | jq .
```

Direct Collab health (bypass API):

```bash
curl -sS http://127.0.0.1:8790/healthz | jq .
curl -sS -H "Authorization: Bearer ${PREVIEW_RUNNER_TOKEN:-dev-preview-token}" \
  http://127.0.0.1:8790/v1/preview/$WS_ID/status | jq .
```

## 6. Firecrawl + research publish

```bash
# Needs FIRECRAWL_API_KEY on the API
curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"https://example.com\",\"workspace_id\":\"$WS_ID\"}" \
  http://127.0.0.1:8788/api/firecrawl/scrape | jq .

curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/scrapes | jq .

# After preview/start (Collab has a checkout), re-render public/research/
curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/workspaces/$WS_ID/research/publish | jq .
```

## 7. Commands / jobs / SSE / vocalbridge

```bash
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/models | jq .

curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"text\":\"scrape https://example.com\",\"workspace_id\":\"$WS_ID\"}" \
  http://127.0.0.1:8788/api/submit_command | jq .
# research keyword routing → command_type research

curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"text\":\"say hello\",\"workspace_id\":\"$WS_ID\",\"pane_id\":\"$PANE_ID\"}" \
  http://127.0.0.1:8788/api/vocalbridge/query | jq .

# Fresh chat thread (new kind=chat pane)
curl -sS -X POST -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"text\":\"start over\",\"workspace_id\":\"$WS_ID\",\"new_conversation\":true}" \
  http://127.0.0.1:8788/api/submit_command | jq .

# Poll job
curl -sS -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/jobs/$JOB_ID | jq .

# SSE until ready/failed
curl -sSN -H "Authorization: Bearer $ID_TOKEN" \
  http://127.0.0.1:8788/api/jobs/$JOB_ID/events
```

## 8. Voice token (unchanged)

```bash
curl -sS -X POST http://127.0.0.1:8788/api/voice-token \
  -H 'Content-Type: application/json' \
  -d '{"participant_name":"Smoke Test"}' | jq .
```

## Local run

```bash
cd vektral-api
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ALLOW_DEV_BEARER=true VEKTRAL_USE_MEMORY_STORE=true
export PREVIEW_RUNNER_URL=http://127.0.0.1:8790
export PREVIEW_RUNNER_TOKEN=dev-preview-token
uvicorn app.main:app --host 0.0.0.0 --port 8788
```

## Zero Jac

This service is plain FastAPI. There is **no** `jac-agent` container, no `jac start`,
no OSP graph. See [JAC_AGENT_CUTOVER.md](JAC_AGENT_CUTOVER.md).
