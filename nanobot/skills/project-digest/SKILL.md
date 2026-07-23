---
name: project-digest
description: "Post a plain-English daily update to the project's Slack channel: read the code changes on GitHub since yesterday — both shipped (merged) and in-progress work pushed to any branch — and summarize them in non-technical language (what changed for people, not code internals). Triggered automatically once per day; runs unattended."
metadata: {"nanobot":{"emoji":"📣","always":false,"requires":{"bins":["gh"]}}}
---

# Daily plain-English project update

Triggered once per day for one project. The trigger gives you: `project`,
`repos` (space-separated owner/name), `since` (ISO date of the last update), and
`digest_channel`. Run unattended — ask the user nothing. Your final reply IS the
Slack message.

## Step 1 — Gather what changed since yesterday

For each repo in `repos`, read the merged pull requests AND commits on **every
branch** since `since` — not just the default branch — so work pushed to feature
branches shows up too. Use ONE qualifier per query (never combine with OR, which
escapes the `--repo` scope):

```bash
# Merged pull requests (shipped)
gh pr list --repo <repo> --state merged --search "merged:>=<since>" --limit 30 \
  --json title,body,mergedAt,headRefName

# Recent commits on EVERY branch since <since>, grouped by branch
for br in $(gh api "repos/<repo>/branches" --paginate --jq '.[].name'); do
  echo "--- branch: $br"
  gh api "repos/<repo>/commits?sha=$br&since=<since>T00:00:00Z" \
    --jq '.[].commit.message | split("\n")[0]' 2>/dev/null | head -20
done
```

The same commit can show under several branches — treat each distinct change
once. Commits under the default branch (`main`/`master`) or in a merged PR are
**shipped**; a change that appears only under another branch is **in progress**.

## Step 2 — Write the update in plain English (this is your reply text)

Translate the changes into what a **non-technical** person (faculty, a
stakeholder) would understand — describe the effect, not the code.

- Say *"Added a way for students to reset their password"* — NOT *"merged PR #42:
  refactor auth middleware"*.
- **No** commit hashes, PR/issue numbers, file names, branch names, or jargon
  (refactor, middleware, endpoint, migration, dependency, etc.).
- Group related changes into one plain point; skip trivial internal churn
  (formatting, test-only, dependency bumps, CI).
- Fold in meaningful work that is still only on a feature branch as an
  *in progress* point (e.g. *"In progress: a new way to export grades"*) so
  people see what's coming — still plain English, and never name the branch.

Format for Slack (no `#` headers, no bold walls):

- One short intro line: what got worked on today, at a glance.
- 2–5 bullets, each a plain-English improvement or fix (mark upcoming ones
  *in progress*).
- **Hard limit: 100 words total.** Be concise; cut rather than exceed it.
- If nothing meaningful changed anywhere (shipped or in progress), reply with
  exactly one line: `No code updates today.`

## Forbidden

- Asking the user anything (unattended).
- Technical jargon, code identifiers, PR/issue/commit numbers, or file names.
- Exceeding 100 words.
- Inventing changes that aren't in the commits/PRs.
