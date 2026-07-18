---
name: project-digest
description: "Post a plain-English daily update to the project's Slack channel: read the code changes shipped to GitHub since yesterday and summarize them in non-technical language (what changed for people, not code internals). Triggered automatically once per day; runs unattended."
metadata: {"nanobot":{"emoji":"📣","always":false,"requires":{"bins":["gh"]}}}
---

# Daily plain-English project update

Triggered once per day for one project. The trigger gives you: `project`,
`repos` (space-separated owner/name), `since` (ISO date of the last update), and
`digest_channel`. Run unattended — ask the user nothing. Your final reply IS the
Slack message.

## Step 1 — Gather what shipped since yesterday

For each repo in `repos`, read the merged pull requests and commits since
`since` (use ONE qualifier per query — never combine with OR, which escapes the
`--repo` scope):

```bash
gh pr list  --repo <repo> --state merged --search "merged:>=<since>" --limit 30 \
  --json title,body,mergedAt
gh api "repos/<repo>/commits?since=<since>T00:00:00Z" --jq '.[].commit.message' 2>/dev/null | head -50
```

## Step 2 — Write the update in plain English (this is your reply text)

Translate the changes into what a **non-technical** person (faculty, a
stakeholder) would understand — describe the effect, not the code.

- Say *"Added a way for students to reset their password"* — NOT *"merged PR #42:
  refactor auth middleware"*.
- **No** commit hashes, PR/issue numbers, file names, branch names, or jargon
  (refactor, middleware, endpoint, migration, dependency, etc.).
- Group related changes into one plain point; skip trivial internal churn
  (formatting, test-only, dependency bumps, CI).

Format for Slack (no `#` headers, no bold walls):

- One short intro line: what got worked on today, at a glance.
- 2–5 bullets, each a plain-English improvement or fix.
- **Hard limit: 100 words total.** Be concise; cut rather than exceed it.
- If nothing meaningful shipped, reply with exactly one line:
  `No code updates today.`

## Forbidden

- Asking the user anything (unattended).
- Technical jargon, code identifiers, PR/issue/commit numbers, or file names.
- Exceeding 100 words.
- Inventing changes that aren't in the commits/PRs.
