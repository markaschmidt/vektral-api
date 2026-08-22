# Cutover from `services/jac_agent` — no 4th container

## Decision

Former jac-agent HTTP surface (`VR_API.md`) is implemented **inside Vektral-API**
as FastAPI routers + plain Python services. Vektral-Collab owns preview Docker /
`/v1/preview/*` and `/v1/research/publish`.

Compose remains **three** services: `web`, `api`, `collab`. Do **not** add
`vektral-jac-agent`.

## Mapping

| Legacy Jac | Vektral-API |
|------------|-------------|
| `session.jac` | `app/routers/session.py` |
| `panes.jac` | `app/routers/panes.py` + `services/panes.py` |
| `preview.jac` / `preview_client.jac` | `routers/preview.py` + `services/preview*.py` |
| `firecrawl.jac` / `research.jac` | `routers/firecrawl.py` + `services/firecrawl.py` / `research.py` |
| `commands.jac` / `events.jac` | `routers/jobs.py` + `services/jobs.py` |
| `coding_agent.jac` models | `services/models_catalog.py` (+ MVP OpenRouter stub) |
| Mongo / OSP nodes | Firestore (+ in-memory when `ALLOW_DEV_BEARER` / `VEKTRAL_USE_MEMORY_STORE`) |

## Env (API)

- `PREVIEW_RUNNER_URL` (or `COLLAB_INTERNAL_URL`) → Collab `:8790`
- `PREVIEW_RUNNER_TOKEN` — shared with Collab
- `FIRECRAWL_API_KEY`, `OPENROUTER_*`
- `ALLOW_DEV_BEARER` — local `Bearer dev:<uid>` only

## Wave 4

Nginx `/vektral/api/` → API; stop any leftover `vektral-jac-agent` / Jac lobby
containers. This Wave 3 deliverable does not require Jac at runtime.

## Background jobs (later)

MVP runs research/coding inline. For multi-replica scale, move job execution to
Celery/RQ + Redis; keep Firestore as job status source of truth.
