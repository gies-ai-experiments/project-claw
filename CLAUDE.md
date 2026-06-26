# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, MoChat, MS Teams, WebSocket). `manager.py` discovers and coordinates them. Channels are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, project-context search, MindForum room creation/invites (`mindforum.py`), and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins. External-service tools (e.g. MindForum, memory) follow an inert-until-configured `active` gate on their config (e.g. `tools.mindforum` needs both `host` and `api_key`).
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **Bridge** (`bridge/`): TypeScript services (e.g. WhatsApp bridge) bundled into the wheel via `pyproject.toml` `force-include`.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/heartbeat/`): Periodic agent wake-up service for scheduled task checking.
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (long-goal, cron, github, image-generation, mindforum, raise-issue, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Branching Strategy

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full two-branch model (`main` vs `nightly`) and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.

## Completed Work

### 2026-06-26 — MindForum tool, raise-issue skill, project-resolution follow-ons, Slack reply routing
- **MindForum integration:** new tool module `nanobot/agent/tools/mindforum.py` (`create_mindforum_room`, `invite_to_mindforum_room`) — a thin Bearer-auth wrapper over the MindForum v1 REST API. Config at `tools.mindforum` (`host` + `api_key`), inert until both are set (`active` gate mirroring `MemoryConfig.active`). Tools never raise on HTTP errors (return a short structured error string instead) and never leak the `api_key`. Results include the room link (`{host}/room/{id}`). Paired with the `mindforum` skill (`nanobot/skills/mindforum/`).
- **raise-issue skill** (`nanobot/skills/raise-issue/`, `always:true`, requires `gh`): turns a Slack brainstorm thread into a GitHub issue — drafts a structured issue, confirms with the user, then creates it scoped to the project's repos. Reads `metadata.project.github.repos` (same project contract as other skills) and refuses when the channel has no mapped project / no repos.
- **Project resolution (Phase-1 follow-ons):** `ProjectResolver` now surfaces `github_repos` and `granola_folder_id` into `meta["project"]` (joined from `project_registry`, including on the lock-cache path) so skills/tools can scope to the project. Added a **per-channel default project**: migration `0004_project_default_channels.sql` adds `default_channels`; a channel with exactly one allowed project resolves to it unnamed (restores legacy one-project-per-channel behavior), and a configured default resolves *softly* (not locked) so a later explicit `[project]` can still claim the thread. `registry_sync` hydrates `default_channels` from per-channel `default_project` config.
- **Slack reply routing + "thinking…" placeholder:** Slack replies now post to the **channel by default**; they only thread when the user opts in with `/brainstorm` or is already inside an existing thread (decided at ingest via `reply_thread_ts` in slack meta). The session/memory key still keys on `thread_ts`, so project resolution and conversation memory are unaffected by where the reply lands. While the agent works, Slack posts a transient `thinking…` placeholder (`thinking_message`/`thinking_text` config) that is edited in place into the first reply chunk (or cleaned up if the reply never claims it).
- **github skill gotcha:** never combine qualifiers with `OR` in a `gh issue list --search` clause — an unparenthesized `OR` escapes the `--repo` scope and silently runs a global search. Use a single `updated:>=` qualifier (which `--repo` scopes correctly), or two separate single-qualifier queries; documented in `nanobot/skills/github/SKILL.md`.

### 2026-05-28 — project-context-db Phase 1 (`feat/project-context-db`)
- **Built:** a Postgres+pgvector three-layer memory. L1 = raw per-thread conversation log (`nanobot/store/`: `pool.py`, `migrations.py` + `migrations/*.sql`, `message_store.py`); project resolution (`nanobot/channels/project_resolver.py`, sticky per-thread, multi-project per channel); L2 distilled facts table + `project_context_search` tool (`nanobot/agent/tools/project_context.py`, FTS + optional hybrid pgvector); `registry_sync` to hydrate the registry from config; `embeddings.py` (OpenAIEmbedder + hybrid path, used in Phase 2).
- **Wiring:** all memory work is centralized in `AgentLoop` (channel-agnostic) — it resolves the project, persists inbound/assistant/tool rows, and injects the L1 `[Conversation Memory]` block per turn. Gateway boot (`_setup_agent_memory` in `cli/commands.py`) creates the pool, runs migrations + registry sync, and attaches the store — all gated by `cfg.memory.active`.
- **Key decisions:** project registry is **channel-local** on `SlackConfig` (no `NanobotConfig`); `MemoryConfig.enabled` defaults true but `active` requires a `dsn` (inert otherwise); changes are additive/backward-compatible (`AgentRunner` untouched; persistence at the loop level, not the runner); every memory op is guarded so it can never break a live turn.
- **Conventions:** all store SQL in numbered `nanobot/store/migrations/*.sql` (bundled into the wheel via hatch `include`); migration runner sets `search_path TO "<schema>", public` so the pgvector type resolves; tests use the repo-root `tests/conftest.py` `pg_schema` throwaway-schema fixture and skip when no Postgres is reachable.
- **Status:** Tasks 1–15 done, all tested (full suite 3803 passed / 2 skipped). **Pending: live Slack smoke test** (set `memory.dsn`, run the docker-compose postgres) and **Phase 2** (Tasks 16–24: nightly distiller, L3 learnings, `learning_search`/`remember_learning`, cron).
