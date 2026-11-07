import pytest

from denon_link import protocol as p


@pytest.mark.parametrize(
    "param, db",
    [("80", 0.0), ("40", -40.0), ("405", -39.5), ("34", -46.0), ("00", -80.0), ("98", 18.0)],
)
def test_parse_volume(param, db):
    assert p.parse_volume(param) == db


@pytest.mark.parametrize("param", ["MAX 98", "", "UP", "4x"])
def test_parse_volume_rejects_non_numeric(param):
    assert p.parse_volume(param) is None


def test_format_volume_main_half_steps_and_clamp():
    assert p.format_volume(-40, half_steps=True) == "40"
    assert p.format_volume(-39.5, half_steps=True) == "405"
    assert p.format_volume(-39.7, half_steps=True) == "405"  # nearest 0.5
    assert p.format_volume(-75.5, half_steps=True) == "045"
    assert p.format_volume(50, half_steps=True) == "98"  # clamped to +18 dB
    assert p.format_volume(-100, half_steps=True) == "00"


def test_format_volume_zone_whole_db():
    assert p.format_volume(-46, half_steps=False) == "34"
    assert p.format_volume(-45.6, half_steps=False) == "34"


def test_zone_names_and_prefixes():
    assert p.zone_name(1) == "Main"
    assert p.zone_name("2") == "Zone2"
    assert p.zone_prefix("Zone3") == "Z3"
    assert p.zone_prefix("Main") == ""
    with pytest.raises(ValueError):
        p.zone_name(4)


@pytest.mark.parametrize(
    "zone, event, param, expected",
    [
        ("Main", "ZM", "ON", ("Main", {"power": True})),
        ("Main", "ZM", "OFF", ("Main", {"power": False})),
        ("Zone2", "Z2", "ON", ("Zone2", {"power": True})),
        ("Zone2", "Z2", "OFF", ("Zone2", {"power": False})),
        ("All", "PW", "STANDBY", ("All", {"power": False})),
        ("Main", "SI", "SAT/CBL", ("Main", {"source": "SAT/CBL"})),
        ("Zone2", "SI", "SOURCE", ("Zone2", {"source": "SOURCE"})),
        ("Main", "MV", "405", ("Main", {"volume": -39.5})),
        ("Zone2", "MV", "34", ("Zone2", {"volume": -46.0})),
        ("Zone2", "MU", "ON", ("Zone2", {"muted": True})),
    ],
)
def test_state_changes(zone, event, param, expected):
    change = p.state_changes(zone, event, param)
    assert (change.zone, change.values) == expected


@pytest.mark.parametrize(
    "zone, event, param",
    [
        ("All", "PW", "ON"),  # zones report their own power
        ("Zone2", "SLP", "OFF"),  # sleep timer: Cynical Denon misread this as a source
        ("Main", "MS", "DOLBY DIGITAL"),
        ("Main", "MV", "MAX 98"),
        ("Main", "SI", ""),
    ],
)
def test_untracked_events(zone, event, param):
    assert p.state_changes(zone, event, param) is None


def test_commands():
    assert p.power_command("Main", True) == "ZMON"
    assert p.power_command("Zone2", False) == "Z2OFF"
    assert p.source_command("Main", "CD") == "SICD"
    assert p.source_command("Zone2", "SOURCE") == "Z2SOURCE"
    assert p.volume_command("Main", -39.5) == "MV405"
    assert p.volume_command("Zone2", -46) == "Z234"
    assert p.volume_step_command("Zone3", up=False) == "Z3DOWN"
    assert p.mute_command("Main", True) == "MUON"
    assert p.mute_command("Zone2", False) == "Z2MUOFF"
    with pytest.raises(ValueError):
        p.source_command("Main", " ")


def test_status_queries_cover_each_zone():
    assert p.status_queries(["Zone2"]) == ["ZM?", "SI?", "MV?", "MU?", "Z2?", "Z2MU?"]
