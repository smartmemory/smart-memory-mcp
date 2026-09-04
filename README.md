# smart-memory-mcp

Unified SmartMemory MCP (Model Context Protocol) server — tiered tools, local + remote backends.

## Overview

MCP server exposing SmartMemory operations to MCP-compatible clients (Claude Desktop, Cursor, etc.). Implements the full memory toolset (add, search, recall, decisions, plans, anchors, code-index) and routes to either a local SmartMemory instance or a remote `smart-memory-service` API endpoint.

## Status

**Version:** 0.2.1

## Quick start

```bash
pip install -e .

# Run the server
smartmemory-mcp        # or python -m smartmemory_mcp
```

Tests:

```bash
pytest tests/ -v
```

## Hosted mode

`--hosted` serves the multi-tenant, OAuth-protected endpoint that runs at
`mcp.smartmemory.ai`. It is a different server from the stdio and `--http` modes:
every request carries its own credential, and the tool call is executed as that
caller against `smart-memory-service`. The process holds no shared API key and no
core SmartMemory instance.

```bash
SMARTMEMORY_MCP_MODE=hosted smartmemory-mcp
# or
smartmemory-mcp --hosted
```

Install with the extra, which adds the Redis-backed OAuth state store:

```bash
pip install "smartmemory-mcp[hosted]"
```

### Modes

| Mode | Command | Authentication | Tenancy |
|------|---------|----------------|---------|
| stdio | `smartmemory-mcp` | none, local process | single identity |
| http | `smartmemory-mcp --http` | none | single identity, loopback by default |
| hosted | `smartmemory-mcp --hosted` | Clerk OAuth or a SmartMemory API key | per request |

`--http` has no per-request authentication, so every caller of it acts as
whoever the process's API key belongs to. It therefore binds `127.0.0.1` and
logs a warning if asked for anything else. Set
`SMARTMEMORY_MCP_ALLOW_UNAUTH_HTTP=true` to override that deliberately. Use
`--hosted` if what you want is a server on the network.

### Environment

All of these are required in hosted mode. A missing one raises at startup,
naming the variable.

| Variable | Meaning |
|----------|---------|
| `SMARTMEMORY_API_URL` | Base URL of `smart-memory-service` |
| `MCP_PUBLIC_BASE_URL` | Public URL of this server, e.g. `https://mcp.smartmemory.ai` |
| `CLERK_DOMAIN` | Clerk instance domain |
| `CLERK_OAUTH_CLIENT_ID` | Clerk OAuth application client id |
| `CLERK_OAUTH_CLIENT_SECRET` | Clerk OAuth application client secret |
| `MCP_JWT_SIGNING_KEY` | Signs the reference tokens issued to MCP clients |
| `MCP_STATE_ENCRYPTION_KEY` | Fernet key encrypting all stored OAuth state |
| `MCP_REDIS_URL` | Redis holding OAuth state, ideally its own instance |

Optional:

| Variable | Default | Meaning |
|----------|---------|---------|
| `MCP_ALLOWED_CLIENT_REDIRECTS` | the six built-in patterns | Comma-separated client callback allowlist |
| `SMARTMEMORY_WEB_URL` | `https://app.smartmemory.ai` | Web app URL shown when an invited beta user must accept the agreement |
| `MCP_HOSTED_PORT` | `8012` | Port to bind |
| `MCP_TRUST_PROXY` | `false` | Honour `X-Forwarded-For` for rate limiting. Only true behind our own reverse proxy |

Set `MCP_JWT_SIGNING_KEY` explicitly. Left unset, the signing key is derived
from the Clerk client secret, which ties rotating that secret to invalidating
every token already issued.

### Tools

Hosted mode advertises 25 tools, an explicit allowlist rather than a tier:

`memory_ingest` `memory_search` `memory_recall` `read_around` `memory_get`
`memory_explain` `memory_recall_pack` `memory_policy_bundle` `memory_add`
`memory_update` `memory_delete` `memory_list` `memory_stats` `memory_distill`
`memory_ingest_conversation` `memory_search_by_metadata` `memory_feedback`
`code_search` `code_dead_code` `code_dependencies`
`agent_set_recall_profile` `agent_get_recall_profile` `reasoning_query_traces`
`whoami` `switch_team`

Everything else is hidden, and hidden is the default, so a tool added to a
shared module does not appear here until it is added to the allowlist. Three
kinds of tool are excluded on purpose:

- **Anything that reads or writes a filesystem path.** The container's disk is
  shared by every tenant, so `memory_export`, `memory_import`, `code_index`,
  `code_blame` and the rest are out.
- **Anything that needs a local SmartMemory instance.** Tools reaching for
  `backend._mem` or the core graph cannot work against a REST backend.
- **Destructive bulk operations**, such as `memory_clear`.

Two tools have options that cannot work here and refuse them rather than
quietly ignoring the request:

- `memory_search(cite=True)` — citation formatting lives in the `smartmemory`
  core package, which the hosted server does not ship.
- `memory_recall(session_id=…)` and `memory_recall(cite=True)` — the
  alternative recall path builds a working context through the core activation
  scorer.

`switch_team` changes the workspace for the current session only. It validates
membership against the API and never leaks into another user's calls.

### Client configuration

Claude Code:

```bash
claude mcp add --transport http smartmemory https://mcp.smartmemory.ai/mcp
```

The first tool call opens a browser for consent. Claude Code registers itself
through client id metadata, so nothing needs to be created in advance.

Claude.ai custom connector: add a connector with the URL
`https://mcp.smartmemory.ai/mcp` and complete the OAuth prompt.

Grok web: add a connector with the same URL. Grok asks for a client id rather
than registering dynamically, so paste the SmartMemory MCP OAuth application
client id when prompted.

xAI API and Grok Build take a static bearer token instead of an OAuth flow, so
give them a SmartMemory API key:

```json
{
  "type": "mcp",
  "server_url": "https://mcp.smartmemory.ai/mcp",
  "authorization": "sm_live_your_key_here"
}
```

An API key skips the OAuth flow entirely and acts as its owner in that owner's
default workspace. Legacy `sk_` keys are accepted.

## Documentation

Full SmartMemory documentation: https://docs.smartmemory.ai

## Part of SmartMemory

This is one component of the SmartMemory ecosystem. See the [main repo](https://github.com/smart-memory/smart-memory) for the broader project.
