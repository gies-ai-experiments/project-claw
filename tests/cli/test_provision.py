from nanobot.cli.provision import provision_channels, slugify
from nanobot.config.schema import GitHubProjectConfig, PersonConfig, Project


def _project(name):
    return Project(
        name=name, github=GitHubProjectConfig(repos=["o/r"]),
        people=[PersonConfig(email="a@b.c", slack_id="U1")],
    )


def test_slugify():
    assert slugify("Market Game") == "market-game"


class FakeWeb:
    def __init__(self):
        self.created, self.invited = [], []

    async def conversations_create(self, name, is_private=False):
        self.created.append(name)
        return {"channel": {"id": "C_" + name}}

    async def conversations_invite(self, channel, users):
        self.invited.append((channel, users))
        return {"ok": True}


async def test_creates_channel_and_invites_roster():
    web = FakeWeb()
    out = await provision_channels(web, [_project("Claw")], existing={}, dry_run=False)
    assert web.created == ["claw"]
    assert web.invited == [("C_claw", "U1")]
    assert out == [{"project": "Claw", "channel_id": "C_claw", "created": True}]


async def test_skips_existing():
    web = FakeWeb()
    out = await provision_channels(web, [_project("Claw")], existing={"Claw": "COLD"}, dry_run=False)
    assert web.created == []
    assert out == [{"project": "Claw", "channel_id": "COLD", "created": False}]


async def test_dry_run_creates_nothing():
    web = FakeWeb()
    out = await provision_channels(web, [_project("Claw")], existing={}, dry_run=True)
    assert web.created == [] and web.invited == []
    assert out[0]["created"] is False
