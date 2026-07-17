# Asana meeting-task sync and approved project provisioning

**Date:** 2026-07-17
**Status:** Design approved; pending written-spec review

## Goal

Connect ProjectClaw's existing meeting-classifier approval flow to one Asana
workspace and one Slack workspace.

For an existing project, Sakshi reviews and edits the meeting summary, action
items, owners, collaborators, and explicit due dates in Slack. Approval creates
one unassigned parent task in the project's existing Asana project, creates the
approved action items as subtasks, and posts the approved result with an Asana
link in the project's Slack channel.

When a meeting proposes a new project, Sakshi reviews the project name,
description, lead, permanent Slack channel, participants, and initial tasks.
Nothing external is created until she approves. Approval then provisions the
Asana project, permanent Slack channel, memberships, durable ProjectClaw
mapping, and initial meeting tasks.

The workflow must survive process restarts and partial Slack or Asana failures
without duplicating external resources.

## Product decisions

- One ProjectClaw deployment connects to one Slack workspace and one Asana
  workspace.
- Existing ProjectClaw projects map to existing Asana projects by Asana project
  GID.
- The only approver is Sakshi, identified by the existing
  `gateway.meetingClassifier.adminSlackId` setting.
- ProjectClaw uses a personal access token belonging to Sakshi. The token is an
  environment-backed deployment secret, not database data.
- Each approved meeting/project slice creates one unassigned parent Asana task.
- Every approved action item becomes a subtask.
- A subtask has one primary owner. If an action names more people, the remaining
  people become Asana followers/collaborators.
- Owners, collaborators, and due dates are used only when explicitly present in
  the meeting or explicitly entered by Sakshi. The model does not invent them.
- People are joined across ProjectClaw, Slack, and Asana by normalized email.
- Missing Slack or Asana identity matches block approval until Sakshi corrects
  or removes the person.
- Sakshi reviews tasks with inline Slack controls: **Edit**, **Remove**,
  **Add task**, and **Approve all**. Editing a task opens a focused Slack modal.
- A new project is proposed automatically but provisioned only after explicit
  approval.
- Each new project has exactly one lead.
- The lead becomes the Asana project owner and is invited to the permanent Slack
  project channel.
- The lead receives only tasks that explicitly name them; the parent meeting task
  remains unassigned.
- All approved task participants are added to the Asana project and invited to
  the Slack project channel.
- New Slack project channels are public workspace channels in v1 and remain
  active for future meetings and project updates.
- New Asana projects use the `private` privacy setting; the approved lead and
  participants are added as members.
- Successful external resources are retained after partial failure. Recovery
  resumes the workflow; it never deletes resources as rollback.

## Scope decomposition

This feature contains two connected paths implemented on one shared foundation:

1. **Existing-project meeting sync:** approve a project slice, create Asana
   parent/subtasks, and post the linked result to the existing Slack channel.
2. **New-project provisioning:** approve a proposed project, create its Asana
   project and Slack channel, register it dynamically, then run the same meeting
   task sync.

Both paths use the same identity directory, approval snapshots, external API
adapters, and resumable provisioning engine. They belong in one design because
new-project provisioning must finish by executing the existing-project task
path.

## Architecture

```text
Granola shared-folder poller
  -> meeting classifier
  -> approval coordinator
       -> Postgres project/identity/approval registry
       -> Sakshi Slack review UI
  -> approved immutable snapshot
  -> resumable provisioning engine
       -> Asana adapter
       -> Slack adapter
       -> runtime project registry
  -> linked project-channel announcement
```

### Approval coordinator

The coordinator owns draft validation, Slack preview rendering, revision
tracking, edit/add/remove actions, and the transition from an editable draft to
an immutable approved snapshot. It does not call Asana or provision channels
while the draft is editable.

The existing meeting classifier is extended from action strings to structured
draft data:

```json
{
  "project": "projectclaw",
  "isNewProject": false,
  "summary": "The team agreed to connect approved meeting work to Asana.",
  "lead": null,
  "tasks": [
    {
      "title": "Implement resumable Asana task creation",
      "owner": {"name": "Ashleyn", "email": "ashleyn@example.edu"},
      "collaborators": [
        {"name": "Jordan", "email": "jordan@example.edu"}
      ],
      "dueOn": "2026-07-24"
    }
  ]
}
```

For a new project, `isNewProject` is true and the draft also contains a proposed
project key, display name, description, lead identity, and normalized channel
slug. The classifier may propose these fields, but Sakshi can edit all of them
before approval.

### Runtime project registry

Static `channels.slack.projects` configuration remains a seed source. At gateway
startup, configured projects are upserted into Postgres. Dynamic projects live
in Postgres and are merged into the Slack channel resolver and meeting
classifier registry at runtime.

