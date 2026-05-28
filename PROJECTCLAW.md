# projectclaw

A multi-project Slack assistant. Answers a team's questions about its projects — status, decisions, action items, code — using **GitHub** and **Granola** (meeting notes) as live sources of truth, scoped per Slack channel.

The premise is simple: each Slack channel maps to one project. When someone asks the bot a question in `#project-foo`, the agent restricts every tool call to that project's GitHub repos and Granola folder. No cross-project leakage; no guessing.

**Repo:** [`gies-ai-experiments/project-claw`](https://github.com/gies-ai-experiments/project-claw)

---

## Quick start

```bash
git clone https://github.com/gies-ai-experiments/project-claw.git
cd project-claw
pip install -e .

# Authenticate gh and write ~/.projectclaw/config.json (see Setup below)
gh auth login                       # GitHub access for the bot
projectclaw onboard                 # initialize workspace
python slack-app/install_cron.py    # schedule daily + weekly standup

projectclaw gateway                 # start the bot
```

---

## What it does

Mention the bot in a project channel or DM it:

| Question | What the bot does |
|---|---|
| *"what's open right now?"* | Runs `gh pr list --repo <project's repo> --state open` and filters to non-draft PRs awaiting review |
| *"what shipped this week?"* | Combines recently-merged PRs (`gh pr list --state merged --search "repo:X merged:>=..."`) with recent Granola meeting summaries |
| *"summarize the latest meeting"* | Calls `granola_list_notes(folder_id=…)` then `granola_get_note(...)` with the project's folder ID |
| *"what did we decide about X?"* | Searches recent Granola transcripts in the project's folder + closed PR descriptions |
| *"who owns the auth refactor?"* | Cross-references Granola action items with GitHub assignees |

Answers cite their sources (`acme/repo#NUMBER` for PRs/issues, meeting title + date). On tool failure the bot returns a partial answer and surfaces the failure inline — it never silently fabricates.

If you ask in a channel that isn't mapped to a project, the bot refuses to guess: it asks you to specify which project.

---

## Architecture

```mermaid
flowchart TB
  subgraph slack["Slack workspace"]
    direction TB
    chan["#project-foo channel"]
    user["@projectclaw mention"]
  end

  subgraph nb["projectclaw process"]
    direction TB
    sch["SlackChannel"]
    bus["MessageBus"]
    loop["AgentLoop + ContextBuilder"]
    runner["AgentRunner (LLM + tools)"]
    pcskill["projectclaw skill (always-on)"]
    ghskill["github skill (always-on)"]
    rl["project_runtime_lines"]
    gtools["granola_* tools"]
    exec["exec tool (runs gh)"]
    cron["cron tool (Tue-Fri 9am + Mon 9am)"]
  end

  subgraph ext["External APIs"]
    direction TB
    ghapi["GitHub api.github.com (via gh CLI)"]
    grapi["Granola public-api.granola.ai/v1"]
  end

  user --> chan --> sch
  sch -- "InboundMessage + metadata.project" --> bus
  bus --> loop
  loop -- "system prompt + skill bodies + project runtime block" --> runner
  pcskill -.-> loop
  ghskill -.-> loop
  rl -.-> loop
  cron -- "synthesizes scheduled inbounds" --> bus
  runner -- "scoped tool calls" --> gtools
  runner -- "exec(gh ...)" --> exec
  gtools --> grapi
  exec --> ghapi
  runner -- "OutboundMessage" --> sch --> chan
```

**Five load-bearing pieces, all at the edges of the runtime:**

1. **`SlackChannel.project_map`** in `nanobot/channels/slack.py` — maps Slack channel ID → `Project` (name, GitHub repos, Granola folder).
2. **`project_runtime_lines`** in `nanobot/agent/context.py` — surfaces the resolved project to the LLM as a `[Runtime Context]` block. Without this, the skill's rules would reference a field the model can't see.
3. **The `projectclaw` skill** at `nanobot/skills/projectclaw/SKILL.md` — text-only policy (always-on) that tells the agent how to read the project scope and the *Forbidden* rule: never call a tool with a repo or folder_id outside scope.
4. **The `github` skill** at `nanobot/skills/github/SKILL.md` — also always-on. Documents the exact `gh` query patterns for open PRs, recently merged PRs, and issue activity, including the **mandatory `repo:<repo>` qualifier inside `--search`** that prevents GitHub's global-search fallback from leaking unrelated repos.
5. **The Granola tool** at `nanobot/agent/tools/granola.py` — three read-only tools (`granola_list_notes`, `granola_get_note`, `granola_list_folders`) wrapping `https://public-api.granola.ai/v1` with structured-error semantics.

GitHub access goes through the built-in `github` skill + `gh` CLI on the host. The `exec` tool runs the commands; the skill body tells the LLM the exact invocations.

The **daily + weekly standup** posts are driven by entries in `<workspace>/cron/jobs.json` registered via `slack-app/install_cron.py`. The cron synthesizes inbound messages on a schedule; the agent loop processes them identically to user-typed messages.

---

## Setup

### 1. Slack app

```bash
# Optional CLI (mostly useful for next-generation Slack platform workspaces)
curl -fsSL https://downloads.slack-edge.com/slack-cli/install.sh | bash
```

Either way, the canonical path is:

1. Open [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**
2. Pick your workspace → paste `slack-app/manifest.yaml` from this repo → **Create**
3. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`) from *OAuth & Permissions*
4. *Basic Information* → **App-Level Tokens** → generate one with `connections:write` scope → copy `xapp-…`
5. Invite `@projectclaw` (or whatever you named the bot) to each project channel: `/invite @projectclaw`

The manifest enables Socket Mode and requests only the scopes the `SlackChannel` actually uses — no over-permissioning.

### 2. GitHub access

```bash
gh auth login          # interactive; pick the github.com host
gh auth status         # verify: scopes should include 'repo' and 'read:org'
```

No token to paste into config. The bot's `exec` tool runs `gh` on the host, and `gh` resolves auth itself (macOS Keychain or `~/.config/gh/hosts.yml`). If you ever see `HTTP 401` from `gh`, run `gh auth refresh -s repo,read:org`.

For private repos behind SAML SSO, the token must be authorized for that org: `https://github.com/orgs/<ORG>/sso`.

### 3. Granola API key

Granola desktop → **Settings → Connectors → API keys → Create new key** (Business+ plan; Enterprise admins control which scopes are available). Copy the `grn_…` token. You only see it once.

### 4. OpenAI key (or any other supported provider)

Standard `sk-…` token from [platform.openai.com/api-keys](https://platform.openai.com/api-keys).

### 5. Config

Write to `~/.projectclaw/config.json` (chmod 600):

```jsonc
{
  "agents": { "defaults": { "model": "gpt-4o" } },

  "providers": {
    "openai": { "apiKey": "sk-..." }
  },

  "tools": {
    "granola": {
      "apiKey": "grn_..."
      // baseUrl and timeout have sensible defaults
    }
  },

  "channels": {
    "slack": {
      "enabled": true,
      "mode": "socket",
      "botToken": "xoxb-...",
      "appToken": "xapp-...",
      "allowFrom": ["*"],
      "replyInThread": false,   // false = top-level reply visible to channel;
                                // true  = bot replies in a thread (quieter)

      "projectMap": {
        "C0B6FAWLRA7": {
          "name": "projectclaw",
          "github": {
            "repos": [
              "gies-ai-experiments/project-claw",
              "gies-ai-experiments/Gies-Factory",
              "gies-ai-experiments/MindForum"
            ]
          },
          "granola": { "folderId": "fol_..." }
        }
      },
      "defaultProject": null
    }
  }
}
```

`projectMap` keys must be Slack channel IDs (start with `C`/`D`/`G`/`U`/`W`), not channel names — names get renamed, IDs don't. To find a channel ID: right-click the channel in Slack → *Copy link* → grab the trailing segment.

A project must declare at least one of `github` or `granola`. To find a Granola `folderId`, hit `GET /v1/folders` with your token (or run the bot and ask it: `@projectclaw list our granola folders`).

### 6. Schedule the standup (optional)

The bot can post two recurring updates to your project channel:

- **Daily standup** — Tue–Fri 9am — open PRs awaiting review + PRs merged in the last 24h.
- **Weekly summary** — Mon 9am — all PRs merged in the last 7 days, open PRs aging (oldest first), issues opened/closed in the last 7 days.

Install both with one command:

```bash
python slack-app/install_cron.py
```

The script is idempotent — re-running detects existing jobs by name and skips them. Both jobs target the Slack channel ID set in the script (`TARGET_CHAT_ID`); edit that constant to use a different channel.

The cron only fires while `projectclaw gateway` is running — if the host is asleep at 9am, that standup is silently missed (no catch-up).

### 7. Run it

```bash
projectclaw gateway
```

Watch for `Slack Socket Mode WebSocket connected (events enabled)` in the log. Then in a mapped channel:

> @projectclaw summarize the latest meeting

…and the agent should reply with a cited summary from the right Granola folder. Or for a GitHub-side test:

> @projectclaw what's open right now?

…which exercises the always-on `github` skill (uses the host's authenticated `gh` CLI).

---

## Configuration reference

### Per-channel `Project` (under `channels.slack.projectMap`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Short, used in replies and as `defaultProject` reference |
| `github.repos` | string[] | one of github/granola | `owner/repo` form; non-empty list; multiple repos OK |
| `granola.folderId` | string | one of github/granola | Granola folder ID (e.g. `fol_…`) |

### `tools.granola`

| Field | Default | Notes |
|---|---|---|
| `enable` | `true` | If `false`, all 3 granola tools become unavailable to the agent |
| `apiKey` | `""` | If empty, the tools refuse rather than hit the API |
| `baseUrl` | `https://public-api.granola.ai/v1` | Override only if Granola changes their endpoint |
| `timeout` | `30` (seconds) | Per-request httpx timeout |

### `channels.slack` additions on top of the existing Slack config

| Field | Default | Notes |
|---|---|---|
| `projectMap` | `{}` | Channel-ID-keyed dict of `Project` records |
| `defaultProject` | `null` | Name of a project to use when a message arrives in an unmapped channel. `null` means "ask the user". |
| `replyInThread` | `true` | `false` = bot reply is a top-level message visible to everyone in the channel. `true` = bot opens a thread for its reply (quieter but less discoverable). DMs and explicit threads always reply in-thread regardless. |

---

## Scoping invariant (the whole point)

Three pieces enforce it:

1. **Channel side** (`SlackChannel._resolve_inbound_project`): attaches `metadata.project` to every inbound. Lives in `nanobot/channels/slack.py`. Tests pin it: `tests/channels/test_slack_channel.py`.
2. **Agent side** (`project_runtime_lines` + the always-on projectclaw skill body): formats that metadata into a `[Runtime Context]` block the LLM can read, then instructs the LLM to use only the listed `folder_id` / repos for tool calls. Lives in `nanobot/agent/context.py` and `nanobot/skills/projectclaw/SKILL.md`.
3. **GitHub query side** (always-on github skill): documents the exact `gh` patterns and includes the **mandatory `repo:<repo>` qualifier inside `--search`**. Without that qualifier, an OR-clause search makes `gh` fall back to a global GitHub issue search and silently return results from unrelated repos. We hit this live; the regression tests in `tests/skills/test_github_skill.py` exist specifically to catch it on future prose edits.

If any piece breaks, the symptom is silent: the LLM happily calls `granola_list_notes` with no folder filter, or `gh issue list` without a `repo:` qualifier, and returns plausible-looking data from the wrong source. The tests are the canary.

---

## What's intentionally not built

- **No background ingestion / vector store / cache.** Every query hits GitHub and Granola live. If a project gets large enough that "what shipped this quarter?" times out, add a nightly index for *closed* PRs and *past* meetings — not before.
- **No GitHub webhooks / event-driven posts.** Standup is a scheduled prompt, not a notification system.
- **No multi-workspace Slack support.** One workspace per process.
- **No permission system beyond Slack channel membership.** If you can see the channel, you can ask the bot project questions. The bot's `gh` auth and Granola token determine the upper bound on what data it can actually fetch.
- **No standalone Granola MCP server.** Original design called for one; we built an in-tree tool instead. Cheaper, easier to test, an MCP server can wrap it later without changing the skill.
- **Phase 2 package rename pending.** The Python package directory is still `nanobot/` (~150 files) and the SDK class is still `Nanobot`. The user-facing CLI (`projectclaw`) and config dir (`~/.projectclaw/`) have already been renamed in Phase 1. Phase 2 would rewrite ~400 imports.

---

## Tests

```bash
# Run the projectclaw-relevant suites
pytest tests/config/test_project_map.py \
       tests/channels/test_slack_channel.py \
       tests/skills/test_projectclaw_skill.py \
       tests/skills/test_github_skill.py \
       tests/agent/test_project_runtime_lines.py \
       tests/tools/test_granola_tool.py \
       tests/cron/test_projectclaw_cron_install.py -v
```

Three invariant-pinning suites worth knowing about:

- `tests/skills/test_projectclaw_skill.py` — asserts the load-bearing policy clauses ("never call a tool with a folder_id outside scope", "if project is null, do not call any tool", "surface the failure", "never fabricate") are present in the SKILL.md. A future prose refactor that drops one fails CI.
- `tests/skills/test_github_skill.py` — 12 tests. Same idea for the github skill, **including a regression test that the `repo:<repo>` qualifier remains inside the `gh issue list --search` string** so the search-leak bug can't silently come back.
- `tests/tools/test_granola_tool.py` — 17 tests. Uses `httpx.MockTransport` to pin every request shape (URL, Bearer header, query params) and the error surface (4xx/5xx/429/transport-failure all return structured error strings instead of raising). No network in CI.

---

## File map

```
nanobot/
  agent/
    context.py                ← project_runtime_lines + ContextBuilder wiring
    tools/
      granola.py              ← 3 read-only Granola tools
  channels/
    slack.py                  ← SlackConfig.project_map + _resolve_inbound_project
  config/
    schema.py                 ← Project / GitHubProjectConfig / GranolaProjectConfig
  skills/
    projectclaw/SKILL.md      ← the always-on scoping policy
    github/SKILL.md           ← always-on; gh patterns + repo: qualifier rule

slack-app/
  manifest.yaml               ← Slack app manifest to spin up @projectclaw
  install_cron.py             ← one-shot installer for daily + weekly standup jobs

tests/
  agent/test_project_runtime_lines.py
  channels/test_slack_channel.py            (extended)
  config/test_project_map.py
  cron/test_projectclaw_cron_install.py
  skills/test_projectclaw_skill.py
  skills/test_github_skill.py
  tools/test_granola_tool.py
```

---

## License

MIT — see [LICENSE](LICENSE).
