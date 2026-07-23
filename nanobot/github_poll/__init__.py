"""Near-real-time GitHub -> Slack by polling project repos' default branches."""

from nanobot.github_poll.service import GithubPollService, build_repo_channel_map

__all__ = ["GithubPollService", "build_repo_channel_map"]
