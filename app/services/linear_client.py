"""Linear GraphQL client — issues (cards), teams, organizations."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException

logger = logging.getLogger("vektral.linear")

LINEAR_GQL = "https://api.linear.app/graphql"

_VIEWER_Q = """
query VektralViewer {
  viewer {
    id
    name
    email
    displayName
    organization {
      id
      name
      urlKey
    }
  }
}
"""

_TEAMS_Q = """
query VektralTeams {
  teams(first: 50) {
    nodes {
      id
      name
      key
      description
      private
      organization {
        id
        name
        urlKey
      }
    }
  }
}
"""

_PROJECTS_Q = """
query VektralProjects {
  projects(first: 50, includeArchived: false) {
    nodes {
      id
      name
      description
      url
      state
      teams {
        nodes {
          id
          name
          key
        }
      }
    }
  }
}
"""

_ISSUE_Q = """
query VektralIssue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    url
    description
    state { id name type }
    team { id name key }
  }
}
"""

_SEARCH_Q = """
query VektralIssueSearch($term: String!, $first: Int!) {
  issueSearch(query: $term, first: $first) {
    nodes {
      id
      identifier
      title
      url
      state { id name type }
      team { id name key }
    }
  }
}
"""

_RECENT_Q = """
query VektralRecentIssues($teamId: ID, $first: Int!) {
  issues(
    first: $first
    filter: { team: { id: { eq: $teamId } }, state: { type: { nin: ["completed", "canceled"] } } }
  ) {
    nodes {
      id
      identifier
      title
      url
      state { id name type }
    }
  }
}
"""

_RECENT_ALL_Q = """
query VektralRecentIssuesAll($first: Int!) {
  issues(
    first: $first
    filter: { state: { type: { nin: ["completed", "canceled"] } } }
  ) {
    nodes {
      id
      identifier
      title
      url
      state { id name type }
      team { id name key }
    }
  }
}
"""

_STATES_Q = """
query VektralStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name type }
  }
}
"""

_CREATE_M = """
mutation VektralIssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      title
      url
      state { id name type }
      team { id name key }
    }
  }
}
"""

_CREATE_PROJECT_M = """
mutation VektralProjectCreate($input: ProjectCreateInput!) {
  projectCreate(input: $input) {
    success
    project {
      id
      name
      description
      url
      state
      teams {
        nodes {
          id
          name
          key
        }
      }
    }
  }
}
"""

_UPDATE_M = """
mutation VektralIssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue {
      id
      identifier
      title
      url
      state { id name type }
      team { id name key }
    }
  }
}
"""

_ARCHIVE_M = """
mutation VektralIssueArchive($id: String!) {
  issueArchive(id: $id) {
    success
  }
}
"""


def _auth_headers(token: str) -> dict[str, str]:
    value = token.strip()
    if not value.lower().startswith("bearer "):
        value = f"Bearer {value}"
    return {
        "Authorization": value,
        "Content-Type": "application/json",
    }


def _upstream(message: str) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={"error": "linear_upstream", "message": message},
    )


def _issue_view(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    state = raw.get("state") or {}
    team = raw.get("team") or {}
    return {
        "id": raw.get("id") or "",
        "identifier": raw.get("identifier") or "",
        "title": raw.get("title") or "",
        "url": raw.get("url") or "",
        "description": raw.get("description") or "",
        "state": (state.get("name") or "") if isinstance(state, dict) else "",
        "team_id": (team.get("id") or "") if isinstance(team, dict) else "",
        "team_key": (team.get("key") or "") if isinstance(team, dict) else "",
    }


async def graphql(
    token: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not token:
        raise HTTPException(
            status_code=400,
            detail={"error": "linear_not_connected", "message": "connect Linear first"},
        )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                LINEAR_GQL,
                headers=_auth_headers(token),
                json={"query": query, "variables": variables or {}},
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Linear GraphQL request failed")
        raise _upstream(str(exc)) from exc

    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        logger.warning("Linear GraphQL HTTP %s: %s", resp.status_code, str(data)[:300])
        raise _upstream(f"Linear HTTP {resp.status_code}")
    errors = data.get("errors")
    if errors:
        msg = str(errors[0].get("message") if isinstance(errors[0], dict) else errors[0])
        raise _upstream(msg[:400])
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise _upstream("Linear returned no data")
    return payload


async def viewer(token: str) -> dict[str, Any]:
    data = await graphql(token, _VIEWER_Q)
    raw = data.get("viewer") or {}
    org = raw.get("organization") or {}
    return {
        "id": raw.get("id") or "",
        "name": raw.get("displayName") or raw.get("name") or "",
        "email": raw.get("email") or "",
        "organization": {
            "id": org.get("id") or "",
            "name": org.get("name") or "",
            "url_key": org.get("urlKey") or "",
        }
        if isinstance(org, dict) and org.get("id")
        else None,
    }


def personal_organization() -> dict[str, Any]:
    return {
        "id": "",
        "name": "Personal",
        "url_key": "",
        "scope": "personal",
    }


async def list_organizations(token: str) -> list[dict[str, Any]]:
    info = await viewer(token)
    out = [personal_organization()]
    org = info.get("organization")
    if isinstance(org, dict) and org.get("id"):
        out.append(
            {
                "id": org["id"],
                "name": org.get("name") or "Organization",
                "url_key": org.get("url_key") or "",
                "scope": "organization",
            }
        )
    return out


def _team_view(raw: dict[str, Any]) -> dict[str, Any]:
    org = raw.get("organization") or {}
    return {
        "id": raw.get("id") or "",
        "name": raw.get("name") or "",
        "key": raw.get("key") or "",
        "description": raw.get("description") or "",
        "private": bool(raw.get("private")),
        "organization_id": org.get("id") or "" if isinstance(org, dict) else "",
        "organization_name": org.get("name") or "" if isinstance(org, dict) else "",
    }


async def list_teams(
    token: str, organization_id: str = ""
) -> list[dict[str, Any]]:
    data = await graphql(token, _TEAMS_Q)
    nodes = ((data.get("teams") or {}).get("nodes")) or []
    out: list[dict[str, Any]] = []
    org_id = (organization_id or "").strip()
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        team = _team_view(raw)
        if org_id and team.get("organization_id") != org_id:
            continue
        out.append(team)
    return out


def _project_view(raw: dict[str, Any]) -> dict[str, Any]:
    teams_raw = ((raw.get("teams") or {}).get("nodes")) or []
    teams: list[dict[str, str]] = []
    for t in teams_raw:
        if not isinstance(t, dict):
            continue
        teams.append(
            {
                "id": str(t.get("id") or ""),
                "name": str(t.get("name") or ""),
                "key": str(t.get("key") or ""),
            }
        )
    primary = teams[0] if teams else {"id": "", "name": "", "key": ""}
    return {
        "id": raw.get("id") or "",
        "name": raw.get("name") or "",
        "description": raw.get("description") or "",
        "url": raw.get("url") or "",
        "state": raw.get("state") or "",
        "team_id": primary.get("id") or "",
        "team_name": primary.get("name") or "",
        "team_key": primary.get("key") or "",
        "teams": teams,
    }


async def list_projects(
    token: str, team_id: str = ""
) -> list[dict[str, Any]]:
    """List Linear workspace projects, optionally filtered to a team."""
    data = await graphql(token, _PROJECTS_Q)
    nodes = ((data.get("projects") or {}).get("nodes")) or []
    want_team = (team_id or "").strip()
    out: list[dict[str, Any]] = []
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        project = _project_view(raw)
        if want_team:
            team_ids = {t.get("id") for t in project.get("teams") or []}
            if want_team not in team_ids:
                continue
        out.append(project)
    out.sort(key=lambda p: (p.get("name") or "").lower())
    return out


async def create_project(
    token: str,
    *,
    name: str,
    description: str = "",
    team_id: str = "",
    team_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Linear workspace project linked to one or more teams."""
    title = (name or "").strip()
    if not title:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_input", "message": "Project name is required"},
        )
    ids = [str(t).strip() for t in (team_ids or []) if str(t).strip()]
    if not ids and team_id.strip():
        ids = [team_id.strip()]
    if not ids:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "linear_team_required",
                "message": "Pick a default Linear team under Integrations.",
            },
        )
    payload: dict[str, Any] = {"name": title, "teamIds": ids}
    desc = (description or "").strip()
    if desc:
        payload["description"] = desc
    data = await graphql(token, _CREATE_PROJECT_M, {"input": payload})
    result = data.get("projectCreate") or {}
    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail={"error": "linear_upstream", "message": "Linear project creation failed"},
        )
    created = result.get("project")
    if not isinstance(created, dict):
        raise HTTPException(
            status_code=502,
            detail={"error": "linear_upstream", "message": "Linear returned no project"},
        )
    return _project_view(created)


