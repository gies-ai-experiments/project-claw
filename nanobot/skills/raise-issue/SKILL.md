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
- An object `{ "name", "github": {"repos":[...]}|null, ... }` → use ONLY the
  repos listed in `github.repos`. If `github` is `null`, tell the user this
  project has no GitHub repos configured and stop.

## Step 2 — Assemble the brainstorm

Summarize the discussion from the conversation already in your context (recent
turns plus any `[Conversation Memory]` block). Do not invent details. If the
thread is too thin to make a real issue, say so and ask the user for a one-line
problem statement instead of fabricating content.

## Step 3 — Draft the issue (structured template)

Draft a **title** and a **body** with these sections:

```
## Summary
<1–3 sentences: the problem or proposal>

## Background / Context
<what the thread surfaced — motivation, constraints, prior art>

## Proposed approach
<the direction the discussion landed on; bullet steps if helpful>

## Open questions
<unresolved points / decisions still needed>

---
_Discussed in Slack._
```

## Step 4 — Resolve the target repo

- Exactly one entry in `project.github.repos` → use it.
- More than one → **ask the user which repo** before creating.
- Never use a repo that is not in `project.github.repos`.

## Step 5 — Confirm before creating

Post the full drafted **title + body** in the thread and ask for explicit
approval (e.g. "Want me to create this?").

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
