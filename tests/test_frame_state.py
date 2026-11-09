"""Frame regressions against real method bodies, without a Home Assistant runtime.

Run with pytest --noconftest -o addopts= tests/test_frame_state.py.
Transport dependencies are stubbed; the state/control methods come from source.
Live device validation remains necessary for firmware and wake behavior.
"""

import ast
import asyncio
import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "samsungtv_smart"


class ArtModeStatus(Enum):
    Unsupported = 0
    Unavailable = 1
    Off = 2
    On = 3


def methods(file, cls, names, namespace):
    tree = ast.parse((ROOT / file).read_text())
    body = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    nodes = [
        n
        for n in body.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    for node in nodes:
        node.decorator_list = []
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / file), "exec"),
        namespace,
    )
    return SimpleNamespace(**{node.name: namespace[node.name] for node in nodes})


NS = {
    "ArtModeStatus": ArtModeStatus,
    "_LOGGER": logging.getLogger("test"),
    "_LOGGING": logging.getLogger("test"),
    "_process_api_response": json.loads,
    "ATTR_IP_ADDRESS": "ip_address",
    "ATTR_ART_MODE_STATUS": "art_mode_status",
    "STATE_ON": "on",
    "STATE_OFF": "off",
}
MEDIA = methods(
    "media_player.py",
    "SamsungTVDevice",
    [
        "_check_status",
        "_get_art_status",
        "extra_state_attributes",
        "_async_power_on",
        "async_set_art_mode",
    ],
    dict(
        NS,
        MediaPlayerState=SimpleNamespace(OFF="off", ON="on"),
        ArtModeSupport=SimpleNamespace(PARTIAL="partial", FULL="full"),
    ),
)
WS = methods(
    "api/samsungws.py",
    "SamsungTVWS",
    ["_handle_artmode_status", "_check_art_mode"],
    dict(NS, datetime=datetime, MAX_WS_PING_INTERVAL=10),
)


def bind_art_status(device):
    device._last_valid_art_status = None
    device._get_art_status = lambda: MEDIA._get_art_status(device)
    return device


def test_frame_modes_and_rest_failure():
    for power, art, expected_state, expected_mode in [
        ("on", ArtModeStatus.On, False, "art"),
        ("on", ArtModeStatus.Off, True, "tv"),
        ("standby", ArtModeStatus.On, False, "off"),
        ("standby", ArtModeStatus.Off, False, "off"),
        ("on", ArtModeStatus.Unavailable, False, "unknown"),
        (None, ArtModeStatus.On, False, "unknown"),
    ]:
        device = SimpleNamespace(
            _device_info={"device": {"PowerState": "on"}},
            _host="tv",
            _st=None,
            _ws=SimpleNamespace(artmode_status=art, is_connected=True),
            entity_id="media_player.tv",
            _get_device_spec=lambda key: "true" if key == "FrameTVSupport" else "on",
            _async_load_device_info=AsyncMock(
                return_value={"device": {"PowerState": power}} if power else None
            ),
        )
        bind_art_status(device)
        assert asyncio.run(MEDIA._check_status(device)) is expected_state
        assert MEDIA.extra_state_attributes(device)["tv_mode"] == expected_mode
        if art == ArtModeStatus.Unavailable:
            assert (
                MEDIA.extra_state_attributes(device)["art_mode_status"] == "unavailable"
            )


def test_current_and_legacy_art_protocol_and_invalid_values():
    device = SimpleNamespace(
        _power_on_requested=False,
        _artmode_status=ArtModeStatus.Unavailable,
        _status_callback=Mock(),
    )
    for event, field, value, expected in [
        ("get_artmode_status", "value", "on", ArtModeStatus.On),
        ("art_mode_changed", "status", "off", ArtModeStatus.Off),
        ("artmode_status", "value", "on", ArtModeStatus.On),
        ("get_artmode_status", "value", "bad", ArtModeStatus.On),
        ("go_to_standby", "value", "", ArtModeStatus.Unavailable),
    ]:
        WS._handle_artmode_status(
            device, {"data": json.dumps({"event": event, field: value})}
        )
        assert device._artmode_status == expected
    assert device._status_callback.call_count == 4


def test_pending_art_request_corrects_tv_once():
    device = SimpleNamespace(
        _power_on_requested=True,
        _power_on_artmode=True,
        _artmode_status=ArtModeStatus.Off,
        _status_callback=Mock(),
        send_key=Mock(),
    )
    response = {"data": json.dumps({"event": "get_artmode_status", "value": "off"})}
    WS._handle_artmode_status(device, response)
    WS._handle_artmode_status(device, response)
    device.send_key.assert_called_once_with("KEY_POWER", key_press_delay=0)
    assert not device._power_on_requested


def test_art_status_reconciliation_poll():
    device = SimpleNamespace(
        _artmode_status=ArtModeStatus.On,
        _ws_art=Mock(),
        _last_art_ping=datetime.utcnow(),
        _last_art_query=datetime.min,
        _get_artmode_status=Mock(),
    )
    WS._check_art_mode(device)
    device._get_artmode_status.assert_called_once()
    device._last_art_query = datetime.utcnow()
    WS._check_art_mode(device)
    device._get_artmode_status.assert_called_once()