async def recent_issues(
    token: str, team_id: str = "", first: int = 20
) -> list[dict[str, Any]]:
    first = max(1, min(int(first or 20), 50))
    if team_id:
        data = await graphql(token, _RECENT_Q, {"teamId": team_id, "first": first})
    else:
        data = await graphql(token, _RECENT_ALL_Q, {"first": first})
    nodes = ((data.get("issues") or {}).get("nodes")) or []
    return [_issue_view(n) for n in nodes if isinstance(n, dict)]


async def search_issues(token: str, term: str, first: int = 10) -> list[dict[str, Any]]:
    cleaned = (term or "").strip()
    if not cleaned:
        return []
    data = await graphql(token, _SEARCH_Q, {"term": cleaned, "first": max(1, min(first, 25))})
    nodes = ((data.get("issueSearch") or {}).get("nodes")) or []
    return [_issue_view(n) for n in nodes if isinstance(n, dict)]


async def get_issue(token: str, issue_id: str) -> dict[str, Any]:
    data = await graphql(token, _ISSUE_Q, {"id": issue_id})
    return _issue_view(data.get("issue"))


async def resolve_issue(
    token: str,
    *,
    issue_id: str = "",
    identifier: str = "",
    title: str = "",
) -> dict[str, Any]:
    key = (issue_id or identifier or "").strip()
    if key:
        try:
            found = await get_issue(token, key)
            if found.get("id"):
                return found
        except HTTPException:
            logger.info("Linear issue lookup by id/identifier missed: %s", key)
    needle = (title or identifier or "").strip()
    if needle:
        hits = await search_issues(token, needle, first=5)
        if hits:
            lowered = needle.lower()
            for hit in hits:
                if (hit.get("identifier") or "").lower() == lowered:
                    return hit
                if (hit.get("title") or "").lower() == lowered:
                    return hit
            return hits[0]
    return {}


