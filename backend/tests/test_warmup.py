"""D46 验收：开机预热。

预热的价值全在"连接是热的"，所以这里只验证行为：
没配 provider 要安静跳过；配了要真去打一次；失败绝不影响启动。
"""

import pytest

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.llm import warmup
from backend.llm.errors import LLMConfigError
from backend.services import settings_service


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "warmup.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    create_app()  # 会跑迁移把表建好（不启动 lifespan）
    return db_file


class _FakeProvider:
    def __init__(self) -> None:
        self.probed = 0
        self.closed = 0
        self.error: Exception | None = None

    async def probe(self) -> None:
        self.probed += 1
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed += 1


def _configure(fake: _FakeProvider, monkeypatch) -> _FakeProvider:
    with db.session_scope() as s:
        settings_service.set_llm_bases(
            s,
            [
                {
                    "name": "nanogpt",
                    "base_url": "https://example.com/v1",
                    "api_key": "k",
                    "model": "m",
                }
            ],
            active="nanogpt",
        )
    monkeypatch.setattr(warmup, "create_provider", lambda *_a, **_k: fake)
    return fake


async def test_skips_quietly_when_nothing_configured(temp_db):
    """没配 provider → 安静跳过（走真实的 factory，让它抛 LLMConfigError）。"""
    assert await warmup.warm_up_provider(delay=0) is False


async def test_probes_and_releases_the_provider(temp_db, monkeypatch):
    fake = _configure(_FakeProvider(), monkeypatch)
    assert await warmup.warm_up_provider(delay=0) is True
    assert fake.probed == 1
    assert fake.closed == 1


async def test_failure_never_propagates(temp_db, monkeypatch):
    """预热炸了只是记个日志——绝不能把启动带崩。"""
    fake = _FakeProvider()
    fake.error = RuntimeError("boom")
    _configure(fake, monkeypatch)
    assert await warmup.warm_up_provider(delay=0) is False


async def test_config_error_is_swallowed(temp_db, monkeypatch):
    def _boom(*_a, **_k):
        raise LLMConfigError("没配")

    monkeypatch.setattr(warmup, "create_provider", _boom)
    assert await warmup.warm_up_provider(delay=0) is False


# ---------- D87：定时预热 ----------


@pytest.fixture(autouse=True)
def _clean_warmup_state():
    """模块级"上次预热时间"是全局状态，每个用例前后都清一下，免得互相影响。"""
    warmup.reset_state()
    yield
    warmup.reset_state()


async def test_warmup_tick_respects_the_interval(temp_db, monkeypatch):
    """到点了才探；没到点一次都不打上游（D87）。"""
    fake = _configure(_FakeProvider(), monkeypatch)

    assert await warmup.warmup_tick(enabled=True, interval_minutes=5) is True
    assert fake.probed == 1
    # 同一个时间点再来一次 → 还没到间隔，不该探
    assert await warmup.warmup_tick(enabled=True, interval_minutes=5) is False
    assert fake.probed == 1
    # 假装上次是 6 分钟前 → 该探了
    warmup._last_warm_at -= 6 * 60
    assert await warmup.warmup_tick(enabled=True, interval_minutes=5) is True
    assert fake.probed == 2


async def test_warmup_tick_can_be_turned_off(temp_db, monkeypatch):
    fake = _configure(_FakeProvider(), monkeypatch)
    assert await warmup.warmup_tick(enabled=False, interval_minutes=5) is False
    assert fake.probed == 0


async def test_warmup_tick_counts_failures_too(temp_db, monkeypatch):
    """探失败也记一次时间 —— 否则会每分钟狂试（把日志和网络都刷爆）。"""
    fake = _FakeProvider()
    fake.error = RuntimeError("boom")
    _configure(fake, monkeypatch)

    assert await warmup.warmup_tick(enabled=True, interval_minutes=5) is False
    assert fake.probed == 1
    assert await warmup.warmup_tick(enabled=True, interval_minutes=5) is False
    assert fake.probed == 1  # 没到间隔，不重试


def test_spawn_warm_up_resets_the_clock(temp_db, monkeypatch):
    """开机预热过一次 → 定时任务不该一分钟后又来一次。"""
    fake = _configure(_FakeProvider(), monkeypatch)
    monkeypatch.setattr(warmup, "_MAIN_LOOP", None)  # 没有事件循环 → 只记时间，不真跑
    warmup.spawn_warm_up(delay=0)  # 没有事件循环 → 静默跳过，但时间戳要记上
    assert warmup._last_warm_at > 0
    assert fake.probed == 0  # 确实没探（没循环就不跑）
