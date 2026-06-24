---
name: mindforum
description: Create MindForum rooms and send invitations.
---

# MindForum

Use the MindForum tools when the user asks you to create a MindForum room, set up a forum, or invite people to one.

If neither `create_mindforum_room` nor `invite_to_mindforum_room` is available in the current tool list, tell the user that the MindForum integration is not enabled for this nanobot instance (it needs `tools.mindforum.host` and `tools.mindforum.api_key` configured).

## When To Use

- Create a room: call `create_mindforum_room` with a concrete `name`. Add a `system_prompt` only if the user specified a persona/behavior for the room. Add a `slug` only if the user asked for a specific room id.
- Invite people: call `invite_to_mindforum_room` with the `room_id` returned by `create_mindforum_room` (or a room id the user gave you) and an `invites` list of `{invitee_email, invitee_name}` objects. Max 50 per call.

## Rules

- The `name` is required and user-facing. The `slug` (optional) must be lowercase letters, digits, and hyphens, 3-40 chars — if the user gives you an invalid slug, fix it or ask; do not pass it through.
- Only the room's creator can invite (the API key owner). If `invite_to_mindforum_room` returns `not_found`, the room is not owned by this key — tell the user.
- The API does not return a room URL, only an id. Report the room id; do not invent a URL.
- Rate limits are enforced server-side: ~20 rooms/day, ~200 invites/day, 50 invites per call. If a call comes back rate-limited, tell the user and suggest waiting.

## Examples

Create a room with a system prompt:

```text
create_mindforum_room(
  name="Design Critique",
  system_prompt="You are a constructive design critic. Be specific, kind, and propose alternatives.",
)
```

Create a room with a custom slug, no system prompt:

```text
create_mindforum_room(
  name="Weekly Sync",
  slug="weekly-sync",
)
```

Invite people to the room you just created:

```text
invite_to_mindforum_room(
  room_id="weekly-sync",
  invites=[
    {"invitee_email": "a@x.edu", "invitee_name": "Avery"},
    {"invitee_email": "b@x.edu", "invitee_name": "Blair"},
  ],
)
```
