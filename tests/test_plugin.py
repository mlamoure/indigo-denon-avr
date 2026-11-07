import pytest

import plugin as plugin_mod
from denon_link.link import EVENT_CONNECTION, EVENT_ERROR, EVENT_INFO, EVENT_ZONE, Event
from fakes import FakeLink


@pytest.fixture
def plugin(indigo, monkeypatch):
    FakeLink.instances.clear()
    monkeypatch.setattr(plugin_mod, "LINK_FACTORY", FakeLink)
    return plugin_mod.Plugin("com.vtmikel.denon", "Denon AVR", "2026.11.0", {})


@pytest.fixture
def receiver(make_device):
    return make_device(
        "Denon AVR - Living Room (Zone 1)",
        "receiver",
        props={"address": "192.0.2.52"},
        states={"power": True, "state": "on"},
    )


@pytest.fixture
def zone2(make_device, receiver):
    return make_device(
        "Denon AVR - Patio (Zone 2)",
        "zone",
        props={"receiver": str(receiver.id), "zone": "2"},
        # Indigo still believes Zone 2 is on (the stale state seen on 2026-11-07).
        states={"power": True, "state": "on", "source": "SOURCE", "volume": -40.0},
    )


def started(plugin, *devs):
    for dev in devs:
        plugin.deviceStartComm(dev)
    plugin._sync_links()
    return FakeLink.instances[-1] if FakeLink.instances else None


def deliver(plugin, receiver, *events):
    for event in events:
        plugin.events.put((receiver.id, event))
    plugin._drain_events()


def test_one_link_per_receiver_with_its_zones(plugin, receiver, zone2):
    link = started(plugin, receiver, zone2)
    assert link.started and link.host == "192.0.2.52" and link.zones == ["Zone2"]
    plugin._sync_links()
    assert len(FakeLink.instances) == 1  # no restart when nothing changed


def test_zone_starting_after_receiver_restarts_link(plugin, receiver, zone2):
    started(plugin, receiver)
    first = FakeLink.instances[-1]
    assert first.zones == []
    started(plugin, zone2)
    assert first.stopped
    assert FakeLink.instances[-1].zones == ["Zone2"]


def test_stale_on_state_is_corrected_from_receiver_report(plugin, receiver, zone2):
    started(plugin, receiver, zone2)
    deliver(plugin, receiver, Event(EVENT_ZONE, "Zone2", {"power": False, "source": "SOURCE"}))
    assert zone2.states["power"] is False
    assert zone2.states["state"] == "off"
    assert zone2.images[-1] == "PowerOff"


def test_unchanged_values_are_not_rewritten(plugin, receiver, zone2):
    started(plugin, receiver, zone2)
    deliver(plugin, receiver, Event(EVENT_ZONE, "Zone2", {"power": True, "volume": -40.0}))
    assert zone2.state_updates == []


def test_standby_turns_every_zone_off(plugin, receiver, zone2):
    started(plugin, receiver, zone2)
    deliver(plugin, receiver, Event(EVENT_ZONE, "All", {"power": False}))
    assert receiver.states["power"] is False
    assert zone2.states["power"] is False


def test_volume_written_with_one_decimal_and_ui_value(plugin, receiver, zone2):
    started(plugin, receiver, zone2)
    deliver(plugin, receiver, Event(EVENT_ZONE, "Main", {"volume": -39.5}))
    change = receiver.state_updates[-1][0]
    assert change == {"key": "volume", "value": -39.5, "uiValue": "-39.5 dB", "decimalPlaces": 1}


def test_connection_info_and_error_events(plugin, receiver, zone2):
    started(plugin, receiver, zone2)
    deliver(plugin, receiver, Event(EVENT_ERROR, values={"message": "setup failed"}))
    assert receiver.errorState == "no connection" and zone2.errorState == "no connection"
    deliver(
        plugin,
        receiver,
        Event(EVENT_INFO, "Main", {"model": "Denon AVR-X3700H", "sources": {"CD": "Sonos"}}),
        Event(EVENT_CONNECTION, values={"connected": True}),
    )
    assert receiver.errorState is None and zone2.errorState is None
    assert receiver.states["model"] == "Denon AVR-X3700H"
    assert receiver.states["connected"] is True
    assert plugin.sources[receiver.id] == {"CD": "Sonos"}


class Action:
    def __init__(self, **props):
        self.props = props


