"""Editable meeting-approval lifecycle owned by Sakshi's Slack interactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable
from uuid import UUID

from nanobot.meeting_classifier.models import MeetingAction, PersonRef, ProjectDraft
from nanobot.meeting_classifier.slack_ui import (
    TaskSubmissionError,
    build_review_pages,
    build_task_modal,
    parse_task_submission,
)


@dataclass(frozen=True)
class InteractionResult:
    kind: str
    message: str = ""
    approval_id: UUID | None = None
    job_id: UUID | None = None
    response_action: dict[str, Any] | None = None


class MeetingApprovalCoordinator:
    def __init__(
        self,
        approval_repository: Any,
        provisioning_repository: Any,
        identity_resolver: Any,
        slack: Any,
        *,
        admin_slack_id: str,
        known_projects: set[str] | None = None,
    ) -> None:
        self.repo = approval_repository
        self._provisioning = provisioning_repository
        self._identity = identity_resolver
        self._slack = slack
        self._admin_slack_id = admin_slack_id
        self._known_projects = known_projects

    async def on_new_note(
        self,
        note_id: str,
        meeting_title: str,
        meeting_date: date,
        drafts: list[ProjectDraft],
    ) -> list[Any]:
        records: list[Any] = []
        for draft in drafts:
            record = await self.repo.create_draft(
                note_id, meeting_title, meeting_date, draft
            )
            records.append(record)
            if self._slack is None:
                continue
            channel_id = await self._slack.open_dm(self._admin_slack_id)
            timestamps: list[str] = []
            for page in build_review_pages(
                record.id, record.revision, meeting_title, record.draft
            ):
                timestamps.append(
                    await self._slack.post_blocks(
                        channel_id,
                        f"Review {meeting_title}: {record.draft.project}",
                        page,
                    )
                )
            if hasattr(self.repo, "set_review_messages"):
                await self.repo.set_review_messages(record.id, channel_id, timestamps)
        return records

    async def handle_interaction(self, payload: dict[str, Any]) -> InteractionResult:
        sender_id = str(((payload.get("user") or {}).get("id")) or "")
        if sender_id != self._admin_slack_id:
            return InteractionResult("forbidden", "Only Sakshi can change meeting tasks.")

        if payload.get("type") == "view_submission":
            return await self._handle_submission(payload)

        actions = payload.get("actions") or []
        action = MeetingAction.parse(str((actions[0] if actions else {}).get("value") or ""))
        if action is None:
            return InteractionResult("ignored", "Unknown meeting action.")
        record = await self.repo.get(action.approval_id)
        if record is None:
            return InteractionResult("stale", "This approval no longer exists.")
        if record.revision != action.revision or record.status != "pending":
            return InteractionResult("stale", "This preview is stale; use the latest one.")

        if action.verb in {"add", "edit"}:
            task = next(
                (item for item in record.draft.tasks if item.id == action.task_id), None
            )
            if action.verb == "edit" and task is None:
                return InteractionResult("validation_error", "The task no longer exists.")
            if self._slack is not None:
                await self._slack.open_modal(
                    str(payload.get("trigger_id") or ""), build_task_modal(action, task)
                )
            return InteractionResult("modal_opened", approval_id=action.approval_id)

        if action.verb == "remove":
            tasks = [task for task in record.draft.tasks if task.id != action.task_id]
            if len(tasks) == len(record.draft.tasks):
                return InteractionResult("validation_error", "The task no longer exists.")
            updated = _replace_tasks(record.draft, tasks)
            new_record = await self.repo.replace_draft(
                action.approval_id, updated, action.revision
            )
            return InteractionResult("updated", approval_id=new_record.id)

        if action.verb == "skip":
            skipped = await self.repo.skip(action.approval_id, action.revision)
            return InteractionResult(
                "skipped" if skipped else "stale", approval_id=action.approval_id
            )

        if action.verb == "retry":
            return await self.retry(action.approval_id, sender_id=sender_id)

        if action.verb != "approve":
            return InteractionResult("ignored", "Unknown meeting action.")
        validation = await self._validate_for_approval(record.draft)
        if validation:
            return InteractionResult(
                "validation_error", validation, approval_id=action.approval_id
            )
        _snapshot, job_id = await self.repo.approve_and_enqueue(
            action.approval_id,
            expected_revision=action.revision,
            approver_slack_id=sender_id,
        )
        return InteractionResult(
            "approved", approval_id=action.approval_id, job_id=job_id
        )

    async def retry(
        self, approval_id: UUID, *, sender_id: str | None = None
    ) -> InteractionResult:
        if sender_id is not None and sender_id != self._admin_slack_id:
            return InteractionResult("forbidden", "Only Sakshi can retry provisioning.")
        if self._provisioning is None or not hasattr(
            self._provisioning, "retry_needs_attention"
        ):
            return InteractionResult("validation_error", "Retry is not available.")
        retried = await self._provisioning.retry_needs_attention(approval_id)
        return InteractionResult(
            "retrying" if retried else "stale", approval_id=approval_id
        )

    async def _handle_submission(
        self, payload: dict[str, Any]
    ) -> InteractionResult:
        view = payload.get("view") or {}
        action = MeetingAction.parse(str(view.get("private_metadata") or ""))
        if action is None:
            return InteractionResult("stale", "This task form is invalid.")
        record = await self.repo.get(action.approval_id)
        if record is None or record.revision != action.revision or record.status != "pending":
            return InteractionResult("stale", "This task form is stale.")
        try:
            task = parse_task_submission(view)
        except TaskSubmissionError as exc:
            return InteractionResult(
                "validation_error",
                "Review the task fields.",
                response_action=exc.response_action,
            )
        tasks = list(record.draft.tasks)
        if action.verb == "add":
            tasks.append(task)
        else:
            index = next(
                (i for i, existing in enumerate(tasks) if existing.id == action.task_id),
                None,
            )
            if index is None:
                return InteractionResult("stale", "The task no longer exists.")
            tasks[index] = task
        updated = _replace_tasks(record.draft, tasks)
        new_record = await self.repo.replace_draft(
            action.approval_id, updated, action.revision
        )
        return InteractionResult("updated", approval_id=new_record.id)

    async def _validate_for_approval(self, draft: ProjectDraft) -> str:
        if not draft.tasks:
            return "At least one task is required."
        if self._known_projects is not None:
            if draft.is_new_project and draft.project in self._known_projects:
                return f"Project key {draft.project} already exists."
            if not draft.is_new_project and draft.project not in self._known_projects:
                return f"Project {draft.project} is not configured."
        if draft.is_new_project and draft.lead is None:
            return "A new project requires exactly one lead."
        for person in _unique_people(draft):
            try:
                await self._identity.resolve(person)
            except Exception:
                return f"{person.email} could not be resolved in Slack and Asana."
        return ""


def _replace_tasks(draft: ProjectDraft, tasks: list[Any]) -> ProjectDraft:
    values = draft.model_dump(mode="python")
    values["tasks"] = tasks
    return ProjectDraft.model_validate(values)


def _unique_people(draft: ProjectDraft) -> Iterable[PersonRef]:
    people: list[PersonRef] = []
    if draft.lead:
        people.append(draft.lead)
    for task in draft.tasks:
        if task.owner:
            people.append(task.owner)
        people.extend(task.collaborators)
    by_email = {person.email.strip().lower(): person for person in people}
    return by_email.values()