This removes the current dependency on a baked config redeploy for newly created
projects. Static projects remain authoritative for their explicitly configured
GitHub and Granola values; runtime-managed Slack, Asana, lead, membership, and
lifecycle fields are database-owned.

The runtime registry must start whenever the Asana integration is enabled. It is
not gated by `memory.active`; conversation memory may remain disabled in
production.

### Asana adapter

The adapter is a small async HTTP client with typed operations for:

- validating the configured workspace/team and Sakshi's token;
- listing and resolving workspace users by exact normalized email;
- finding and validating existing mapped projects;
- creating a project in the configured workspace/team;
- assigning the approved lead as project owner;
- adding approved participants as project members;
- creating the unassigned parent meeting task in a project;
- creating subtasks with one assignee and an optional explicit `due_on` date;
- adding additional named participants as task followers; and
- returning permalink URLs for Slack announcements.

Asana tasks support one assignee and multiple followers, which is why
multi-person meeting actions map to one primary owner plus collaborators. See
[Asana object hierarchy](https://developers.asana.com/docs/object-hierarchy).

Asana custom external data is not used because it requires OAuth and this design
uses Sakshi's personal access token. Instead, ProjectClaw writes deterministic,
human-visible provenance markers in project/task notes and reconciles them after
ambiguous timeouts. See
[Asana custom external data](https://developers.asana.com/jd/docs/custom-external-data).

### Slack adapter

The existing Slack channel gains typed operations for:

- resolving workspace users by exact normalized email;
- finding or creating a public project channel by normalized slug;
- inviting the approved lead and task participants;
- setting a ProjectClaw provenance marker in the channel topic/purpose;
- opening and updating Sakshi's approval DM and task-edit modals; and
- posting the final linked meeting summary to the project channel.

Channel creation uses `conversations.create`, and membership uses
`conversations.invite`. The app that creates a channel is already a member of it,
which satisfies the invitation requirement. See
[Slack channel creation](https://api.slack.com/methods/conversations.create) and
[Slack channel invitations](https://api.slack.com/methods/conversations.invite/test).

### Resumable provisioning engine

The engine executes named, durable steps. Each step is idempotent and records its
state and external resource identifier before the next step starts.

For an existing project:

```text
validate_mapped_project
  -> ensure_asana_members
  -> create_or_find_parent_task
  -> create_or_find_subtasks
  -> ensure_task_followers
  -> post_or_find_slack_announcement
  -> complete
```

For a new project:

```text
reserve_project_key_and_channel_slug
  -> create_or_find_asana_project
  -> assign_project_lead
  -> ensure_asana_members
  -> create_or_find_slack_channel
  -> ensure_slack_members
  -> activate_runtime_project_mapping
  -> create_or_find_parent_task
  -> create_or_find_subtasks
  -> ensure_task_followers
  -> post_or_find_slack_announcement
  -> complete
```

The job stores external IDs immediately. If a request times out after the remote
service may have succeeded but before the ID is saved, the next attempt performs
reconciliation before creating anything:

- Slack channels are found by reserved slug and verified by the ProjectClaw
  marker in their topic/purpose.
- Asana projects are enumerated within the configured workspace/team; exact-name
  candidates are fetched and verified by the ProjectClaw marker in their notes.
- Parent tasks and subtasks are found beneath their known project/parent and
  verified by deterministic approval/task markers in their notes.
- If reconciliation finds zero matches, creation may retry. If it finds an
  ambiguous or conflicting match, the job pauses for Sakshi instead of risking a
  duplicate.

## Slack approval experience

### Draft header

The header identifies the meeting, target project, existing/new status, summary,
and revision. A new-project header also shows project display name, key, public
channel slug, description, and lead.

### Task controls

Each task row shows:

- title;
- primary owner name and email, or `Unassigned`;
- collaborator names and emails;
- explicit due date, or `No due date`; and
- **Edit** and **Remove** buttons.

**Edit** opens a modal for the title, primary owner, collaborators, and due date.
**Add task** opens the same modal with empty fields. The approval preview is
rendered in pages of at most ten task rows so large meetings stay within Slack
Block Kit limits; a stable header message contains **Add task**, **Skip project**,
and **Approve all**.

Every mutation increments the draft revision and revalidates the whole draft.
Actions from an older revision are rejected with an ephemeral message directing
Sakshi to the current preview.

### Approval validation

**Approve all** is accepted only when:

- the click comes from Sakshi's configured Slack user ID;
- the revision is current and the draft is still pending;
- the project mapping exists, or the new-project proposal has a unique key and
  channel slug;
- a new project has exactly one lead;
- every person has a syntactically valid email and resolves to exactly one active
  Slack user and one accessible Asana user;
- every due date is a valid ISO calendar date explicitly sourced or entered;
- every task has a non-empty title; and
- the draft contains at least one task.

On success, the editable draft is copied into an immutable approved snapshot,
the approval actor/time are recorded, and one provisioning job is created in the
same database transaction.

### Completion and failure messages

The project channel receives a message only after required Asana membership,
tasks, followers, and Slack membership steps succeed. The message contains:

- meeting title and project name;
- approved summary;
- every approved task with owner, collaborators, and explicit due date;
- a link to the parent Asana task; and
- a note that the work was approved by Sakshi.

A permanent provisioning failure updates Sakshi's DM with the failed step, a
sanitized error, completed resources, and a **Retry** button. Retry resumes the
same approved snapshot. The snapshot cannot be edited after approval; corrections
after completion are normal Asana edits. Reprocessing the same meeting/project
slice as a new approval is outside v1.

## Durable data model

### General database configuration

Add a top-level `database` configuration with a `dsn`. It owns the shared
Postgres connection and migrations used by the runtime project registry and
provisioning engine.

`memory.dsn` remains supported as a backward-compatible fallback for existing
deployments. Memory activation remains independently controlled by
`memory.enabled`; configuring `database.dsn` for Asana must not turn on
conversation-memory injection or the distiller.

### Project registry extensions

Extend `project_registry` without removing its existing GitHub, Granola, and
channel fields:

- `display_name`
- `description`
- `lead_email`
- `slack_channel_id` (unique when non-null)
- `asana_project_gid` (unique when non-null)
- `lifecycle_status`: `provisioning`, `active`, or `needs_attention`
- `source`: `static_config` or `dynamic`
- `created_by_slack_id`
- `created_at` and `updated_at`

An Asana mapping becomes a first-class `Project` source. The current validation
rule changes from “GitHub or Granola required” to “at least one of GitHub,
Granola, or Asana required,” allowing a newly approved Asana-only project to be
usable immediately.

### Identity directory

Add `identity_directory`:

- `email_normalized` primary key
- `display_name`
- `slack_user_id`
- `asana_user_gid`
- `verified_at`

Email comparison trims whitespace and lowercases the complete address. Automatic
matching requires one exact result in each service. Cached matches are rechecked
when older than 24 hours or when an API operation reports an inaccessible user.

Add `project_membership` keyed by `(project_id, email_normalized)` with role
`lead` or `participant`.

### Approvals

Add `meeting_approval`:

- internal approval UUID;
- Granola note ID and meeting title/date;
- existing project ID or proposed project key;
- integer revision;
- status: `draft`, `pending`, `skipped`, `approved`, `provisioning`, `complete`,
  or `needs_attention`;
- editable draft JSONB;
- immutable approved snapshot JSONB;
- approver Slack ID and approval timestamp; and
- created/updated timestamps.

The unique logical key is `(note_id, project_or_proposed_key)`. Only one approved
revision may exist for that logical key.

### Provisioning jobs and steps

Add `provisioning_job` with one row per approved snapshot and
`provisioning_step` keyed by `(job_id, step_name)`. Step rows store state,
attempt count, deterministic idempotency key, external resource GID/ID, sanitized
last error, and timestamps.

The approved snapshot and step records are the recovery source of truth. The
legacy JSON approval store remains readable for unresolved legacy approvals, but
all new Asana-enabled approvals use Postgres.

## Configuration surface

```jsonc
{
  "database": {
    "dsn": "${PROJECTCLAW_DATABASE_DSN}"
  },
  "integrations": {
    "asana": {
      "enabled": true,
      "accessToken": "${ASANA_ACCESS_TOKEN}",
      "workspaceGid": "1234567890",
      "teamGid": "2345678901",
      "baseUrl": "https://app.asana.com/api/1.0",
      "newProjectPrivacy": "private"
    }
  },
  "gateway": {
    "meetingClassifier": {
      "enabled": true,
      "folderId": "fol_shared",
      "adminSlackId": "U_SAKSHI",
      "intervalSeconds": 900
    }
  },
  "channels": {
    "slack": {
      "projects": {
        "projectclaw": {
          "name": "projectclaw",
          "description": "ProjectClaw development",
          "channel": "C_PROJECTCLAW",
          "asana": {"projectGid": "3456789012"},
          "people": [
            {
              "name": "Ashleyn",
              "email": "ashleyn@example.edu",
              "slackId": "U_ASHLEYN",
              "asanaUserGid": "4567890123"
            }
          ]
        }
      }
    }
  }
}
```

The example values are illustrative, not deployable credentials or production
identifiers. `${...}` placeholders continue to resolve through the existing
config secret-resolution mechanism.

Gateway startup fails fast when the Asana integration is enabled without a
database DSN, token, workspace GID, or team GID. It validates the token and
workspace/team before starting the meeting poller.

## Permissions and security

### Slack

Extend the app manifest with:

- `channels:manage` for public project-channel creation;
- `channels:write.invites` for inviting existing workspace members; and
- `users:read.email` for exact email identity matching.

The existing messaging, channel-read, user-read, and interactivity scopes remain.
The v1 workflow does not invite people who are not already Slack workspace
members.

### Asana

Sakshi's token must be able to read users/projects, create and update projects,
manage project membership, and create/update tasks and followers within the
configured workspace/team. Asana authorization is still constrained by Sakshi's
own access. Project creation uses the configured workspace/team; Asana's API
requires a workspace and, for an organization, association with a team. See
[Asana project creation](https://developers.asana.com/reference/createproject).

### Secret handling

- The Asana token and database DSN are environment-backed secrets.
- Neither value is stored in Postgres, approval JSON, logs, Slack messages, or
  exception text.
- HTTP error logging records service, endpoint name, status, retry metadata, and
  a sanitized message, never request authorization headers or raw bodies that may
  echo credentials.
- Slack and Asana external IDs are not secrets and may be stored for reconciliation.

## Error handling

- **Validation errors:** keep the draft editable and show field-level corrections.
- **Unauthenticated/forbidden API responses:** pause the job in
  `needs_attention`; do not retry automatically.
- **Rate limits:** honor `Retry-After` and retry with bounded exponential backoff.
- **Network timeouts and 5xx responses:** retry automatically, reconciling before
  any create operation.
- **Not found:** invalidate the cached external mapping and reconcile once; if the
  resource is truly absent, pause for Sakshi rather than silently replacing an
  approved existing project.
- **Ambiguous reconciliation:** pause and show all candidate identifiers; never
  guess or create another resource.
- **Process restart:** a gateway worker claims incomplete jobs using database row
  locking and resumes their first incomplete step.
- **Concurrent clicks/workers:** revision checks, unique constraints, and row locks
  allow only one approval transition and one active executor per job.
- **Slack announcement failure:** the Asana work remains valid; retry only the
  announcement step.

## Testing strategy

### Unit tests

- structured meeting classification for existing and proposed new projects;
- explicit-date acceptance and rejection of inferred/invalid dates;
- normalized-email identity matching, ambiguity, inactive users, and cache expiry;
- project-key and public-channel-slug normalization/collision behavior;
- approval validation and immutable snapshot construction;
- Slack task pagination, edit/add/remove actions, revision encoding, stale-click
  rejection, and approver authorization;
- Asana parent/subtask payloads, single assignee, followers, and absence of
  invented due dates; and
- sanitization of API errors and credential-bearing values.

### Database tests

- additive migrations against the current project-registry schema;
- static-config seeding without overwriting runtime-owned external IDs;
- dynamic project persistence and immediate channel resolution;
- approval status/revision constraints;
- one job per approved snapshot and one row per named step; and
- row-lock behavior with two simulated workers.

### Adapter contract tests

Use fake HTTP/Slack clients to pin request paths, payloads, pagination, rate-limit
handling, membership idempotency, and response parsing. Include 401, 403, 404,
409/name collision, 429, timeout, and 5xx cases.

### Workflow failure injection

For both existing- and new-project flows, fail after every external step, restart
the engine, and assert:

- completed step IDs are reused;
- ambiguous create timeouts reconcile by provenance marker;
- no duplicate Asana project, Slack channel, parent task, subtask, membership, or
  final announcement is created; and
- the job reaches `complete` after retry.

### Regression and smoke tests

- The current meeting classifier still posts approved slices when Asana is
  disabled.
- Existing static projects and channel resolution continue to work.
- Conversation memory remains off when only `database.dsn` is configured.
- A sandbox end-to-end smoke test provisions one public Slack channel and one
  Asana project, assigns the lead, adds multiple participants, creates an
  unassigned parent and single/multi-person subtasks, and posts the final link.

## Out of scope

- Synchronizing later Asana edits back into Slack or ProjectClaw.
- Completing Slack tasks when Asana tasks complete.
- General-purpose Asana tools exposed to the LLM outside this approved workflow.
- Automatically inviting people who are not already members of the Slack or
  Asana workspace.
- Private Slack project channels in v1.
- AI-inferred assignees or due dates.
- Multiple approvers, Slack workspaces, or Asana workspaces.
- Deleting or rolling back externally created resources after partial failure.
