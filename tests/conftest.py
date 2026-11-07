"""Pytest setup: stub the `indigo` module and put the plugin folder on sys.path.

The plugin runs inside Indigo's embedded Python where `indigo` is injected by the host. The
stub covers only the surface plugin.py touches (same approach as the Sense plugin repo).
"""

from __future__ import annotations

import logging
import os
import sys
import types

import pytest

PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Denon AVR.indigoPlugin",
    "Contents",
    "Server Plugin",
)
sys.path.insert(0, PLUGIN_DIR)

indigo_stub = types.SimpleNamespace()


class Devices(dict):
    def __iter__(self):
        return iter(self.values())

    def iter(self, filter=""):
        type_id = filter.split(".", 1)[1] if filter.startswith("self.") else None
        return iter([d for d in self.values() if type_id is None or d.deviceTypeId == type_id])


class Device:
    _next_id = 1000

    def __init__(self, name, deviceTypeId, props=None, states=None, dev_id=None):
        if dev_id is None:
            Device._next_id += 1
            dev_id = Device._next_id
        self.id = dev_id
        self.name = name
        self.deviceTypeId = deviceTypeId
        self.pluginProps = dict(props or {})
        self.states = dict(states or {})
        self.state_updates = []
        self.images = []
        self.errorState = None
        self.state_list_changed = 0

    def updateStatesOnServer(self, changes):
        self.state_updates.append(list(changes))
        for change in changes:
            self.states[change["key"]] = change["value"]

    def updateStateImageOnServer(self, image):
        self.images.append(image)

    def setErrorStateOnServer(self, message):
        self.errorState = message

    def stateListOrDisplayStateIdChanged(self):
        self.state_list_changed += 1


class IndigoDict(dict):
    pass


class _StopThread(Exception):
    pass


class _Handler(logging.Handler):
    def emit(self, record):
        pass


class PluginBase:
    StopThread = _StopThread

    def __init__(self, plugin_id, display_name, version, prefs):
        self.pluginId = plugin_id
        self.pluginDisplayName = display_name
        self.pluginVersion = version
        self.pluginPrefs = prefs
        self.logger = logging.getLogger("Plugin")
        self.indigo_log_handler = _Handler()
        self.plugin_file_handler = _Handler()

    def substitute(self, text, validateOnly=False):
        return text

    def sleep(self, seconds):
        raise _StopThread()


indigo_stub.PluginBase = PluginBase
indigo_stub.Dict = IndigoDict
indigo_stub.devices = Devices()
indigo_stub.kStateImageSel = types.SimpleNamespace(PowerOn="PowerOn", PowerOff="PowerOff")
sys.modules["indigo"] = indigo_stub


@pytest.fixture
def indigo():
    indigo_stub.devices.clear()
    return indigo_stub


@pytest.fixture
def make_device(indigo):
    def _make(name, type_id, props=None, states=None):
        dev = Device(name, type_id, props, states)
        indigo.devices[dev.id] = dev
        return dev

    return _make
