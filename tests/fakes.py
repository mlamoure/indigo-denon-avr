"""Test doubles: FakeAVR stands in for denonavr.DenonAVR; FakeLink for ReceiverLink."""

from __future__ import annotations

import types

ALL_EVENTS = "ALL"


class FakeZone:
    def __init__(self, power="OFF", volume=-40.0, muted=False, input_func="SOURCE", input_map=None):
        self.power = power
        self.volume = volume
        self.muted = muted
        self.input_func = input_func
        self.input = types.SimpleNamespace(_input_func_map=dict(input_map or {}))
        self.updates = 0

    async def async_update(self):
        self.updates += 1


class FakeAVR:
    """Implements the subset of denonavr.DenonAVR that ReceiverLink uses."""

    def __init__(self, host, zones, *, setup_failures=0, telnet_failures=0):
        self.host = host
        self.model_name = "Denon AVR-X3700H"
        names = {"Living Room Apple TV": "SAT/CBL", "Sonos": "CD", "Living Room Roku": "MPLAY"}
        self.input = types.SimpleNamespace(_input_func_map=dict(names))
        main = FakeZone(
            power="ON", volume=-41.5, input_func="Living Room Apple TV", input_map=names
        )
        self.zones = {"Main": main}
        for z in zones:
            self.zones[z] = FakeZone(input_map={"SOURCE": "SOURCE", **names})
        self.setup_failures = setup_failures
        self.telnet_failures = telnet_failures
        self.callbacks = []
        self.telnet_sent = []
        self.http_sent = []
        self.telnet_healthy = False
        self.telnet_connected = False
        self.updates = 0
        self.disconnected = False

    async def async_setup(self):
        if self.setup_failures:
            self.setup_failures -= 1
            raise OSError("receiver unreachable")

    async def async_update(self):
        self.updates += 1

    def register_callback(self, event, callback):
        self.callbacks.append((event, callback))

    async def async_telnet_connect(self):
        if self.telnet_failures:
            self.telnet_failures -= 1
            raise ConnectionRefusedError("busy")
        self.telnet_connected = True
        self.telnet_healthy = True

    async def async_telnet_disconnect(self):
        self.disconnected = True
        self.telnet_connected = False
        self.telnet_healthy = False

    async def async_send_telnet_commands(self, *commands):
        self.telnet_sent.extend(commands)

    async def async_get_command(self, request):
        self.http_sent.append(request)
        return ""

    # test helper: deliver a telnet event the way denonavr does
    def push(self, zone, event, parameter):
        for _key, callback in self.callbacks:
            callback(zone, event, parameter)


class FakeLink:
    """Stands in for ReceiverLink inside plugin tests."""

    instances = []

    def __init__(self, host, zones, sink, *, reconcile_seconds=300, logger=None):
        self.host = host
        self.zones = zones
        self.sink = sink
        self.reconcile_seconds = reconcile_seconds
        self.started = False
        self.stopped = False
        self.commands = []
        self.refreshed = 0
        FakeLink.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout=10.0):
        self.stopped = True

    def command(self, zone, raw, timeout=10.0):
        self.commands.append((zone, raw))

    def refresh(self):
        self.refreshed += 1