@pytest.mark.parametrize(
    "method, props, expected",
    [
        ("set_power", {"value": "on"}, "Z2ON"),
        ("set_power", {"value": "off"}, "Z2OFF"),
        ("set_power", {"value": "toggle"}, "Z2OFF"),  # state says on
        ("set_source", {"source": "SOURCE"}, "Z2SOURCE"),
        ("set_source", {"source": "other", "source_other": "CD"}, "Z2CD"),
        ("set_volume", {"volume": "-46"}, "Z234"),
        ("adjust_volume", {"direction": "down"}, "Z2DOWN"),
        ("set_mute", {"value": "on"}, "Z2MUON"),
        ("send_command", {"command": "Z2SLP60"}, "Z2SLP60"),
    ],
)
def test_zone_actions_send_protocol_commands(plugin, receiver, zone2, method, props, expected):
    link = started(plugin, receiver, zone2)
    getattr(plugin, method)(Action(**props), zone2)
    assert link.commands[-1] == ("Zone2", expected)


def test_receiver_actions_use_main_zone_commands(plugin, receiver, zone2):
    link = started(plugin, receiver, zone2)
    plugin.set_source(Action(source="CD"), receiver)
    plugin.set_volume(Action(volume="-39.5"), receiver)
    plugin.set_power(Action(value="off"), receiver)
    assert link.commands == [("Main", "SICD"), ("Main", "MV405"), ("Main", "ZMOFF")]


def test_bad_volume_is_logged_not_sent(plugin, receiver, zone2):
    link = started(plugin, receiver, zone2)
    plugin.set_volume(Action(volume="loud"), zone2)
    assert link.commands == []


def test_refresh_action(plugin, receiver, zone2):
    link = started(plugin, receiver, zone2)
    plugin.refresh(Action(), zone2)
    assert link.refreshed == 1


def test_stop_comm_stops_link(plugin, receiver, zone2):
    link = started(plugin, receiver, zone2)
    plugin.deviceStopComm(receiver)
    assert link.stopped and receiver.id not in plugin.links


def test_source_menu_lists_receiver_inputs(plugin, receiver, zone2):
    started(plugin, receiver, zone2)
    plugin.sources[receiver.id] = {"CD": "Sonos", "SOURCE": "Follow main zone"}
    zone_items = plugin.sourceMenu(targetId=zone2.id)
    main_items = plugin.sourceMenu(targetId=receiver.id)
    assert ("CD", "Sonos (CD)") in zone_items
    assert ("SOURCE", "Follow main zone (SOURCE)") in zone_items
    assert all(code != "SOURCE" for code, _ in main_items)
    assert zone_items[-1][0] == "other"


def test_zone_config_validation(plugin, receiver):
    ok, values = plugin.validateDeviceConfigUi(
        {"receiver": str(receiver.id), "zone": "2"}, "zone", 0
    )
    assert ok and values["address"] == f"{receiver.name} Zone 2"
    ok, _values, errors = plugin.validateDeviceConfigUi({"receiver": "0", "zone": "4"}, "zone", 0)
    assert not ok and set(errors) == {"receiver", "zone"}


def test_action_descriptions(plugin, zone2):
    ok, values = plugin.validateActionConfigUi({"value": "on"}, "set_power", zone2.id)
    assert ok and values["description"] == f"{zone2.name}: power on"
    ok, _v, errors = plugin.validateActionConfigUi(
        {"source": "other", "source_other": ""}, "set_source", zone2.id
    )
    assert not ok and "source_other" in errors


def test_unreachable_receiver_marks_every_device_unavailable_until_it_answers(
    plugin, receiver, zone2
):
    started(plugin, receiver, zone2)
    deliver(plugin, receiver, Event(EVENT_ERROR, values={"message": "status read failed"}))
    assert receiver.states["state"] == "unavailable"
    assert zone2.states["state"] == "unavailable"
    deliver(plugin, receiver, Event(EVENT_ZONE, "Zone2", {"power": True}))
    assert zone2.states["state"] == "on"
    assert receiver.errorState is None and zone2.errorState is None


def test_telnet_outage_alone_does_not_make_devices_unavailable(plugin, receiver, zone2):
    started(plugin, receiver, zone2)
    deliver(plugin, receiver, Event(EVENT_CONNECTION, values={"connected": False}))
    assert receiver.states["connected"] is False
    assert receiver.states["state"] == "on"
