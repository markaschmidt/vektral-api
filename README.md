# Vektral-API

FastAPI monolith: Firebase auth, domain Firestore REST, Vocal Bridge token mint,
and VR `/api/*` (former jac-agent). **Zero Jac dependency.**

## Routes (Wave 3)

| Area | Paths |
|------|--------|
| Health | `GET /health`, `/healthz` |
| Auth | `/auth/me`, `/auth/lobby-config`, `/sso/vr/...` |
| Voice | `/api/voice-token`, `/api/v1/token` |
| Session | `GET/POST /api/session`, `POST /api/workspaces/{id}/enter` |
| Domain | `/api/workspaces`, `/api/orgs`, `/api/github/*` |
| Panes | `/api/workspaces/{id}/panes` CRUD |
| Preview | `.../preview/start\|restart\|status\|logs` → Collab |
| Firecrawl | `/api/firecrawl/scrape\|crawl`, `.../scrapes`, `.../research/publish` |
| Jobs | `/api/submit_command`, `/api/panes/{id}/commands`, `/api/jobs/{id}`, SSE events |
| Voice intent | `POST /api/vocalbridge/query` |
| Models | `GET /api/models` |

See [TESTING.md](TESTING.md) and [JAC_AGENT_CUTOVER.md](JAC_AGENT_CUTOVER.md).

## Local

```bash
cd vektral-api
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ALLOW_DEV_BEARER=true VEKTRAL_USE_MEMORY_STORE=true
uvicorn app.main:app --host 0.0.0.0 --port 8788
```

Compose: from `vektral-web/deploy` → `docker compose up -d --build` (API on `:8788`).
