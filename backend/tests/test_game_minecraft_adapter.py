"""P1：真 Minecraft 适配器的**无服务器自检**（不需要开游戏）。

· 能力清单必须来自 mineflayer 自己（D252），不是手写；
· 连不上服务器时必须给一句人话，而不是静默失败。

真机进服 / 跟随 / 说话属于 P1 的人工验收（需要开世界），不放进自动测试。
"""

import shutil
from pathlib import Path

import pytest
import pytest_asyncio

from backend.game import registry
from backend.game.client import AdapterError
from backend.game.process import AdapterProcess

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
MODULES = ROOT / "adapters" / "minecraft" / "node_modules"

pytestmark = pytest.mark.skipif(
    not NODE or not MODULES.exists(),
    reason="需要 Node + adapters/minecraft/node_modules（先 npm install）",
)


@pytest_asyncio.fixture()
async def adapter():
    async with AdapterProcess(registry.command_for("minecraft"), ready_timeout=30.0) as client:
        yield client


@pytest.mark.asyncio
async def test_capabilities_come_from_mineflayer(adapter):
    caps = await adapter.capabilities()
    assert caps.game == "minecraft"
    # 完整数据时是 28；P4 裁剪后是保留的那几个 —— 两种都要能过
    assert len(caps.supported_versions) >= 5
    assert "1.20.1" in caps.supported_versions
    assert "1.20.4" in caps.supported_versions
    assert set(caps.action_names()) == {
        "follow",
        "mode",
        "goto",
        "say",
        "stop",
            "sequence",
            "rest",
            "wait",
            "patrol",
        "mine",
        "place",
        "dig_at",
        "fly",
        "fish",
        "shoot",
        "boat",
        "mount",
        "dismount",
        "use_item",
        "attack",
        "eat",
        "drop",
        "bring",
        "escape",
        "pillar",
        "craft",
        "smelt",
        "equip",
        "command",
        "inspect_blocks",
        "survey_area",
        "knowledge_query",
        "configure_reflex",
        "plugin_list",
        "plugin_call",
    }


@pytest.mark.asyncio
async def test_unreachable_server_gives_human_error(adapter):
    with pytest.raises(AdapterError) as exc:
        await adapter.start_session(
            {"host": "127.0.0.1", "port": 1, "username": "Xiyin_"}
        )
    assert exc.value.code == "CONNECT_FAILED"
    assert "世界开着吗" in str(exc.value)
