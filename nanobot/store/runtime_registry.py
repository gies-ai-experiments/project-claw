"""Durable runtime project registry layered over static Slack configuration."""

from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import asyncpg

from nanobot.config.schema import Project
from nanobot.meeting_classifier.models import ProjectDraft

if TYPE_CHECKING:
    from nanobot.channels.slack import SlackConfig


Database = asyncpg.Connection | asyncpg.Pool


@asynccontextmanager
async def _acquire(database: Database):
    if isinstance(database, asyncpg.Pool):
        async with database.acquire() as conn:
            yield conn
    else:
        yield database


class RuntimeProjectRegistry:
    def __init__(self, conn: Database) -> None:
        self._database = conn

    async def seed_static(self, slack_cfg: "SlackConfig") -> None:
        """Upsert config-owned fields without clearing runtime-owned external IDs."""
        channels_for: dict[str, list[str]] = defaultdict(list)
        defaults_for: dict[str, list[str]] = defaultdict(list)
        for channel_id, channel in slack_cfg.project_channels.items():
            for project_id in channel.allowed_projects:
                channels_for[project_id].append(channel_id)
            if channel.default_project:
                defaults_for[channel.default_project].append(channel_id)

        async with _acquire(self._database) as conn:
            async with conn.transaction():
                for project_id, project in slack_cfg.projects.items():
                    await conn.execute(
                        """
                        INSERT INTO project_registry
                          (project_id, display_name, description, lead_email, github_repos,
                           granola_folder_id, allowed_channels, default_channels, source,
                           updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'static_config', now())
                        ON CONFLICT (project_id) DO UPDATE SET
                          display_name = EXCLUDED.display_name,
                          description = EXCLUDED.description,
                          lead_email = EXCLUDED.lead_email,
                          github_repos = EXCLUDED.github_repos,
                          granola_folder_id = EXCLUDED.granola_folder_id,
                          allowed_channels = EXCLUDED.allowed_channels,
                          default_channels = EXCLUDED.default_channels,
                          updated_at = now()
                        """,
                        project_id,
                        project.name,
                        project.description,
                        project.lead_email.strip().lower() or None,
                        project.github.repos if project.github else [],
                        project.granola.folder_id if project.granola else None,
                        sorted(set(channels_for.get(project_id, []))),
                        sorted(set(defaults_for.get(project_id, []))),
                    )

    async def load_dynamic(self) -> list[Project]:
        rows = await self._database.fetch(
            """
            SELECT * FROM project_registry
            WHERE source='runtime' AND lifecycle_status='active'
            ORDER BY project_id
            """
        )
        projects: list[Project] = []
        for row in rows:
            values: dict[str, object] = {
                "name": row["project_id"],
                "description": row["description"],
                "lead_email": row["lead_email"] or "",
                "channel": row["slack_channel_id"] or "",
            }
            if row["github_repos"]:
                values["github"] = {"repos": list(row["github_repos"])}
            if row["granola_folder_id"]:
                values["granola"] = {"folderId": row["granola_folder_id"]}
            if row["asana_project_gid"]:
                values["asana"] = {"projectGid": row["asana_project_gid"]}
            projects.append(Project.model_validate(values))
        return projects

    async def reserve_new_project(self, draft: ProjectDraft, approver_slack_id: str) -> None:
        if not draft.is_new_project or draft.lead is None:
            raise ValueError("only complete new-project drafts can be reserved")
        lead_email = draft.lead.email.strip().lower()
        async with _acquire(self._database) as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    INSERT INTO project_registry
                      (project_id, display_name, description, lead_email, lifecycle_status,
                       source, created_by_slack_id, updated_at)
                    VALUES ($1, $2, $3, $4, 'provisioning', 'runtime', $5, now())
                    ON CONFLICT (project_id) DO UPDATE SET
                      display_name=EXCLUDED.display_name,
                      description=EXCLUDED.description,
                      lead_email=EXCLUDED.lead_email,
                      lifecycle_status='provisioning',
                      created_by_slack_id=EXCLUDED.created_by_slack_id,
                      updated_at=now()
                    WHERE project_registry.source='runtime'
                    """,
                    draft.project,
                    draft.display_name,
                    draft.description,
                    lead_email,
                    approver_slack_id,
                )
                if result == "INSERT 0 0":
                    raise ValueError("project ID is already owned by static configuration")
                current_lead = await conn.fetchval(
                    """
                    SELECT email_normalized FROM project_membership
                    WHERE project_id=$1 AND role='lead'
                    """,
                    draft.project,
                )
                if current_lead is not None and current_lead != lead_email:
                    raise ValueError("project already has a different lead")
                await conn.execute(
                    """
                    INSERT INTO identity_directory (email_normalized, display_name)
                    VALUES ($1, $2)
                    ON CONFLICT (email_normalized) DO UPDATE
                      SET display_name=EXCLUDED.display_name
                    """,
                    lead_email,
                    draft.lead.name,
                )
                await conn.execute(
                    """
                    INSERT INTO project_membership (project_id, email_normalized, role)
                    VALUES ($1, $2, 'lead')
                    ON CONFLICT (project_id, email_normalized) DO UPDATE SET role='lead'
                    """,
                    draft.project,
                    lead_email,
                )

    async def activate_dynamic(
        self, project: Project, channel_id: str, asana_project_gid: str
    ) -> None:
        if not channel_id.strip() or not asana_project_gid.strip():
            raise ValueError("dynamic projects require Slack and Asana external IDs")
        result = await self._database.execute(
            """
            INSERT INTO project_registry
              (project_id, display_name, description, lead_email, github_repos,
               granola_folder_id, slack_channel_id, asana_project_gid,
               lifecycle_status, source, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'active', 'runtime', now())
            ON CONFLICT (project_id) DO UPDATE SET
              description=CASE WHEN EXCLUDED.description <> '' THEN EXCLUDED.description
                               ELSE project_registry.description END,
              lead_email=COALESCE(EXCLUDED.lead_email, project_registry.lead_email),
              github_repos=CASE WHEN cardinality(EXCLUDED.github_repos) > 0
                                THEN EXCLUDED.github_repos ELSE project_registry.github_repos END,
              granola_folder_id=COALESCE(EXCLUDED.granola_folder_id,
                                         project_registry.granola_folder_id),
              slack_channel_id=EXCLUDED.slack_channel_id,
              asana_project_gid=EXCLUDED.asana_project_gid,
              lifecycle_status='active', source='runtime', updated_at=now()
            WHERE project_registry.source='runtime'
            """,
            project.name,
            project.name,
            project.description,
            project.lead_email.strip().lower() or None,
            project.github.repos if project.github else [],
            project.granola.folder_id if project.granola else None,
            channel_id,
            asana_project_gid,
        )
        if result == "INSERT 0 0":
            raise ValueError("project ID is already owned by static configuration")

    async def mark_needs_attention(self, project_id: str) -> None:
        result = await self._database.execute(
            """
            UPDATE project_registry SET lifecycle_status='needs_attention', updated_at=now()
            WHERE project_id=$1
            """,
            project_id,
        )
        if result == "UPDATE 0":
            raise ValueError("unknown project")
