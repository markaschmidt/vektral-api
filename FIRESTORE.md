"""Firestore collections used by Vektral-API domain routes (Plan B + org sharing)."""

## Collections

```text
users/{uid}
  display_name, email, avatar_url / photo_url,
  github_login, github_connected, github_connected_at,
  org_count, created_at, updated_at

users/{uid}/workspaces/{workspaceId}     # denormalized index
  workspace_id, name, created_at

users/{uid}/org_memberships/{orgId}
  role: owner|admin|member, org_name, joined_at

users/{uid}/github/connection
  connected, login, access_token_encrypted, scopes, connected_at

orgs/{orgId}
  name, slug, owner_uid, member_count, join_code?, created_at

orgs/{orgId}/members/{uid}
  role, status, email, display_name, joined_at

orgs/{orgId}/invites/{inviteId}
  email, role, status: pending|accepted, invited_by_uid, org_name, created_at

invite_index/{inviteId}                  # email lookup for pending invites
  email, role, status, org_id, org_name, invited_by_uid, created_at

org_join_codes/{CODE}                    # CODE is uppercase hex
  code, org_id, org_name, created_at

workspaces/{workspaceId}
  owner_uid, org_id (nullable for personal; set for org workspaces),
  name, description, slug,
  repo_full_name, default_branch, vektral_branch,
  storage_prefix, is_personal, thumbnail_url,
  created_at, updated_at, deleted_at (null when active)

github_oauth_pending/{state}
  state, uid, status, created_at, github_login?
```

## Access rules (API Admin SDK)

Mutations go through Vektral-API with a verified Firebase ID token
(or local `Authorization: Bearer dev:<uid>` when `ALLOW_DEV_BEARER=true`).

### Workspace ACL

| Access | Who |
|--------|-----|
| List / get / enter | Workspace `owner_uid`, **or** any member of `org_id` when set |
| Patch / soft-delete | Workspace owner, **or** org `owner`/`admin` |
| Create org workspace | Org member with `create_workspace` (owner, admin, member) |

### Org roles

| Role | Capabilities |
|------|----------------|
| `owner` | manage, invite, create_workspace, transfer (join codes) |
| `admin` | manage, invite, create_workspace |
| `member` | create_workspace |

Client SDK rules should deny direct writes to `access_token_encrypted`
and prefer Admin-only paths for workspace/org writes.

## Org sharing endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/orgs` | Caller's orgs |
| POST | `/api/orgs` | Create (caller = owner) |
| GET | `/api/orgs/{id}` | Member-gated |
| GET | `/api/orgs/{id}/members` | Member list + roles |
| POST | `/api/orgs/{id}/invites` | Owner/admin; body `{email, role}` |
| GET | `/api/orgs/{id}/invites` | Owner/admin pending/accepted |
| GET | `/api/orgs/invites/pending` | Invites for caller's email |
| POST | `/api/orgs/invites/{id}/accept` | Email must match profile |
| POST | `/api/orgs/{id}/join-code` | Owner only; rotates code |
| POST | `/api/orgs/join` | Body `{code}` → member role |
| GET/POST | `/api/orgs/{id}/workspaces` | List / create org workspaces |

## Local alternatives

- `VEKTRAL_USE_MEMORY_STORE=true` — process-local store (tests / no SA)
- `ALLOW_DEV_BEARER=true` — accept `Authorization: Bearer dev:<uid>`
  (email becomes `<uid>@dev.local` for invite matching)
