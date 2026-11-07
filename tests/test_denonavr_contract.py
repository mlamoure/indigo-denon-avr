"""Contract tests against the real denonavr library (the pinned version).

These pin the library behaviour the plugin relies on, so a Renovate bump that changes it fails
CI instead of the house:

- the telnet parser's (zone, event, parameter) split, fed through its own message handler;
- the public methods ReceiverLink calls;
- the private ``input._input_func_map`` (display name -> input code) ReceiverLink reads.
"""

import asyncio

import pytest

denonavr = pytest.importorskip("denonavr")
from denonavr.const import ALL_TELNET_EVENTS  # noqa: E402

from denon_link import protocol  # noqa: E402


def _parse(messages):
    async def run():
        avr = denonavr.DenonAVR("127.0.0.1", add_zones={"Zone2": None, "Zone3": None})
        seen = []
        avr.register_callback(ALL_TELNET_EVENTS, lambda z, e, p: seen.append((z, e, p)))
        out = {}
        for msg in messages:
            seen.clear()
            avr._device.telnet_api._process_event(msg)  # the library's own message handler
            out[msg] = list(seen)
        return out

    return asyncio.run(run())


def test_telnet_messages_reach_the_plugin_as_expected():
    got = _parse(
        [
            "ZMON",
            "Z2OFF",
            "Z2SOURCE",
            "Z2CD",
            "Z234",
            "Z2MUON",
            "Z2SLPOFF",
            "SISAT/CBL",
            "MV405",
            "PWSTANDBY",
            "Z3ON",
        ]
    )
    assert got["ZMON"] == [("Main", "ZM", "ON")]
    assert got["Z2OFF"] == [("Zone2", "Z2", "OFF")]
    assert got["Z2SOURCE"] == [("Zone2", "SI", "SOURCE")]
    assert got["Z2CD"] == [("Zone2", "SI", "CD")]
    assert got["Z234"] == [("Zone2", "MV", "34")]
    assert got["Z2MUON"] == [("Zone2", "MU", "ON")]
    assert got["Z2SLPOFF"] == [("Zone2", "SLP", "OFF")]
    assert got["SISAT/CBL"] == [("Main", "SI", "SAT/CBL")]
    assert got["MV405"] == [("Main", "MV", "405")]
    assert got["PWSTANDBY"] == [("All", "PW", "STANDBY")]
    assert got["Z3ON"] == [("Zone3", "Z3", "ON")]


def test_parsed_events_map_to_states():
    got = _parse(["Z2ON", "Z2SLPOFF", "Z234"])
    changes = [protocol.state_changes(*evt) for msgs in got.values() for evt in msgs]
    assert [c.values if c else None for c in changes] == [{"power": True}, None, {"volume": -46.0}]


def test_public_api_used_by_the_link():
    avr = denonavr.DenonAVR("127.0.0.1", add_zones={"Zone2": None})
    for name in (
        "async_setup",
        "async_update",
        "register_callback",
        "async_telnet_connect",
        "async_telnet_disconnect",
        "async_send_telnet_commands",
        "async_get_command",
    ):
        assert callable(getattr(avr, name)), name
    for prop in ("zones", "model_name", "telnet_connected", "telnet_healthy"):
        assert hasattr(type(avr), prop), prop
    zone = avr.zones["Zone2"]
    for prop in ("power", "volume", "muted", "input_func"):
        assert hasattr(type(zone), prop), prop


def test_private_input_map_still_exists():
    avr = denonavr.DenonAVR("127.0.0.1")
    assert isinstance(avr.input._input_func_map, dict)