def test_standby_ignores_cached_art_mode_when_waking():
    for power, art in (
        (power, art) for power in ("off", "standby", None) for art in ArtModeStatus
    ):
        ws = SimpleNamespace(artmode_status=art, set_power_on_request=Mock())
        device = SimpleNamespace(
            _physical_power_state=power,
            _ws=ws,
            state="off",
            async_send_command=AsyncMock(return_value=True),
        )
        assert asyncio.run(MEDIA._async_power_on(device, True)) is True
        device.async_send_command.assert_awaited_once_with("KEY_POWER")
        ws.set_power_on_request.assert_called_once_with(True)


def test_awake_art_request_is_idempotent():
    ws = SimpleNamespace(artmode_status=ArtModeStatus.On, set_power_on_request=Mock())
    device = SimpleNamespace(
        _physical_power_state="on",
        _ws=ws,
        state="off",
        async_send_command=AsyncMock(return_value=True),
    )
    assert asyncio.run(MEDIA._async_power_on(device, True)) is False
    device.async_send_command.assert_not_awaited()
    ws.set_power_on_request.assert_not_called()


def test_ha_startup_in_standby_learns_power_before_waking():
    device = SimpleNamespace(
        _device_info=None,
        _physical_power_state=None,
        _host="tv",
        _st=None,
        _ws=SimpleNamespace(artmode_status=ArtModeStatus.On),
        entity_id="media_player.tv",
    )

    def spec(key):
        return (device._device_info or {}).get("device", {}).get(key)

    async def load(force=False):
        assert force
        device._device_info = {
            "device": {"PowerState": "standby", "FrameTVSupport": "true"}
        }
        return device._device_info

    bind_art_status(device)
    device._get_device_spec = spec
    device._async_load_device_info = load
    assert asyncio.run(MEDIA._check_status(device)) is False
    assert device._physical_power_state == "standby"
    assert MEDIA.extra_state_attributes(device)["tv_mode"] == "off"


def test_art_service_wakes_a_frame_before_art_channel_discovery():
    for power in ("off", "standby", None):
        device = SimpleNamespace(
            _physical_power_state=power,
            _state="off",
            support_art_mode="partial",
            _async_turn_on=AsyncMock(),
        )
        asyncio.run(MEDIA.async_set_art_mode(device))
        device._async_turn_on.assert_awaited_once_with(True)


def test_deep_standby_requires_known_capabilities_and_both_transports_offline():
    for spec, connected, expected in [
        ({"PowerState": "on", "FrameTVSupport": "true"}, False, "off"),
        ({"FrameTVSupport": "true"}, False, "off"),
        ({"FrameTVSupport": "true"}, True, "unknown"),
        ({}, False, None),
    ]:
        device = bind_art_status(
            SimpleNamespace(
                _device_info={"device": spec} if spec else None,
                _physical_power_state=None,
                _host="tv",
                _st=None,
                _ws=SimpleNamespace(
                    artmode_status=ArtModeStatus.On, is_connected=connected
                ),
                _get_device_spec=spec.get,
                _async_load_device_info=AsyncMock(return_value=None),
            )
        )
        assert asyncio.run(MEDIA._check_status(device)) is False
        attrs = MEDIA.extra_state_attributes(device)
        assert attrs.get("tv_mode") == expected
        assert device._physical_power_state == ("off" if expected == "off" else None)


def test_awake_art_reconnect_retains_last_valid_mode_but_not_raw_status():
    for art, expected, playing in [
        (ArtModeStatus.On, "art", False),
        (ArtModeStatus.Off, "tv", True),
    ]:
        device = bind_art_status(
            SimpleNamespace(
                _device_info={"device": {"PowerState": "on"}},
                _host="tv",
                _st=None,
                _ws=SimpleNamespace(artmode_status=art, is_connected=True),
                _get_device_spec=lambda key: (
                    "true" if key == "FrameTVSupport" else "on"
                ),
                _async_load_device_info=AsyncMock(
                    return_value={"device": {"PowerState": "on"}}
                ),
            )
        )
        assert asyncio.run(MEDIA._check_status(device)) is playing
        device._ws.artmode_status = ArtModeStatus.Unavailable
        assert asyncio.run(MEDIA._check_status(device)) is playing
        attrs = MEDIA.extra_state_attributes(device)
        assert attrs["tv_mode"] == expected
        assert attrs["art_mode_status"] == "unavailable"
        device._async_load_device_info.return_value = {
            "device": {"PowerState": "standby"}
        }
        assert asyncio.run(MEDIA._check_status(device)) is False
        assert MEDIA.extra_state_attributes(device)["tv_mode"] == "off"
        device._ws.is_connected = False
        device._async_load_device_info.return_value = None
        assert asyncio.run(MEDIA._check_status(device)) is False
        assert MEDIA.extra_state_attributes(device)["tv_mode"] == "off"
