---
name: raise-issue
description: "Turn a Slack brainstorming thread into a detailed GitHub issue: draft a structured issue, confirm with the user, then create it with `gh issue create` scoped to the project's repos."
metadata: {"nanobot":{"emoji":"📝","always":true,"requires":{"bins":["gh"]}}}
---

# Raise GitHub Issue from a Brainstorm

Use this skill when the user asks, in natural language, to capture the current
Slack discussion as a GitHub issue — e.g. "raise an issue for this",
"file/open a GitHub issue", "turn this into a ticket", "make an issue out of
what we discussed".

This skill owns *what the issue says* and the *confirmation gate*. It defers the
actual `gh` call to the **github** skill's patterns.

## Step 1 — Check project scope

Read `metadata.project` (same contract the `projectclaw` skill uses):

- `null` → **Do not create anything.** Reply asking the user to run this in a
  project-mapped channel or to name the project explicitly.
- An object `{ "name", "github": {"repos":[...], "org": "<org>"?}|null, ... }`:
  - If `github.org` is set, the project allows **any repo in that org**. A bare
    repo name from the user (no `owner/`) resolves to `<org>/<name>`.
  - Else use **only** the `owner/name` repos listed in `github.repos`.
  - If `github` is `null` (and no `org`), tell the user this project has no GitHub
    repos configured and stop.

## Step 2 — Assemble the brainstorm

Summarize the discussion from the conversation already in your context (recent
turns plus any `[Conversation Memory]` block). Do not invent details. If the
thread is too thin to make a real issue, say so and ask the user for a one-line
problem statement instead of fabricating content.

## Step 3 — Draft the issue (keep it clear, not heavily formatted)

Produce two things: a **title** and a **body**.

- **Title:** one clear, specific line naming the issue (no markdown, no
  trailing period).
- **Body:** a clear plain-language description — 2–4 sentences on the problem or
  proposal and why it matters — followed only if useful by a short `-` bulleted
  list of key points and a short list of any open questions. End with a single
  line: `Discussed in Slack.`

**Do not** use markdown headers (`#`, `##`) or bold (`**…**`) in the body. Slack
renders every header and bold run as bold, so a header-heavy draft shows up as a
wall of bold text. Lead with the title, keep the description readable, use plain
`-` bullets sparingly. Example shape:

```
<clear description of the problem/proposal, 2–4 sentences>

Key points:
- <point>
- <point>

Open questions:
- <question>

Discussed in Slack.
```

Omit the "Key points" / "Open questions" lists entirely when there's nothing
substantive to put in them — don't pad with empty sections.

## Step 4 — Resolve the target repo

- If `project.github.org` is set: use the repo the user named as `<org>/<name>`
  (accept a bare name and prefix the org). Any repo in the org is allowed; if the
  user named none, ask which repo.
- Else, with `project.github.repos`: exactly one entry → use it; more than one →
  **ask the user which repo**; never use a repo not in the list.

## Step 5 — Confirm before creating

Post the draft in the thread so it's easy to scan: the title on its own first
line (e.g. `Title: <title>`), a blank line, then the body. Keep the formatting
light per Step 3. Ask for explicit approval (e.g. "Want me to create this?").

- If the user asks for changes, re-draft and confirm again.
- If the user declines, drop it — create nothing.
- Only proceed to Step 6 after a clear yes.

## Step 6 — Create the issue

After approval, create it with the **github** skill via the `exec` tool. Pass
the body through a quoted heredoc so markdown, backticks, and newlines survive:

```bash
gh issue create --repo <owner/name> \
  --title "<title>" \
  --body "$(cat <<'EOF'
<body>
EOF
)"
```

## Step 7 — Reply

Post the issue URL `gh` returned. If `gh` fails (not authenticated, rate limit,
network), surface the error inline and stop — do not retry blindly and do not
fabricate a URL.
