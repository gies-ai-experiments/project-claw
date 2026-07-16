---
name: project-digest
description: "Post a daily project digest to the project's Slack channel: compare what the meetings/brainstorm planned (project memory) against what actually shipped in GitHub. Triggered automatically once per day; runs unattended."
metadata: {"nanobot":{"emoji":"📊","always":false,"requires":{"bins":["gh"]}}}
---

# Daily project digest

Triggered once per day for one project. The trigger gives you: `project`,
`repos` (space-separated owner/name), `since` (ISO date of the last digest), and
`digest_channel`. Run unattended — ask the user nothing. Your final reply IS the
Slack message.

## Step 1 — The plan side (project memory)

Call `project_context_search` for this project with queries like "open action
items", "decisions", "open questions". These are the distilled facts from
meetings/brainstorms and chat. If it returns nothing, say so plainly — do not
invent plans.

## Step 2 — The actual side (live GitHub)

For each repo in `repos`, gather activity since `since` (use ONE qualifier per
query — never combine with OR, which escapes `--repo` scope):

```bash
gh pr list    --repo <repo> --state merged --search "merged:>=<since>"  --limit 30
gh issue list --repo <repo> --state closed --search "closed:>=<since>"  --limit 30
gh issue list --repo <repo> --state all    --search "created:>=<since>" --limit 30
```

## Step 3 — Align and write the digest (this is your reply text)

Keep it tight and Slack-formatted (no `#` headers, no bold):

- One line: project name + date range.
- `Shipped:` 1–4 bullets of merged PRs / closed issues that match a planned action.
- `Planned, no movement:` open actions/decisions with no matching GitHub activity.
- `Unplanned:` notable GitHub activity with no matching plan.
- If a section is empty, omit it. If both sides are empty, post one line: "No activity to report."

## Forbidden

- Asking the user anything (unattended).
- Combining `gh` search qualifiers with OR.
- Inventing plans, PRs, or issue numbers.
