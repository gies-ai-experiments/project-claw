"""Durable, marker-reconciled provisioning of approved meeting actions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from nanobot.config.schema import Project
from nanobot.integrations.asana import (
    AsanaAmbiguousError,
    AsanaPermanentError,
    AsanaRetryableError,
)
from nanobot.integrations.errors import safe_external_error
from nanobot.integrations.slack_workspace import (
    SlackAmbiguousError,
    SlackPermanentError,
    SlackRetryableError,
)
from nanobot.meeting_classifier.models import ApprovalSnapshot, PersonRef
from nanobot.meeting_classifier.repository import ProvisioningJob

_Retryable = (AsanaRetryableError, SlackRetryableError)
_Permanent = (AsanaPermanentError, AsanaAmbiguousError, SlackPermanentError, SlackAmbiguousError)


class ProvisioningWorker:
    """Claims one durable job at a time and resumes it at incomplete steps."""

    def __init__(
        self,
        repository: Any,
        asana: Any,
        slack: Any,
        identities: Any,
        *,
        project_provider: Callable[[str], Project | None],
        admin_slack_id: str,
        registry: Any | None = None,
        slack_channel: Any | None = None,
        retry_delay_s: float = 30,
        poll_interval_s: float = 30,
    ) -> None:
        self._repo = repository
        self._asana = asana
        self._slack = slack
        self._identities = identities
        self._project_provider = project_provider
        self._admin_slack_id = admin_slack_id
        self._registry = registry
        self._slack_channel = slack_channel
        self._retry_delay_s = retry_delay_s
        self._poll_interval_s = poll_interval_s
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._current_step = "000:project"

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self._repo.recover_running_jobs()
        self._task = asyncio.create_task(self._loop(), name="meeting-provisioning")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def wake(self) -> None:
        self._wake_event.set()

    async def _loop(self) -> None:
        while True:
            worked = await self.run_once()
            if worked:
                continue
            self._wake_event.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake_event.wait(), self._poll_interval_s)

    async def run_once(self) -> bool:
        job = await self._repo.claim_next_job()
        if job is None:
            return False
        snapshot = await self._repo.get_snapshot(job.id)
        self._current_step = "000:project"
        try:
            await self._repo.ensure_step(
                job.id,
                "900:announcement",
                f"approval:{snapshot.approval_id}:announcement",
            )
            if job.kind == "existing_project":
                await self._provision_existing(job, snapshot)
            elif job.kind == "new_project":
                await self._provision_new(job, snapshot)
            else:
                raise ValueError("unsupported provisioning job kind")
            await self._repo.complete_job(job.id)
        except _Retryable as exc:
            await self._repo.fail_step(
                job.id, self._current_step, exc.safe_message, permanent=False
            )
            delay = exc.retry_after if exc.retry_after is not None else self._retry_delay_s
            await self._repo.release_retryable_job(
                job.id, datetime.now(UTC) + timedelta(seconds=max(0, delay))
            )
        except _Permanent as exc:
            await self._repo.fail_step(
                job.id, self._current_step, exc.safe_message, permanent=True
            )
            await self._notify_failure(snapshot, exc.safe_message)
        except Exception:
            safe_message = safe_external_error("Meeting provisioning", "unexpected operation")
            await self._repo.fail_step(
                job.id, self._current_step, safe_message, permanent=True
            )
            await self._notify_failure(snapshot, safe_message)
        return True

    async def _provision_existing(
        self, job: ProvisioningJob, snapshot: ApprovalSnapshot
    ) -> None:
        project = self._project_provider(snapshot.draft.project)
        if project is None or project.asana is None or not project.channel:
            raise ValueError("approved project mapping is incomplete")

        steps = {step.step_name: step for step in await self._repo.list_steps(job.id)}
        project_step = steps["000:project"]
        parent_gid = project_step.external_id
        parent_url: str | None = None
        if project_step.status != "complete":
            self._current_step = project_step.step_name
            await self._repo.mark_step_running(job.id, project_step.step_name)
            await self._asana.get_project(project.asana.project_gid)
            identities = await self._resolve_people(self._people(snapshot))
            await self._asana.add_project_members(
                project.asana.project_gid,
                sorted({record.asana_user_gid for record in identities.values()}),
            )
            marker = f"projectclaw:approval:{snapshot.approval_id}:parent"
            parent = await self._asana.find_parent_task_by_marker(
                project.asana.project_gid, marker
            )
            if parent is None:
                parent = await self._asana.create_parent_task(
                    project.asana.project_gid, snapshot, marker
                )
            parent_gid, parent_url = parent.gid, parent.permalink_url
            await self._repo.complete_step(job.id, project_step.step_name, parent_gid)
        if not parent_gid:
            raise ValueError("parent task has no external ID")

        await self._provision_tasks(
            job, snapshot, project, parent_gid, parent_url, steps
        )

    async def _provision_new(
        self, job: ProvisioningJob, snapshot: ApprovalSnapshot
    ) -> None:
        if self._registry is None or snapshot.draft.lead is None:
            raise ValueError("new-project provisioning is not configured")
        extra_steps = (
            "010:asana-members",
            "020:slack-channel",
            "030:slack-members",
            "040:activate",
            "050:parent",
        )
        for step_name in extra_steps:
            await self._repo.ensure_step(
                job.id, step_name, f"approval:{snapshot.approval_id}:{step_name}"
            )
        await self._registry.reserve_new_project(
            snapshot.draft, self._admin_slack_id
        )
        steps = {step.step_name: step for step in await self._repo.list_steps(job.id)}
        project_marker = f"projectclaw:project:{snapshot.draft.project}"

        project_step = steps["000:project"]
        asana_project_gid = project_step.external_id
        if project_step.status != "complete":
            self._current_step = project_step.step_name
            await self._repo.mark_step_running(job.id, project_step.step_name)
            remote_project = await self._asana.find_project_by_marker(
                snapshot.draft.display_name, project_marker
            )
            if remote_project is None:
                remote_project = await self._asana.create_project(
                    name=snapshot.draft.display_name,
                    notes=snapshot.draft.description,
                    marker=project_marker,
                )
            asana_project_gid = remote_project.gid
            await self._repo.complete_step(
                job.id, project_step.step_name, asana_project_gid
            )
        if not asana_project_gid:
            raise ValueError("Asana project has no external ID")

        identities = await self._resolve_people(self._people(snapshot))
        member_step = steps["010:asana-members"]
        if member_step.status != "complete":
            self._current_step = member_step.step_name
            await self._repo.mark_step_running(job.id, member_step.step_name)
            lead_gid = identities[snapshot.draft.lead.email].asana_user_gid
            await self._asana.add_project_members(asana_project_gid, [lead_gid])
            await self._asana.set_project_owner(asana_project_gid, lead_gid)
            await self._asana.add_project_members(
                asana_project_gid,
                sorted({record.asana_user_gid for record in identities.values()}),
            )
            await self._repo.complete_step(job.id, member_step.step_name, None)

        channel_step = steps["020:slack-channel"]
        channel_id = channel_step.external_id
        if channel_step.status != "complete":
            self._current_step = channel_step.step_name
            await self._repo.mark_step_running(job.id, channel_step.step_name)
            channel = await self._slack.find_channel_by_slug(snapshot.draft.channel_slug)
            if channel is not None and channel.marker != project_marker:
                raise SlackAmbiguousError(operation="reconcile_project_channel")
            if channel is None:
                channel = await self._slack.create_public_channel(
                    snapshot.draft.channel_slug
                )
                await self._slack.set_channel_marker(channel.channel_id, project_marker)
            channel_id = channel.channel_id
            await self._repo.complete_step(job.id, channel_step.step_name, channel_id)
        if not channel_id:
            raise ValueError("Slack channel has no external ID")

        slack_members = steps["030:slack-members"]
        if slack_members.status != "complete":
            self._current_step = slack_members.step_name
            await self._repo.mark_step_running(job.id, slack_members.step_name)
            slack_ids = sorted(
                {
                    record.slack_user_id
                    for record in identities.values()
                    if record.slack_user_id
                }
            )
            if len(slack_ids) != len(identities):
                raise ValueError("approved participant could not be mapped to Slack")
            await self._slack.invite_users(channel_id, slack_ids)
            await self._repo.complete_step(job.id, slack_members.step_name, None)

        project = Project(
            name=snapshot.draft.project,
            asana={"projectGid": asana_project_gid},
            channel=channel_id,
            lead_email=snapshot.draft.lead.email,
            description=snapshot.draft.description,
        )
        activation = steps["040:activate"]
        if activation.status != "complete":
            self._current_step = activation.step_name
            await self._repo.mark_step_running(job.id, activation.step_name)
            await self._registry.activate_dynamic(
                project, channel_id, asana_project_gid
            )
            await self._repo.complete_step(job.id, activation.step_name, project.name)
        if self._slack_channel is not None:
            self._slack_channel.activate_project(project, channel_id)

        parent_step = steps["050:parent"]
        parent_gid = parent_step.external_id
        parent_url: str | None = None
        if parent_step.status != "complete":
            self._current_step = parent_step.step_name
            await self._repo.mark_step_running(job.id, parent_step.step_name)
            marker = f"projectclaw:approval:{snapshot.approval_id}:parent"
            parent = await self._asana.find_parent_task_by_marker(
                asana_project_gid, marker
            )
            if parent is None:
                parent = await self._asana.create_parent_task(
                    asana_project_gid, snapshot, marker
                )
            parent_gid, parent_url = parent.gid, parent.permalink_url
            await self._repo.complete_step(job.id, parent_step.step_name, parent_gid)
        if not parent_gid:
            raise ValueError("parent task has no external ID")
        await self._provision_tasks(
            job, snapshot, project, parent_gid, parent_url, steps
        )

    async def _provision_tasks(
        self,
        job: ProvisioningJob,
        snapshot: ApprovalSnapshot,
        project: Project,
        parent_gid: str,
        parent_url: str | None,
        steps: dict[str, Any],
    ) -> None:

        for index, task in enumerate(snapshot.draft.tasks, start=1):
            step_name = f"{index:03d}:task:{task.id}"
            step = steps[step_name]
            if step.status == "complete":
                continue
            self._current_step = step_name
            await self._repo.mark_step_running(job.id, step_name)
            identities = await self._resolve_people(
                ([task.owner] if task.owner else []) + list(task.collaborators)
            )
            assignee_gid = (
                identities[task.owner.email].asana_user_gid if task.owner is not None else None
            )
            marker = f"projectclaw:approval:{snapshot.approval_id}:task:{task.id}"
            remote = await self._asana.find_task_by_marker(parent_gid, marker)
            if remote is None:
                remote = await self._asana.create_subtask(
                    parent_gid, task, assignee_gid, marker
                )
            followers = sorted(
                {identities[person.email].asana_user_gid for person in task.collaborators}
            )
            await self._asana.add_task_followers(remote.gid, followers)
            await self._repo.complete_step(job.id, step_name, remote.gid)

        announcement = next(
            step for step in await self._repo.list_steps(job.id)
            if step.step_name == "900:announcement"
        )
        if announcement.status != "complete":
            self._current_step = announcement.step_name
            await self._repo.mark_step_running(job.id, announcement.step_name)
            marker = f"projectclaw:approval:{snapshot.approval_id}:slack"
            message = await self._slack.find_message_by_marker(project.channel, marker)
            if message is None:
                parent_url = parent_url or f"https://app.asana.com/0/0/{parent_gid}"
                text = self._announcement_text(snapshot, parent_url, marker)
                ts = await self._slack.post_blocks(
                    project.channel,
                    text,
                    [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
                )
            else:
                ts = message.timestamp
            await self._repo.complete_step(job.id, announcement.step_name, ts)

    async def _resolve_people(self, people: list[PersonRef | None]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for person in people:
            if person is None or person.email in resolved:
                continue
            record = await self._identities.get(person.email)
            if record is None or not record.asana_user_gid:
                raise ValueError("approved participant could not be mapped to Asana")
            resolved[person.email] = record
        return resolved

    @staticmethod
    def _people(snapshot: ApprovalSnapshot) -> list[PersonRef | None]:
        people: list[PersonRef | None] = [snapshot.draft.lead]
        for task in snapshot.draft.tasks:
            people.append(task.owner)
            people.extend(task.collaborators)
        return people

    @staticmethod
    def _announcement_text(
        snapshot: ApprovalSnapshot, parent_url: str, marker: str
    ) -> str:
        items = "\n".join(f"• {task.title}" for task in snapshot.draft.tasks)
        return (
            f"Approved meeting tasks are now in Asana: {parent_url}\n"
            f"{snapshot.draft.summary}\n{items}\n{marker}"
        )

    async def _notify_failure(self, snapshot: ApprovalSnapshot, safe_message: str) -> None:
        if not self._admin_slack_id:
            return
        with suppress(Exception):
            channel = await self._slack.open_dm(self._admin_slack_id)
            text = f"Provisioning failed for approval {snapshot.approval_id}: {safe_message}"
            await self._slack.post_blocks(
                channel,
                text,
                [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            )
