"""Structured, validated payloads for the meeting approval workflow."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """Domain model accepting both camelCase and snake_case input."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PersonRef(Base):
    name: str
    email: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class TaskDraft(Base):
    id: str
    title: str
    owner: PersonRef | None = None
    collaborators: list[PersonRef] = Field(default_factory=list)
    due_on: date | None = None
    due_on_source: Literal["meeting", "reviewer"] | None = None

    @field_validator("title")
    @classmethod
    def _require_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("task title must not be blank")
        return title

    @model_validator(mode="after")
    def _validate_due_date_and_collaborators(self) -> "TaskDraft":
        if (self.due_on is None) != (self.due_on_source is None):
            raise ValueError("due_on and due_on_source must be provided together")
        emails = [person.email for person in self.collaborators]
        if len(emails) != len(set(emails)):
            raise ValueError("collaborator emails must be unique")
        return self


class ProjectDraft(Base):
    project: str
    is_new_project: bool = False
    display_name: str = ""
    description: str = ""
    channel_slug: str = ""
    lead: PersonRef | None = None
    summary: str = ""
    tasks: list[TaskDraft] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_project(self) -> "ProjectDraft":
        if self.is_new_project:
            required = {
                "display_name": self.display_name,
                "description": self.description,
                "channel_slug": self.channel_slug,
            }
            missing = [name for name, value in required.items() if not value.strip()]
            if self.lead is None:
                missing.append("lead")
            if missing:
                raise ValueError(
                    "new projects require " + ", ".join(missing)
                )
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique")
        return self


class ApprovalSnapshot(Base):
    approval_id: UUID
    note_id: str
    meeting_title: str
    meeting_date: date
    revision: int
    draft: ProjectDraft


@dataclass(frozen=True)
class MeetingAction:
    """A compact Slack action value scoped to an approval revision."""

    verb: str
    approval_id: UUID
    revision: int
    task_id: str | None = None

    def __post_init__(self) -> None:
        if not self.verb or not self.verb.strip() or ":" in self.verb:
            raise ValueError("verb must be nonempty and colon-free")
        if not isinstance(self.approval_id, UUID):
            raise ValueError("approval_id must be a UUID")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        if self.task_id is not None and (
            not self.task_id or ":" in self.task_id or self.task_id == "-"
        ):
            raise ValueError("task_id must be nonempty, colon-free, and not '-'")

    def encode(self) -> str:
        task_id = self.task_id if self.task_id is not None else "-"
        return f"mtg2:{self.verb}:{self.approval_id}:{self.revision}:{task_id}"

    @classmethod
    def parse(cls, value: str) -> "MeetingAction | None":
        if not isinstance(value, str):
            return None
        parts = value.split(":")
        if len(parts) != 5 or parts[0] != "mtg2":
            return None
        _, verb, approval_raw, revision_raw, task_raw = parts
        if not revision_raw.isascii() or not revision_raw.isdigit():
            return None
        try:
            approval_id = UUID(approval_raw)
            revision = int(revision_raw)
            task_id = None if task_raw == "-" else task_raw
            return cls(verb, approval_id, revision, task_id)
        except (TypeError, ValueError):
            return None
