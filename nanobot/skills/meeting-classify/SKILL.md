---
name: meeting-classify
description: "Classify one meeting note by project. Triggered automatically for each new note in the shared meetings folder: read the note, decide which of the known projects it discusses, and return a per-project breakdown as JSON. Runs unattended; a human approves the result before anything is posted."
metadata: {"nanobot":{"emoji":"🗂️","always":false}}
---

# Meeting classifier

You are triggered automatically for one new Granola meeting note. The trigger
gives you the `note_id`, the note's title/summary/transcript, and `projects` — a
JSON list of `{name, description}` for every known project. A single meeting
usually discusses several projects. Run unattended — ask nothing.

## Step 1 — Read the note

Use the `note_id`, title, summary, and transcript provided in the trigger. If a
full transcript is not present, call `granola_get_note(note_id)`.

## Step 2 — Classify per project

For each project in `projects`, decide whether the meeting actually discusses it,
matching the discussion against that project's `name` and `description`. Include a
project ONLY when there is real, specific discussion of it — do not stretch a
vague mention into a match. A meeting may map to several projects, one, or none.
You may also include a distinct project discussed as newly formed; this is the
only case where `isNewProject` is true.

## Step 3 — Reply with JSON ONLY

Your entire reply MUST be a single JSON array and nothing else — no prose, no
markdown fences. One object per project that was genuinely discussed:

```json
[
  {
    "project": "<exact known project name, or stable name for a newly formed project>",
    "isNewProject": false,
    "displayName": "",
    "description": "",
    "channelSlug": "",
    "lead": null,
    "summary": "<2-4 sentence summary of what was discussed for THIS project>",
    "tasks": [
      {
        "id": "<stable unique ID within this project draft>",
        "title": "<action item>",
        "owner": {"name": "<person name>", "email": "<person email>"},
        "collaborators": [
          {"name": "<person name>", "email": "<person email>"}
        ],
        "dueOn": "2026-07-24",
        "dueOnSource": "meeting"
      }
    ]
  }
]
```

Rules:
- Use exact known project names verbatim from the `projects` list.
- Use `isNewProject: true` only for a distinct project discussed as newly formed.
  For a new project, provide a nonempty `displayName`, `description`,
  `channelSlug`, and exactly one `lead`; otherwise use the empty/null defaults
  shown above.
- `summary` covers only the slice relevant to that project.
- Include name+email for every person. Never infer a person who is not explicit.
- Each task has one primary owner or `null`. Put additional responsible people
  in `collaborators`; the list may be empty.
- Only emit a due date stated in the note. When present, set `dueOnSource` to
  `"meeting"`; otherwise set both `dueOn` and `dueOnSource` to `null`.
- `tasks` may be empty.
- If no project was genuinely discussed, reply with exactly `[]`.

## Forbidden

- Any text outside the JSON array (a human parser reads your reply directly).
- Inventing a project not in the `projects` list unless the note explicitly
  discusses that distinct project as newly formed.
- Forcing a match when the discussion is only a passing mention.
- Inferring assignees, collaborators, or due dates.
