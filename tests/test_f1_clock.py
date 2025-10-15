from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
import importlib.util

import pytest
import pytz


REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_module(module_name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if "discord" not in sys.modules:
    discord_module = ModuleType("discord")
    discord_module.NotFound = type("NotFound", (Exception,), {})
    discord_module.DiscordException = Exception
    discord_abc_module = ModuleType("discord.abc")

    class GuildChannel:
        pass

    discord_abc_module.GuildChannel = GuildChannel
    discord_module.abc = discord_abc_module
    sys.modules["discord"] = discord_module
    sys.modules["discord.abc"] = discord_abc_module
else:
    discord_module = sys.modules["discord"]
    discord_abc_module = sys.modules.setdefault(
        "discord.abc", ModuleType("discord.abc")
    )


if "leo_bot" not in sys.modules:
    pkg = ModuleType("leo_bot")
    pkg.__path__ = [str(REPO_ROOT / "leo_bot")]
    sys.modules["leo_bot"] = pkg
if "leo_bot.cogs" not in sys.modules:
    subpkg = ModuleType("leo_bot.cogs")
    subpkg.__path__ = [str(REPO_ROOT / "leo_bot" / "cogs")]
    sys.modules["leo_bot.cogs"] = subpkg


config_module = _import_module("leo_bot.config", "leo_bot/config.py")
f1_clock_module = _import_module("leo_bot.cogs.f1_clock", "leo_bot/cogs/f1_clock.py")


BotConfig = config_module.BotConfig
F1ClockCog = f1_clock_module.F1ClockCog
discord_abc = discord_module.abc


class DummyBot:
    def __init__(self):
        self._ready = asyncio.Event()

    async def wait_until_ready(self) -> None:
        await self._ready.wait()


class DummyGuildChannel(discord_abc.GuildChannel):
    def __init__(self, channel_id: int, name: str):
        self.id = channel_id
        self.name = name
        self.edit = AsyncMock()


def make_config() -> BotConfig:
    return BotConfig(
        token="token",
        guild_id=1,
        test_guild_id=1,
        admin_ids=(1,),
        ready_channel_id=1,
        report_log_channel_id=1,
        f1_channels={"eventname": 11, "date": 12, "countdown": 13},
        schedule_path=Path("/tmp/schedules.json"),
        default_timezone=pytz.utc,
    )


@pytest.mark.asyncio
async def test_clock_loop_runs_initial_update(monkeypatch):
    bot = DummyBot()
    config = make_config()
    cog = F1ClockCog(bot, config)

    update_mock = AsyncMock()
    sleep_mock = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(cog, "update_channels", update_mock)
    monkeypatch.setattr(cog, "_sleep_until_next_mark", sleep_mock)

    task = asyncio.create_task(cog._clock_loop())
    await asyncio.sleep(0)
    bot._ready.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    update_mock.assert_awaited_once()
    sleep_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_channels_schedules_expected_renames(monkeypatch):
    config = make_config()

    channels = {
        11: DummyGuildChannel(11, "old-event"),
        12: DummyGuildChannel(12, "old-date"),
        13: DummyGuildChannel(13, "old-countdown"),
    }

    class Bot(SimpleNamespace):
        def get_channel(self, channel_id: int):
            return channels.get(channel_id)

    bot = Bot()
    cog = F1ClockCog(bot, config)

    session_time = datetime.utcnow() + timedelta(hours=1)

    monkeypatch.setattr(
        f1_clock_module,
        "find_next_session",
        lambda tz: ({"EventName": "Race"}, "Q", session_time),
    )
    monkeypatch.setattr(
        f1_clock_module,
        "format_session_channel_strings",
        lambda event, code, dt, tz: ("Event", "Date", "Time", "Countdown"),
    )

    scheduled = []

    def capture_schedule(channel, channel_id, target_name):
        scheduled.append((channel_id, target_name))

    monkeypatch.setattr(cog, "_schedule_channel_rename", capture_schedule)

    await cog.update_channels()

    assert scheduled == [
        (11, "Event"),
        (12, "Date • Time"),
        (13, "Countdown"),
    ]


@pytest.mark.asyncio
async def test_schedule_channel_rename_skips_duplicate_pending_targets():
    config = make_config()
    bot = DummyBot()
    cog = F1ClockCog(bot, config)

    class SlowChannel(DummyGuildChannel):
        def __init__(self, channel_id: int, name: str):
            super().__init__(channel_id, name)
            self._gate = asyncio.Event()

        async def edit(self, *, name: str, reason: str):  # type: ignore[override]
            await self._gate.wait()

        def release(self):
            self._gate.set()

        def reset(self):
            self._gate = asyncio.Event()

    channel = SlowChannel(42, "old")

    cog._schedule_channel_rename(channel, 42, "target")

    assert len(cog._rename_tasks) == 1
    stored_target, stored_task = cog._rename_tasks[42]
    assert stored_target == "target"

    cog._schedule_channel_rename(channel, 42, "target")
    assert cog._rename_tasks[42][1] is stored_task

    channel.release()
    await asyncio.sleep(0)

    assert 42 not in cog._rename_tasks

    channel.reset()
    cog._schedule_channel_rename(channel, 42, "target")
    assert len(cog._rename_tasks) == 1

    channel.release()
    await asyncio.sleep(0)

    assert 42 not in cog._rename_tasks

