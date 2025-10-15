from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
import importlib.util

import pytest
import pytz


REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_module(module_name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if "leo_bot" not in sys.modules:
    pkg = ModuleType("leo_bot")
    pkg.__path__ = [str(REPO_ROOT / "leo_bot")]
    sys.modules["leo_bot"] = pkg
if "leo_bot.cogs" not in sys.modules:
    subpkg = ModuleType("leo_bot.cogs")
    subpkg.__path__ = [str(REPO_ROOT / "leo_bot" / "cogs")]
    sys.modules["leo_bot.cogs"] = subpkg

config_module = _import_module("leo_bot.config", "leo_bot/config.py")
scheduler_core = _import_module("leo_bot.scheduler", "leo_bot/scheduler.py")
scheduler_module = _import_module(
    "leo_bot.cogs.scheduler", "leo_bot/cogs/scheduler.py"
)

BotConfig = config_module.BotConfig
ScheduleManager = scheduler_core.ScheduleManager
ScheduleCog = scheduler_module.ScheduleCog


class DummyChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id


class DummyManager(ScheduleManager):
    def __init__(self):
        # Provide a temporary config to satisfy parent initialisation
        tmp_config = BotConfig(
            token="token",
            guild_id=1,
            test_guild_id=1,
            admin_ids=(1,),
            ready_channel_id=1,
            report_log_channel_id=1,
            f1_channels={},
            schedule_path=Path("/tmp/schedules.json"),
            default_timezone=pytz.utc,
        )
        super().__init__(tmp_config)
        self.added_jobs = []

    def add_job(self, job):
        self.added_jobs.append(job)


@pytest.mark.asyncio
async def test_schedule_rejects_blank_message(monkeypatch):
    # Patch discord.TextChannel used within the module to accept DummyChannel
    monkeypatch.setattr(scheduler_module.discord, "TextChannel", DummyChannel)

    config = BotConfig(
        token="token",
        guild_id=1,
        test_guild_id=1,
        admin_ids=(1,),
        ready_channel_id=1,
        report_log_channel_id=1,
        f1_channels={},
        schedule_path=Path("/tmp/schedules.json"),
        default_timezone=pytz.utc,
    )

    manager = DummyManager()

    # Create cog instance without running scheduler loop
    cog = ScheduleCog.__new__(ScheduleCog)
    cog.bot = SimpleNamespace()
    cog.config = config
    cog.manager = manager

    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1),
        id=42,
        guild=SimpleNamespace(id=7),
        channel=DummyChannel(5),
        response=response,
    )

    await ScheduleCog.schedule.callback(
        cog,
        interaction,
        kind="message",
        when="01.01.2099 10:00",
        channel=DummyChannel(5),
        content="   ",
    )

    response.send_message.assert_awaited_once()
    awaited = response.send_message.await_args
    assert awaited.kwargs["ephemeral"] is True
    assert "Provide" in awaited.args[0]
    assert manager.added_jobs == []
