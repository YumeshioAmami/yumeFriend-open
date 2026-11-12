"""P0：游戏适配器契约（D250 / D251）。"""

import pytest

from backend.game.contract import (
    PROTOCOL_VERSION,
    Capabilities,
    ContractError,
    GameEvent,
    UnsupportedVersionError,
)


def _payload(**overrides):
    payload = {
        "game": "mock",
        "display_name": "Mock Game",
        "protocol_version": PROTOCOL_VERSION,
        "adapter_version": "0.0.1",
        "supported_versions": ["1.20.4", "1.21.1"],
        "actions": {
            "goto": {"params": {"x": "number", "y": "number"}, "description": "走过去"},
            "say": {"params": {"text": "string"}},
        },
        "limits": {"max_tasks": 4},
    }
    payload.update(overrides)
    return payload


def test_capabilities_parses_and_normalizes():
    caps = Capabilities.from_payload(_payload())
    assert caps.game == "mock"
    assert caps.protocol_version == PROTOCOL_VERSION
    assert caps.supported_versions == ("1.20.4", "1.21.1")
    assert caps.action_names() == ("goto", "say")
    assert caps.limits == {"max_tasks": 4}


def test_protocol_mismatch_is_rejected():
    with pytest.raises(ContractError) as exc:
        Capabilities.from_payload(_payload(protocol_version=99))
    assert "协议版本" in str(exc.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"game": ""},
        {"actions": {}},
        {"actions": ["goto"]},
        {"supported_versions": "1.20.4"},
    ],
)
def test_malformed_capabilities_are_rejected(overrides):
    with pytest.raises(ContractError):
        Capabilities.from_payload(_payload(**overrides))


def test_supports_version():
    caps = Capabilities.from_payload(_payload())
    assert caps.supports_version("1.20.4") is True
    assert caps.supports_version("1.21.1") is True
    assert caps.supports_version("1.22") is False
    # 没探测到版本就不拦（交给适配器判断）
    assert caps.supports_version(None) is True


def test_prompt_lines_include_params_and_description():
    caps = Capabilities.from_payload(_payload())
    lines = caps.prompt_lines()
    assert lines[0] == "- goto(x, y)  走过去"
    assert lines[1] == "- say(text)"


def test_unsupported_version_error_is_a_contract_error():
    assert issubclass(UnsupportedVersionError, ContractError)


def test_event_requires_type():
    with pytest.raises(ContractError):
        GameEvent.from_payload({"event_id": "x"})


def test_event_generates_id_when_missing():
    event = GameEvent.from_payload({"type": "chat", "text": "hi"})
    assert event.type == "chat"
    assert event.event_id.startswith("chat-")
    assert event.payload["text"] == "hi"


def test_unknown_event_type_is_kept_for_forward_compatibility():
    event = GameEvent.from_payload({"type": "future_thing", "event_id": "e1"})
    assert event.type == "future_thing"
