"""Environment configuration for Vektral-API."""

from __future__ import annotations

import os
from functools import lru_cache


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


class Settings:
    """Runtime settings read from the environment."""

    def __init__(self) -> None:
        self.api_port = int(os.environ.get("API_PORT", "8788"))
        self.firebase_project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
        self.google_application_credentials = os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS", ""
        ).strip()
        # When true (default), missing SA lets the process start; auth returns 503.
        self.firebase_stub_mode = _bool("FIREBASE_STUB_MODE", True)

        self.cors_origins = _csv("CORS_ORIGINS", "*")
        self.web_origin = (
            os.environ.get("WEB_ORIGIN", "http://127.0.0.1:3000").rstrip("/")
        )
        # Next.js basePath (default /vektral). WEB_ORIGIN may omit this segment;
        # vr_sso_login() appends it when building /auth/vr redirects.
        raw_base = os.environ.get("WEB_BASE_PATH", "/vektral").strip()
        self.web_base_path = "" if raw_base in {"", "/"} else raw_base.rstrip("/")

        self.vr_web_origin = os.environ.get("VEKTRAL_VR_WEB_ORIGIN", "").strip()
        if self.vr_web_origin and not self.vr_web_origin.endswith("/"):
            self.vr_web_origin = f"{self.vr_web_origin}/"
        auto_raw = os.environ.get("VEKTRAL_VR_AUTO_OPEN", "true").lower()
        self.vr_auto_open = auto_raw not in ("0", "false", "no", "") and bool(
            self.vr_web_origin
        )

        self.sso_vr_allowlist = _csv(
            "SSO_VR_CALLBACK_ALLOWLIST",
            "http://localhost:5173/,http://127.0.0.1:5173/,"
            "http://localhost:5174/,http://127.0.0.1:5174/,"
            "http://localhost:3000/,http://127.0.0.1:3000/",
        )

        # Vocal Bridge (lifted from services/voice)
        self.vocalbridge_base_url = (
            os.environ.get("VOCALBRIDGE_BASE_URL")
            or os.environ.get("VOCAL_BRIDGE_BASE_URL")
            or "https://vocalbridgeai.com"
        ).rstrip("/")
        self.vocalbridge_api_key = (
            os.environ.get("VOCALBRIDGE_API_KEY")
            or os.environ.get("VOCAL_BRIDGE_API_KEY")
            or ""
        ).strip()
        self.vocalbridge_agent_id = (
            os.environ.get("VOCALBRIDGE_AGENT_ID")
            or os.environ.get("VOCAL_BRIDGE_AGENT_ID")
            or ""
        ).strip()
        self.voice_timeout = float(os.environ.get("VOICE_UPSTREAM_TIMEOUT", "30"))
        self.voice_user_agent = os.environ.get("VOICE_USER_AGENT", "Vektral/1.0")
        self.voice_default_participant = os.environ.get(
            "VOICE_DEFAULT_PARTICIPANT", "Vektral User"
        )
        self.voice_require_auth = _bool("VOICE_REQUIRE_AUTH", False)

        self.preview_runner_token = os.environ.get("PREVIEW_RUNNER_TOKEN", "").strip()
        # Prefer PREVIEW_RUNNER_URL (v1 name); fall back to COLLAB_INTERNAL_URL
        self.preview_runner_url = (
            os.environ.get("PREVIEW_RUNNER_URL")
            or os.environ.get("COLLAB_INTERNAL_URL")
            or "http://127.0.0.1:8790"
        ).rstrip("/")
        self.collab_internal_url = self.preview_runner_url
        self.preview_public_base = os.environ.get(
            "PREVIEW_PUBLIC_BASE", "http://127.0.0.1:8790/proxy"
        ).rstrip("/")
        self.preview_runner_public_host = os.environ.get(
            "PREVIEW_RUNNER_PUBLIC_HOST", "http://127.0.0.1"
        ).rstrip("/")

        # Optional GH token for preview when user has not connected GitHub yet
        self.github_token_fallback = (
            os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        ).strip()

        # Firecrawl
        self.firecrawl_api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
        self.firecrawl_base_url = os.environ.get(
            "FIRECRAWL_BASE_URL", "https://api.firecrawl.dev"
        ).rstrip("/")

        # OpenRouter
        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.openrouter_base_url = os.environ.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ).rstrip("/")
        self.openrouter_model = os.environ.get("OPENROUTER_MODEL", "z-ai/glm-5.2").strip()
        self.openrouter_models = _csv("OPENROUTER_MODELS", "")
        self.openrouter_http_referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
        self.openrouter_x_title = os.environ.get("OPENROUTER_X_TITLE", "Vektral").strip()

        # Direct Mistral API (default LLM for VR coding agent; OpenRouter is fallback)
        self.mistral_api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
        self.mistral_base_url = os.environ.get(
            "MISTRAL_BASE_URL", "https://api.mistral.ai/v1"
        ).rstrip("/")
        self.mistral_model = os.environ.get(
            "MISTRAL_MODEL", "mistral-large-latest"
        ).strip()

        # Domain / GitHub OAuth (repo connect — separate from Firebase GitHub IdP)
        self.github_client_id = (
            os.environ.get("GITHUB_CLIENT_ID")
            or os.environ.get("SSO_GITHUB_CLIENT_ID")
            or ""
        ).strip()
        self.github_client_secret = (
            os.environ.get("GITHUB_CLIENT_SECRET")
            or os.environ.get("SSO_GITHUB_CLIENT_SECRET")
            or ""
        ).strip()
        self.github_oauth_redirect_uri = os.environ.get(
            "GITHUB_OAUTH_REDIRECT_URI",
            "",
        ).strip()
        self.api_public_url = os.environ.get(
            "API_PUBLIC_URL", f"http://127.0.0.1:{self.api_port}"
        ).rstrip("/")
        self.github_oauth_scopes = os.environ.get(
            "GITHUB_OAUTH_SCOPES", "repo read:user"
        ).strip()

        # Linear OAuth (issue cards — same connect pattern as GitHub)
        self.linear_client_id = os.environ.get("LINEAR_CLIENT_ID", "").strip()
        self.linear_client_secret = os.environ.get("LINEAR_CLIENT_SECRET", "").strip()
        self.linear_oauth_redirect_uri = os.environ.get(
            "LINEAR_OAUTH_REDIRECT_URI",
            "",
        ).strip()
        self.linear_oauth_scopes = os.environ.get(
            "LINEAR_OAUTH_SCOPES", "read,write,issues:create"
        ).strip()

        self.token_encryption_key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()

        # Local/dev: Bearer "dev" or "dev:<uid>" — never enable in production
        self.allow_dev_bearer = _bool("ALLOW_DEV_BEARER", False) or _bool(
            "AUTH_DEV_BYPASS", False
        )
        self.auth_dev_bypass = self.allow_dev_bearer
        self.use_memory_store = _bool("VEKTRAL_USE_MEMORY_STORE", False)

    def web_app_origin(self) -> str:
        """Vektral-Web public origin including Next basePath (for VR SSO redirects)."""
        origin = self.web_origin.rstrip("/")
        base_path = self.web_base_path
        if not base_path or origin.endswith(base_path):
            return origin
        return f"{origin}{base_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
