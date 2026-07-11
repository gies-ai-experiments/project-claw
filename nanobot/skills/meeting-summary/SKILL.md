---
name: meeting-summary
description: "Summarize a just-finished Granola meeting and assign its action items. Triggered automatically when a new meeting note lands: read the note, post a short summary to Slack with per-person tasks (@mentions), and open one GitHub issue per task, assigned via the project roster."
metadata: {"nanobot":{"emoji":"🗒️","always":false,"requires":{"bins":["gh"]}}}
---

# Meeting summary & task assignment

You are triggered automatically when a new Granola meeting note lands for a
project. The trigger message gives you everything: the `note_id`, the project
name, the Granola `folder_id`, the GitHub `issue_repo`, and a `roster` — a JSON
list of `{email, name, slackId, githubUsername}`. Use only what the trigger
provides; do not ask the user anything (this runs unattended).

## Step 1 — Read the note

Call `granola_get_note(note_id)`. Use its title, summary, attendees, and
transcript. If it returns an error, stop and reply with the error plainly —
never fabricate a summary.

## Step 2 — Write the Slack summary (this is your reply text)

Your final reply IS the Slack message. Keep it tight:

- One line: the meeting title + date.
- 2–4 bullets of what was decided / discussed.
- A `Tasks:` section, one line per action item: `• <@SLACKID> — <task>`.

Map each task owner to the roster by email (fall back to name, case-insensitive).
When someone is on the roster, mention them as `<@theirSlackId>`. When they are
**not** on the roster, use their plain name — no mention. Do not invent owners:
if the transcript does not clearly assign a task, list it under `• (unassigned) — <task>`.

Do not use markdown headers (`#`) or bold — Slack renders them as walls of bold.

## Step 3 — Open a GitHub issue per task (idempotent)

Only if `issue_repo` is provided. For each action item, before creating,
check it does not already exist by grepping the repo for this note's marker:

```bash
gh issue list --repo <issue_repo> --search "granola-note:<note_id>" --state all --limit 50
```

If that returns any issue, the note was already processed — **skip all creation**
and just post the Slack summary. Otherwise create one issue per task:

```bash
gh issue create --repo <issue_repo> \
  --title "<short task title>" \
  --assignee "<githubUsername or omit the flag if unmapped>" \
  --body "$(cat <<'EOF'
<one-line task description>

From meeting: <title> (<date>).
<!-- granola-note:<note_id> -->
EOF
)"
```

The `<!-- granola-note:<note_id> -->` marker is what makes re-runs safe — always
include it, verbatim, in every issue body. Omit `--assignee` entirely for
unmapped owners; never guess a GitHub username.

## Step 4 — Reply

Post the Slack summary from Step 2. If some `gh` calls failed, add one short line
noting which tasks were not filed — do not retry blindly, do not fabricate issue
URLs.

## Forbidden

- Asking the user anything (this is unattended).
- Creating issues without the `granola-note:` marker.
- Guessing task owners, GitHub usernames, or Slack IDs not in the roster.
- Using a repo other than the trigger's `issue_repo`.
