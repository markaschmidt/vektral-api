# Vocal Bridge — Nova (v2)

Canonical system prompt and agent config for the Vektral voice layer.
Push these to the Vocal Bridge dashboard with the [Vocal Bridge CLI](https://pypi.org/project/vocal-bridge/) (`vb prompt`, `vb config`).

## Why this exists

The v1 Nova prompt told the model to fire client action `spatial_command`
(`set_layout` wall/ring, `select_pane` landing|catalog|detail|checkout,
`apply_change`, `undo`) and to “delegate to Jac.” That stack is gone.

v2 path (see graphify: `api_vocalbridge_query()` → `submit_command()` →
`run_research_job` / `run_linear_job` / `run_coding_job`):

1. VR mints a LiveKit token from Vektral-API (`/api/voice-token`).
2. Spoken turns go to Vocal Bridge. Nova should **query_agent once** with a
   stable `turn_id` for that finalized utterance. Do not also fire
   `spatial_command` or a second native mutation for the same turn.
3. The VR client’s `onAIAgentQuery` posts `POST /api/vocalbridge/query` for
   every finalized transcript, even if the model skips the tool. Body fields:
   `text` (or `transcript`), `workspace_id`, `turn_id` / `turnId` (8–80 chars),
   `is_final: true`, plus optional captures and focus fields.
4. Vektral-API reuses the same dialogue turn record as native
   `POST …/dialogue/turns`. Speak the returned `agent_response`. No Jac. No
   `spatial_command`.

## Push to Vocal Bridge

```bash
pip install --upgrade vocal-bridge
vb auth login   # VOCALBRIDGE_API_KEY (prompts if not passed as an argument)
vb auth status
vb prompt set --file vocalbridge/NOVA_PROMPT.txt
vb config set --client-actions-file vocalbridge/client_actions.json
```

Do **not** push `vocalbridge/ai_agent.json` with `vb config set
--ai-agent-file`: that enables the server-side AI Agent Integration, which the
dashboard refuses while Background AI is on (`AI Agent Integration and
Background System cannot both be enabled`). Vektral does not use the
server-side integration — `query_agent` is a client action (the VR host's
`onAIAgentQuery` POSTs `/api/vocalbridge/query`), and its description already
lives in `client_actions.json` plus `NOVA_PROMPT.txt`. `ai_agent.json` is kept
as a reference copy of that description only.

`client_actions.json` registers one agent-to-app tool, `query_agent`. Pushing
that file replaces any leftover `spatial_command` action. Do not re-add
wall/ring/landing-catalog-detail-checkout actions.

Pane focus stays on `PUT …/agent-context`. That snapshot is observational and
must not be a second tool that creates panes.
