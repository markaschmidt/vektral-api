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
2. Spoken turns go to Vocal Bridge. Nova should **query_agent**.
3. The VR client’s `onAIAgentQuery` posts `POST /api/vocalbridge/query`.
4. Vektral-API routes research → Linear → coding. No Jac. No `spatial_command`.

## Push to Vocal Bridge

```bash
pip install --upgrade vocal-bridge
vb login   # VOCALBRIDGE_API_KEY
vb prompt set --file vocalbridge/NOVA_PROMPT.txt
vb config set --ai-agent-file vocalbridge/ai_agent.json
vb config set --client-actions-file vocalbridge/client_actions.json
```

Empty `client_actions.json` **replaces** any leftover `spatial_command` action
on the agent. Do not re-add wall/ring/landing-catalog-detail-checkout.

If the VR client later implements real pane focus/layout events, add
`agent_to_app` / `app_to_agent` actions that use **dynamic pane ids**, then
mention those names in `NOVA_PROMPT.txt`.