async def resolve_state_id(token: str, team_id: str, state_name: str) -> str:
    name = (state_name or "").strip().lower()
    if not name or not team_id:
        return ""
    data = await graphql(token, _STATES_Q, {"teamId": team_id})
    nodes = ((data.get("workflowStates") or {}).get("nodes")) or []
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        if (raw.get("name") or "").strip().lower() == name:
            return str(raw.get("id") or "")
        if (raw.get("type") or "").strip().lower() == name:
            return str(raw.get("id") or "")
    return ""


async def create_issue(
    token: str,
    *,
    team_id: str,
    title: str,
    description: str = "",
    priority: int | None = None,
    assignee_id: str = "",
    state_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"teamId": team_id, "title": title}
    if description:
        payload["description"] = description
    if priority is not None:
        payload["priority"] = int(priority)
    if assignee_id:
        payload["assigneeId"] = assignee_id
    if state_id:
        payload["stateId"] = state_id
    data = await graphql(token, _CREATE_M, {"input": payload})
    created = (data.get("issueCreate") or {}).get("issue")
    return _issue_view(created)


async def update_issue(
    token: str,
    issue_id: str,
    *,
    title: str = "",
    description: str | None = None,
    priority: int | None = None,
    assignee_id: str = "",
    state_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if title:
        payload["title"] = title
    if description is not None:
        payload["description"] = description
    if priority is not None:
        payload["priority"] = int(priority)
    if assignee_id:
        payload["assigneeId"] = assignee_id
    if state_id:
        payload["stateId"] = state_id
    if not payload:
        return await get_issue(token, issue_id)
    data = await graphql(token, _UPDATE_M, {"id": issue_id, "input": payload})
    updated = (data.get("issueUpdate") or {}).get("issue")
    return _issue_view(updated)


async def archive_issue(token: str, issue_id: str) -> bool:
    data = await graphql(token, _ARCHIVE_M, {"id": issue_id})
    return bool((data.get("issueArchive") or {}).get("success"))
