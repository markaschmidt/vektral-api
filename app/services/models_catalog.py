"""OpenRouter model allowlist (lifted from v1 coding_agent)."""

from __future__ import annotations

from typing import Any

from app.config import get_settings

_CATALOG: list[dict[str, Any]] = [
    {
        "id": "z-ai/glm-5.2",
        "label": "GLM 5.2",
        "provider": "Z.ai",
        "aliases": ["glm", "glm-5.2", "glm5.2"],
    },
    {
        "id": "qwen/qwen3-coder",
        "label": "Qwen3 Coder",
        "provider": "Qwen",
        "aliases": ["qwen", "qwen3-coder", "qwen-coder"],
    },
    {
        "id": "qwen/qwen3.5-397b-a17b",
        "label": "Qwen3.5 397B",
        "provider": "Qwen",
        "aliases": ["qwen3.5", "qwen-latest"],
    },
    {
        "id": "moonshotai/kimi-k2.7-code",
        "label": "Kimi K2.7 Code",
        "provider": "Moonshot",
        "aliases": ["kimi", "kimi-code", "kimi-k2.7-code"],
    },
    {
        "id": "moonshotai/kimi-k3",
        "label": "Kimi K3",
        "provider": "Moonshot",
        "aliases": ["kimi-k3", "kimi3"],
    },
    {
        "id": "deepseek/deepseek-chat-v3.1",
        "label": "DeepSeek V3.1",
        "provider": "DeepSeek",
        "aliases": ["deepseek", "deepseek-chat"],
    },
    {
        "id": "anthropic/claude-sonnet-4.6",
        "label": "Claude Sonnet 4.6",
        "provider": "Anthropic",
        "aliases": ["claude", "sonnet", "claude-sonnet"],
    },
    {
        "id": "openai/gpt-4o",
        "label": "GPT-4o",
        "provider": "OpenAI",
        "aliases": ["gpt-4o", "gpt4o"],
    },
    {
        "id": "google/gemini-2.5-pro",
        "label": "Gemini 2.5 Pro",
        "provider": "Google",
        "aliases": ["gemini", "gemini-2.5-pro"],
    },
]


_MISTRAL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "mistral-large-latest",
        "label": "Mistral Large",
        "provider": "Mistral",
        "aliases": ["mistral", "mistral-large", "large"],
    },
    {
        "id": "mistral-small-latest",
        "label": "Mistral Small",
        "provider": "Mistral",
        "aliases": ["mistral-small", "small"],
    },
    {
        "id": "codestral-latest",
        "label": "Codestral",
        "provider": "Mistral",
        "aliases": ["codestral", "code"],
    },
]


def _active_catalog() -> list[dict[str, Any]]:
    s = get_settings()
    if s.mistral_api_key:
        return list(_MISTRAL_CATALOG)
    if s.openrouter_api_key:
        return list(_CATALOG)
    return list(_MISTRAL_CATALOG)


def _default_model_id() -> str:
    s = get_settings()
    if s.mistral_api_key:
        return s.mistral_model
    if s.openrouter_api_key:
        return s.openrouter_model
    return s.mistral_model


def list_openrouter_models() -> dict[str, Any]:
    s = get_settings()
    allowed = s.openrouter_models
    catalog = _active_catalog()
    if not allowed:
        models = catalog
    else:
        by_id = {m["id"]: m for m in catalog}
        models = []
        for mid in allowed:
            if mid in by_id:
                models.append(by_id[mid])
            else:
                models.append({"id": mid, "label": mid, "provider": "", "aliases": []})
    default_model = _default_model_id()
    if not any(m["id"] == default_model for m in models):
        for opt in catalog:
            if opt["id"] == default_model:
                models = [opt] + models
                break
        else:
            models = [
                {"id": default_model, "label": default_model, "provider": "", "aliases": []}
            ] + models
    return {"default_model": default_model, "models": models}


def embodiment_model_id() -> str:
    """Backend-controlled lightweight planner. Not exposed in the user picker."""
    s = get_settings()
    pick = (s.embodiment_model or "").strip()
    if pick:
        return pick
    if s.mistral_api_key:
        return "mistral-small-latest"
    return s.openrouter_model or "mistral-small-latest"


def resolve_openrouter_model(requested: str = "") -> dict[str, str]:
    catalog_result = list_openrouter_models()
    default_model = catalog_result["default_model"]
    allowed_ids: list[str] = []
    alias_map: dict[str, str] = {}
    for opt in catalog_result["models"]:
        allowed_ids.append(opt["id"])
        alias_map[opt["id"].lower()] = opt["id"]
        for alias in opt.get("aliases") or []:
            alias_map[str(alias).lower()] = opt["id"]
    for opt in _CATALOG:
        if opt["id"] in allowed_ids or opt["id"] == default_model:
            for alias in opt.get("aliases") or []:
                alias_map[str(alias).lower()] = opt["id"]
    for opt in _MISTRAL_CATALOG:
        if opt["id"] in allowed_ids or opt["id"] == default_model:
            for alias in opt.get("aliases") or []:
                alias_map[str(alias).lower()] = opt["id"]

    pick = (requested or "").strip()
    if not pick:
        return {"ok": "true", "model": default_model}
    resolved = alias_map.get(pick.lower())
    if resolved:
        return {"ok": "true", "model": resolved}
    if pick in allowed_ids:
        return {"ok": "true", "model": pick}
    return {
        "ok": "false",
        "error": (
            f"model '{pick}' not allowlisted; allowed: "
            + ", ".join(allowed_ids)
        ),
    }
