import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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

if "fastf1" not in sys.modules:
    fastf1_stub = ModuleType("fastf1")
    fastf1_stub.Cache = SimpleNamespace(enable_cache=lambda *_: None)

    def _raise_schedule(*args, **kwargs):  # pragma: no cover - used to satisfy imports
        raise RuntimeError("fastf1 schedule access not available in tests")

    fastf1_stub.get_event_schedule = _raise_schedule
    sys.modules["fastf1"] = fastf1_stub


betting_module = _import_module("leo_bot.cogs.betting", "leo_bot/cogs/betting.py")
_flip_comma_name = betting_module._flip_comma_name


def test_flip_comma_name_basic():
    assert _flip_comma_name("Verstappen, Max") == "Max Verstappen"


def test_flip_comma_name_preserves_outer_whitespace_and_accents():
    assert _flip_comma_name("  Pérez, Sergio  ") == "  Sergio Pérez  "


def test_flip_comma_name_ignores_missing_parts():
    assert _flip_comma_name("Ferrari") == "Ferrari"
    assert _flip_comma_name("Ferrari,") == "Ferrari,"
    assert _flip_comma_name(", Mercedes") == ", Mercedes"


def test_flip_comma_name_handles_none():
    assert _flip_comma_name(None) is None
