"""Source locators keyed by Collab runtime kind. Do not re-detect frameworks."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_EXTS = (".tsx", ".ts", ".jsx", ".js", ".css", ".scss", ".html", ".json")
GLOBAL_VITE = {
    "index.html",
    "vite.config.ts",
    "vite.config.js",
    "vite.config.mts",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "src/main.tsx",
    "src/main.jsx",
    "src/main.ts",
    "src/App.tsx",
    "src/App.jsx",
    "src/index.css",
    "src/App.css",
}
GLOBAL_NEXT = {
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "middleware.ts",
    "middleware.js",
    "app/layout.tsx",
    "app/layout.jsx",
    "app/globals.css",
    "pages/_app.tsx",
    "pages/_app.js",
    "pages/_document.tsx",
}
GLOBAL_STATIC = {"index.html", "styles.css", "style.css", "main.css", "app.js"}
GLOBAL_EXPO = {
    "app.json",
    "app.config.js",
    "app.config.ts",
    "package.json",
    "app/_layout.tsx",
    "app/_layout.js",
}


def normalize_route(route: str) -> str:
    raw = (route or "/").strip() or "/"
    parsed = urlparse(raw if "://" in raw else f"http://local{raw if raw.startswith('/') else '/' + raw}")
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _utterance_paths(text: str) -> list[str]:
    hits: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_./-]+\.(?:tsx|ts|jsx|js|css|scss|html|json)", text or ""):
        cleaned = token.lstrip("./")
        if cleaned and ".." not in cleaned.split("/"):
            hits.append(cleaned)
    return hits


def _join(*parts: str) -> str:
    bits = [p.strip("/") for p in parts if p and p != "."]
    return "/".join(bits)


def _with_exts(stem: str) -> list[str]:
    out: list[str] = []
    for ext in (".tsx", ".ts", ".jsx", ".js"):
        out.append(f"{stem}{ext}")
        out.append(f"{stem}/index{ext}")
        out.append(f"{stem}/page{ext}")
    return out


def vite_candidates(route: str, workdir: str = ".", utterance: str = "") -> list[str]:
    path = normalize_route(route)
    prefix = "" if workdir in {"", "."} else workdir.rstrip("/") + "/"
    wanted = [prefix + p if prefix and not p.startswith(prefix) else p for p in GLOBAL_VITE]
    wanted.extend(_utterance_paths(utterance))
    if path == "/":
        wanted.extend([f"{prefix}src/pages/index.tsx", f"{prefix}src/pages/Home.tsx"])
    else:
        stem = path.strip("/")
        wanted.extend(
            [
                f"{prefix}src/pages/{stem}.tsx",
                f"{prefix}src/pages/{stem}/index.tsx",
                f"{prefix}src/{stem}.tsx",
                f"{prefix}src/routes/{stem}.tsx",
            ]
        )
        last = stem.split("/")[-1]
        wanted.extend([f"{prefix}src/pages/{last}.tsx", f"{prefix}src/{last}.tsx"])
    return _dedupe(wanted)


def next_candidates(route: str, workdir: str = ".", utterance: str = "") -> list[str]:
    path = normalize_route(route)
    prefix = "" if workdir in {"", "."} else workdir.rstrip("/") + "/"
    wanted = [prefix + p if prefix and not p.startswith(prefix) else p for p in GLOBAL_NEXT]
    wanted.extend(_utterance_paths(utterance))
    segments = [p for p in path.strip("/").split("/") if p] if path != "/" else []
    # app router
    if not segments:
        wanted.extend(
            [
                f"{prefix}app/page.tsx",
                f"{prefix}app/page.jsx",
                f"{prefix}pages/index.tsx",
                f"{prefix}pages/index.js",
            ]
        )
    else:
        app_path = "/".join(segments)
        wanted.extend(_with_exts(f"{prefix}app/{app_path}"))
        wanted.extend(
            [
                f"{prefix}app/{app_path}/layout.tsx",
                f"{prefix}app/{app_path}/layout.jsx",
                f"{prefix}app/(marketing)/{app_path}/page.tsx",
                f"{prefix}app/(app)/{app_path}/page.tsx",
            ]
        )
        # dynamic [id], [...slug], [[...slug]]
        if len(segments) >= 1:
            parent = "/".join(segments[:-1])
            base = f"{prefix}app/{parent}" if parent else f"{prefix}app"
            wanted.extend(
                [
                    f"{base}/[id]/page.tsx",
                    f"{base}/[slug]/page.tsx",
                    f"{base}/[...slug]/page.tsx",
                    f"{base}/[[...slug]]/page.tsx",
                ]
            )
        wanted.extend(_with_exts(f"{prefix}pages/{app_path}"))
        wanted.extend(
            [
                f"{prefix}pages/{app_path}.tsx",
                f"{prefix}pages/{app_path}.js",
                f"{prefix}pages/{app_path}/index.tsx",
            ]
        )
    wanted.extend([f"{prefix}app/layout.tsx", f"{prefix}pages/_app.tsx"])
    return _dedupe(wanted)


def static_candidates(route: str, workdir: str = ".", utterance: str = "") -> list[str]:
    path = normalize_route(route)
    prefix = "" if workdir in {"", "."} else workdir.rstrip("/") + "/"
    wanted = [prefix + p for p in GLOBAL_STATIC]
    wanted.extend(_utterance_paths(utterance))
    if path == "/":
        wanted.append(f"{prefix}index.html")
    else:
        stem = path.strip("/")
        wanted.extend(
            [
                f"{prefix}{stem}.html",
                f"{prefix}{stem}/index.html",
                f"{prefix}{stem}.css",
                f"{prefix}{stem}.js",
            ]
        )
    return _dedupe(wanted)


def expo_web_candidates(route: str, workdir: str = ".", utterance: str = "") -> list[str]:
    path = normalize_route(route)
    prefix = "" if workdir in {"", "."} else workdir.rstrip("/") + "/"
    wanted = [prefix + p for p in GLOBAL_EXPO]
    wanted.extend(_utterance_paths(utterance))
    if path == "/":
        wanted.extend([f"{prefix}app/index.tsx", f"{prefix}app/index.js", f"{prefix}screens/Home.tsx"])
    else:
        stem = path.strip("/")
        wanted.extend(
            [
                f"{prefix}app/{stem}.tsx",
                f"{prefix}app/{stem}/index.tsx",
                f"{prefix}screens/{stem}.tsx",
            ]
        )
    return _dedupe(wanted)


def _prefix(workdir: str) -> str:
    return "" if workdir in {"", "."} else workdir.rstrip("/") + "/"


def creatable_destinations(kind: str, route: str, workdir: str = ".") -> list[str]:
    """Server-derived files a coder may create for a new route under the runtime workdir.

    Vite also includes the existing router/App entry so the new route can be wired.
    The coder may write only these paths; Collab still performs one path-scoped mutation.
    """
    path = normalize_route(route)
    prefix = _prefix(workdir)
    stem = path.strip("/")
    last = stem.split("/")[-1] if stem else "index"
    wanted: list[str] = []
    if kind == "next":
        if not stem:
            wanted.extend(
                [
                    f"{prefix}app/page.tsx",
                    f"{prefix}pages/index.tsx",
                ]
            )
        else:
            wanted.extend(
                [
                    f"{prefix}app/{stem}/page.tsx",
                    f"{prefix}app/{stem}/page.jsx",
                    f"{prefix}pages/{stem}.tsx",
                    f"{prefix}pages/{stem}/index.tsx",
                ]
            )
        wanted.extend([f"{prefix}app/layout.tsx", f"{prefix}pages/_app.tsx"])
    elif kind == "static":
        if not stem:
            wanted.append(f"{prefix}index.html")
        else:
            wanted.extend([f"{prefix}{stem}.html", f"{prefix}{stem}/index.html"])
        wanted.extend([f"{prefix}styles.css", f"{prefix}index.html"])
    elif kind == "expo-web":
        if not stem:
            wanted.extend([f"{prefix}app/index.tsx", f"{prefix}screens/Home.tsx"])
        else:
            wanted.extend(
                [
                    f"{prefix}app/{stem}.tsx",
                    f"{prefix}app/{stem}/index.tsx",
                    f"{prefix}screens/{stem}.tsx",
                ]
            )
        wanted.append(f"{prefix}app/_layout.tsx")
    else:
        if not stem:
            wanted.extend(
                [f"{prefix}src/pages/index.tsx", f"{prefix}src/pages/Home.tsx"]
            )
        else:
            wanted.extend(
                [
                    f"{prefix}src/pages/{stem}.tsx",
                    f"{prefix}src/pages/{stem}/index.tsx",
                    f"{prefix}src/{stem}.tsx",
                    f"{prefix}src/routes/{stem}.tsx",
                    f"{prefix}src/pages/{last}.tsx",
                ]
            )
        wanted.extend(
            [
                f"{prefix}src/App.tsx",
                f"{prefix}src/App.jsx",
                f"{prefix}src/main.tsx",
                f"{prefix}src/main.jsx",
            ]
        )
    return _dedupe([p for p in wanted if p and ".." not in p.split("/")])


def validate_create_paths(
    kind: str,
    route: str,
    proposed: list[str],
    workdir: str = ".",
) -> list[str]:
    allowed = set(creatable_destinations(kind, route, workdir))
    out: list[str] = []
    for raw in proposed:
        path = (raw or "").lstrip("/")
        if path in allowed and path not in out:
            out.append(path)
    return out


def candidates_for(kind: str, route: str, workdir: str = ".", utterance: str = "") -> list[str]:
    if kind == "next":
        return next_candidates(route, workdir, utterance)
    if kind == "static":
        return static_candidates(route, workdir, utterance)
    if kind == "expo-web":
        return expo_web_candidates(route, workdir, utterance)
    return vite_candidates(route, workdir, utterance)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def is_global_path(kind: str, path: str) -> bool:
    rel = (path or "").lstrip("/")
    table = GLOBAL_VITE
    if kind == "next":
        table = GLOBAL_NEXT
    elif kind == "static":
        table = GLOBAL_STATIC
    elif kind == "expo-web":
        table = GLOBAL_EXPO
    if rel in table:
        return True
    name = rel.split("/")[-1]
    if name in {"package.json", "vite.config.ts", "vite.config.js", "next.config.js", "next.config.mjs", "next.config.ts"}:
        return True
    if name.endswith((".lock", ".lockb")):
        return True
    if rel.endswith("layout.tsx") or rel.endswith("layout.jsx") or rel.endswith("_app.tsx"):
        return True
    return False


def path_matches_route(kind: str, path: str, route: str) -> bool:
    rel = (path or "").lstrip("/").lower()
    stem = normalize_route(route).strip("/").lower()
    if not stem:
        return "index" in rel or rel.endswith("app.tsx") or rel.endswith("page.tsx")
    last = stem.split("/")[-1]
    if last in rel or stem in rel:
        return True
    if kind == "next":
        # Dynamic and optional catch-all segments still map to the same pane route.
        if "[id]" in rel or "[slug]" in rel or "[...slug]" in rel or "[[...slug]]" in rel:
            parent = "/".join(stem.split("/")[:-1])
            if parent and parent in rel:
                return True
            if not parent:
                return "app/" in rel or "pages/" in rel
    return False


def invalidate_panes(
    *,
    kind: str,
    changed_paths: list[str],
    targets: list[dict[str, Any]],
    runtime_pane_ids: list[str],
) -> list[str]:
    """Deterministic path → pane invalidation. Model pane_ids are ignored."""
    if not changed_paths:
        return []
    if any(is_global_path(kind, p) for p in changed_paths):
        return list(dict.fromkeys(runtime_pane_ids or [t["pane_id"] for t in targets]))
    hit: list[str] = []
    for target in targets:
        route = str(target.get("route") or "/")
        pid = str(target.get("pane_id") or "")
        if any(path_matches_route(kind, p, route) for p in changed_paths):
            if pid:
                hit.append(pid)
    if not hit:
        return list(dict.fromkeys(runtime_pane_ids or [t["pane_id"] for t in targets]))
    return list(dict.fromkeys(hit))


def needs_restart(kind: str, changed_paths: list[str]) -> bool:
    for path in changed_paths:
        name = path.split("/")[-1].lower()
        if name in {
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
            "vite.config.ts",
            "vite.config.js",
            "next.config.js",
            "next.config.mjs",
            "next.config.ts",
            "app.json",
        }:
            return True
    return False
