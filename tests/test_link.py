import threading
import time

from denon_link.link import (
    EVENT_CONNECTION,
    EVENT_INFO,
    EVENT_ZONE,
    HTTP_COMMAND_PATH,
    ReceiverLink,
)
from fakes import ALL_EVENTS, FakeAVR


class Collector:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event):
        with self.lock:
            self.events.append(event)

    def wait_for(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if any(predicate(e) for e in self.events):
                    return True
            time.sleep(0.02)
        return False

    def zone_values(self, zone):
        with self.lock:
            merged = {}
            for e in self.events:
                if e.kind == EVENT_ZONE and e.zone == zone:
                    merged.update(e.values)
            return merged


def make_link(collector, **fake_kwargs):
    holder = {}

    def factory(host, zones):
        # The link builds a new library object on every setup attempt; one shared fake keeps
        # its failure counters across attempts.
        if "avr" not in holder:
            holder["avr"] = FakeAVR(host, zones, **fake_kwargs)
        return holder["avr"]

    link = ReceiverLink(
        "10.0.0.5",
        ["Zone2"],
        collector,
        reconcile_seconds=30,
        avr_factory=factory,
        all_events_key=ALL_EVENTS,
    )
    return link, holder


def test_start_reports_full_state_including_off():
    c = Collector()
    link, holder = make_link(c)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and e.values["connected"])
        info = next(e for e in c.events if e.kind == EVENT_INFO)
        assert info.values["model"] == "Denon AVR-X3700H"
        assert info.values["sources"]["CD"] == "Sonos"
        zone2 = c.zone_values("Zone2")
        # The bug this plugin fixes: "off" must be reported, not skipped.
        assert zone2["power"] is False
        assert zone2["source"] == "SOURCE"
        assert zone2["muted"] is False
        main = c.zone_values("Main")
        assert main["power"] is True
        assert main["source"] == "SAT/CBL"
        assert main["source_name"] == "Living Room Apple TV"
    finally:
        link.stop()
    assert holder["avr"].disconnected


def test_telnet_events_become_zone_events_with_names():
    c = Collector()
    link, holder = make_link(c)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and e.values["connected"])
        holder["avr"].push("Zone2", "Z2", "ON")
        holder["avr"].push("Zone2", "SLP", "OFF")  # ignored
        holder["avr"].push("Main", "SI", "CD")
        assert c.wait_for(lambda e: e.kind == EVENT_ZONE and e.values.get("source") == "CD")
        assert c.zone_values("Zone2")["power"] is True
        assert c.zone_values("Main")["source_name"] == "Sonos"
        assert "SLPOFF" not in str(c.events)
    finally:
        link.stop()


def test_commands_go_over_telnet_when_healthy():
    c = Collector()
    link, holder = make_link(c)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and e.values["connected"])
        link.command("Zone2", "Z2ON")
        assert holder["avr"].telnet_sent[-1] == "Z2ON"
        assert holder["avr"].http_sent == []
    finally:
        link.stop()


def test_commands_fall_back_to_http_while_telnet_is_down():
    c = Collector()
    link, holder = make_link(c, telnet_failures=100)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and not e.values["connected"])
        before = holder["avr"].updates
        link.command("Zone2", "Z2SOURCE")
        assert holder["avr"].http_sent == [HTTP_COMMAND_PATH + "Z2SOURCE"]
        assert holder["avr"].updates > before  # read back over HTTP
    finally:
        link.stop()


def test_command_quotes_special_characters_for_http():
    c = Collector()
    link, holder = make_link(c, telnet_failures=100)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION)
        link.command("Main", "SISAT/CBL")
        assert holder["avr"].http_sent == [HTTP_COMMAND_PATH + "SISAT%2FCBL"]
    finally:
        link.stop()


def test_refresh_sends_status_queries():
    c = Collector()
    link, holder = make_link(c)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and e.values["connected"])
        link.refresh()
        end = time.monotonic() + 5
        while "Z2?" not in holder["avr"].telnet_sent and time.monotonic() < end:
            time.sleep(0.02)
        assert {"ZM?", "SI?", "MV?", "MU?", "Z2?", "Z2MU?"} <= set(holder["avr"].telnet_sent)
    finally:
        link.stop()


def test_setup_failure_is_reported_and_retried(monkeypatch):
    import denon_link.link as link_mod

    real_sleep = link_mod.ReceiverLink._sleep

    async def fast_sleep(self, seconds):
        return await real_sleep(self, 0.01)

    monkeypatch.setattr(link_mod.ReceiverLink, "_sleep", fast_sleep)
    c = Collector()
    link, holder = make_link(c, setup_failures=2)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_INFO)
        errors = [e for e in c.events if e.kind == "error"]
        assert len(errors) == 2
    finally:
        link.stop()


def test_stop_without_start_is_harmless():
    link = ReceiverLink("10.0.0.5", [], lambda e: None)
    link.stop()


def test_http_failure_with_telnet_down_reports_unreachable(monkeypatch):
    c = Collector()
    link, holder = make_link(c, telnet_failures=100)
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and not e.values["connected"])
        avr = holder["avr"]

        async def broken_update():
            raise OSError("no route to host")

        avr.async_update = broken_update
        link.refresh()
        assert c.wait_for(lambda e: e.kind == "error" and "status read" in e.values["message"])
    finally:
        link.stop()


def test_telnet_retries_while_http_keeps_working(monkeypatch):
    import denon_link.link as link_mod

    monkeypatch.setattr(link_mod, "TELNET_RETRY_START", 0.05)
    monkeypatch.setattr(link_mod, "TELNET_RETRY_MAX", 0.1)
    c = Collector()
    link, holder = make_link(c, telnet_failures=2)
    link._telnet_delay = 0.05
    link.start()
    try:
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and not e.values["connected"])
        assert c.zone_values("Zone2")["power"] is False  # HTTP state arrived meanwhile
        assert c.wait_for(lambda e: e.kind == EVENT_CONNECTION and e.values["connected"])
        assert holder["avr"].telnet_failures == 0
    finally:
        link.stop()
