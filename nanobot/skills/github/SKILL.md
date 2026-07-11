---
name: github
description: "Interact with GitHub using the `gh` CLI. Use `gh issue`, `gh pr`, `gh run`, and `gh api` for issues, PRs, CI runs, and advanced queries."
metadata: {"nanobot":{"emoji":"🐙","always":true,"requires":{"bins":["gh"]},"install":[{"id":"brew","kind":"brew","formula":"gh","bins":["gh"],"label":"Install GitHub CLI (brew)"},{"id":"apt","kind":"apt","package":"gh","bins":["gh"],"label":"Install GitHub CLI (apt)"}]}}
---

# GitHub Skill

Use the `gh` CLI through the `exec` tool, with `--repo` scoped to the project:
- If `metadata.project.github.org` is set, `--repo` may be **any** `<org>/<name>`
  in that org; a bare repo name from the user resolves to `<org>/<name>`.
- Otherwise pass `--repo` only from `metadata.project.github.repos[]` and never
  query a repo outside that list.
If `metadata.project` is null, refuse to call gh — ask the user which
project to use instead.

## Open PRs awaiting review (the "what needs attention?" query)

```bash
gh pr list --repo <repo> --state open \
  --json number,title,url,author,createdAt,updatedAt,reviewDecision,isDraft
```

Filter the result to entries where `isDraft = false` AND
(`reviewDecision` is empty OR equals `REVIEW_REQUIRED`).

## Recently merged PRs (the "what shipped?" query)

```bash
gh pr list --repo <repo> --state merged \
  --search "repo:<repo> merged:>=$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)" \
  --json number,title,url,author,mergedAt
```

Adjust the `-v-1d` to `-v-7d` for the weekly window.

## Issues activity in the last 7 days

**CRITICAL — never combine qualifiers with `OR` in a `gh issue list
--search` clause.** An unparenthesized `OR` escapes the `--repo` scope:
`gh` runs a *global* GitHub issue search and silently returns issues
from random unrelated repos. Parentheses or a `repo:<repo>` qualifier
"fix" it on paper, but the model reliably drops them at runtime — so do
not depend on them. Use a single `updated:>=` qualifier instead, which
`--repo` scopes correctly on its own:

```bash
gh issue list --repo <repo> --state all \
  --search "updated:>=$(date -u -v-7d +%Y-%m-%d)" \
  --json number,title,url,state,createdAt,updatedAt,closedAt
```

`updated:>=` returns every issue touched in the window — opened,
commented, labeled, closed, or reopened — exactly the "what's active?"
set a status update wants.

If you specifically need *opened* vs *closed* split out, run two
separate single-qualifier queries (each stays repo-scoped). **Never**
merge them with `OR`:

```bash
gh issue list --repo <repo> --state all    --search "created:>=$(date -u -v-7d +%Y-%m-%d)" --json number,title,url,state,createdAt
gh issue list --repo <repo> --state closed --search "closed:>=$(date -u -v-7d +%Y-%m-%d)"  --json number,title,url,state,closedAt
```

## One specific PR

```bash
gh pr view <number> --repo <repo> \
  --json title,body,author,state,mergedAt,additions,deletions,url
```

## Output rules

- Cite each item as `owner/repo#NUMBER` linked to the URL `gh` returned.
- If a `gh` call fails (auth, network, rate limit), surface the failure
  inline and continue with whatever data succeeded. Partial answers
  beat silent failures.
- Never invent numbers. Never fabricate. Only report what `gh` returned.

## CI / workflow runs

Check CI status on a PR:
```bash
gh pr checks 55 --repo <repo>
```

List recent workflow runs:
```bash
gh run list --repo <repo> --limit 10
```

View a run and see which steps failed:
```bash
gh run view <run-id> --repo <repo>
```

View logs for failed steps only:
```bash
gh run view <run-id> --repo <repo> --log-failed
```

## API for advanced queries

The `gh api` command is useful for accessing data not available through
other subcommands.

```bash
gh api repos/<repo>/pulls/55 --jq '.title, .state, .user.login'
```

## JSON output filtering

Most commands support `--json` for structured output. Use `--jq` to
filter:

```bash
gh issue list --repo <repo> --json number,title --jq '.[] | "\(.number): \(.title)"'
```
