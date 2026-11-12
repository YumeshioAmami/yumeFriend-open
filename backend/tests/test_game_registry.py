"""P1：适配器注册表（D250）。"""

import pytest

from backend.game import registry


def test_registry_has_mock_and_minecraft():
    mock = registry.command_for("mock")
    assert mock[-1].replace("\\", "/").endswith("adapters/mock/mock_adapter.py")

    mc = registry.command_for("minecraft")
    assert mc[-1].replace("\\", "/").endswith("adapters/minecraft/index.js")


def test_registry_unknown_kind_raises():
    with pytest.raises(KeyError):
        registry.command_for("doom")
