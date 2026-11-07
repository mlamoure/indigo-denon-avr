"""Denon AVR Indigo plugin: thin adapter between Indigo and denon_link.

Receiver communication lives in ``denon_link/`` (Indigo-free). This module owns device
lifecycle, one ReceiverLink per receiver device, applying link events to device states, and
the actions.

Threads: each ReceiverLink runs its own thread. Its events land in ``self.events`` and are
applied on runConcurrentThread, which is also where links are started, restarted and stopped
(so device start order never matters).
"""

try:
    import indigo
except ImportError:  # unit tests inject a stub
    pass

import logging
import queue

from denon_link import protocol
from denon_link.link import (
    EVENT_CONNECTION,
    EVENT_ERROR,
    EVENT_INFO,
    EVENT_ZONE,
    ReceiverLink,
)

RECEIVER = "receiver"
ZONE = "zone"
# Display state `state` is an Indigo list state, so Indigo also provides state.on, state.off and
# state.unavailable as true/false sub-states (triggers, conditions and other plugins use them).
STATE_ON, STATE_OFF, STATE_UNAVAILABLE = "on", "off", "unavailable"
DEFAULT_RECONCILE_MINUTES = 5
STATE_KEYS = ("power", "source", "source_name", "volume", "muted")

# Tests replace this with a fake link class; None means the real ReceiverLink.
LINK_FACTORY = None


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self._apply_log_level(pluginPrefs.get("showDebugInfo", False))
        self.events = queue.Queue()
        self.links = {}  # receiver device id -> ReceiverLink
        self.link_zones = {}  # receiver device id -> tuple of zone names the link serves
        self.sources = {}  # receiver device id -> {code: display name}
        self.started = set()  # device ids between deviceStartComm and deviceStopComm
        self.unavailable = set()  # receiver ids that cannot be reached at all

    # ------------------------------------------------------------------ prefs

    def _apply_log_level(self, debug_enabled):
        self.debug = bool(debug_enabled)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.plugin_file_handler.setLevel(logging.DEBUG)

    def _reconcile_seconds(self):
        try:
            minutes = float(self.pluginPrefs.get("reconcileMinutes", DEFAULT_RECONCILE_MINUTES))
        except (TypeError, ValueError):
            minutes = DEFAULT_RECONCILE_MINUTES
        return max(1.0, minutes) * 60.0

    def validatePrefsConfigUi(self, valuesDict):
        errors = indigo.Dict()
        try:
            minutes = float(valuesDict.get("reconcileMinutes", DEFAULT_RECONCILE_MINUTES))
            if not 1 <= minutes <= 60:
                raise ValueError
        except (TypeError, ValueError):
            errors["reconcileMinutes"] = "Enter 1 to 60 minutes"
        if errors:
            return (False, valuesDict, errors)
        return True

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self._apply_log_level(valuesDict.get("showDebugInfo", False))
        for rid in list(self.links):
            self._stop_link(rid)  # restarted with the new interval by the next sync

    # ------------------------------------------------------------------ device config

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errors = indigo.Dict()
        if typeId == RECEIVER:
            if not str(valuesDict.get("address", "")).strip():
                errors["address"] = "Enter the receiver's IP address or host name"
        elif typeId == ZONE:
            try:
                receiver_id = int(valuesDict.get("receiver", 0))
            except (TypeError, ValueError):
                receiver_id = 0
            receiver = indigo.devices.get(receiver_id) if receiver_id else None
            if receiver is None or receiver.deviceTypeId != RECEIVER:
                errors["receiver"] = "Pick the receiver this zone belongs to"
            if str(valuesDict.get("zone", "")) not in ("2", "3"):
                errors["zone"] = "Pick Zone 2 or Zone 3"
            if not errors:
                valuesDict["address"] = f"{receiver.name} Zone {valuesDict['zone']}"
        if errors:
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def receiverMenu(self, filter="", valuesDict=None, typeId="", targetId=0):
        return [
            (str(dev.id), dev.name)
            for dev in indigo.devices.iter(f"self.{RECEIVER}")
            if dev.id != targetId
        ]

    def sourceMenu(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Inputs of the action's device: receiver names first, then 'other'."""
        items = []
        dev = indigo.devices.get(int(targetId)) if targetId else None
        rid = self._receiver_id(dev) if dev is not None else None
        names = self.sources.get(rid, {}) if rid else {}
        for code, name in sorted(names.items(), key=lambda kv: kv[1].lower()):
            if code == "SOURCE" and dev is not None and dev.deviceTypeId == RECEIVER:
                continue  # only secondary zones can follow the main zone
            items.append((code, f"{name} ({code})" if name != code else code))
        items.append(("other", "Other (enter the input code)"))
        return items

    # ------------------------------------------------------------------ lifecycle

    def deviceStartComm(self, dev):
        dev.stateListOrDisplayStateIdChanged()
        self.started.add(dev.id)

    def deviceStopComm(self, dev):
        self.started.discard(dev.id)
        if dev.deviceTypeId == RECEIVER:
            self._stop_link(dev.id)

    def shutdown(self):
        for rid in list(self.links):
            self._stop_link(rid)

    def runConcurrentThread(self):
        try:
            while True:
                self._sync_links()
                self._drain_events()
                self.sleep(0.25)
        except self.StopThread:
            pass

    # ------------------------------------------------------------------ links

    def _zone_devices(self, receiver_id):
        for dev in indigo.devices.iter(f"self.{ZONE}"):
            if dev.id in self.started and self._int(dev.pluginProps.get("receiver")) == receiver_id:
                yield dev

    def _desired_zones(self, receiver_id):
        zones = set()
        for dev in self._zone_devices(receiver_id):
            try:
                zones.add(protocol.zone_name(dev.pluginProps.get("zone")))
            except (TypeError, ValueError):
                continue
        return tuple(sorted(zones))

    def _sync_links(self):
        """Start, restart or stop links so each started receiver has one for its zones."""
        for dev in indigo.devices.iter(f"self.{RECEIVER}"):
            if dev.id not in self.started:
                continue
            zones = self._desired_zones(dev.id)
            if dev.id in self.links and self.link_zones.get(dev.id) == zones:
                continue
            if dev.id in self.links:
                self.logger.debug(f"{dev.name}: zones changed to {zones}; reconnecting")
                self._stop_link(dev.id)
            self._start_link(dev, zones)
        for rid in list(self.links):
            if rid not in self.started:
                self._stop_link(rid)

    def _start_link(self, dev, zones):
        host = str(dev.pluginProps.get("address", "")).strip()
        if not host:
            dev.setErrorStateOnServer("no address")
            return
        factory = LINK_FACTORY or ReceiverLink
        rid = dev.id
        link = factory(
            host,
            list(zones),
            lambda event, rid=rid: self.events.put((rid, event)),
            reconcile_seconds=self._reconcile_seconds(),
            logger=self.logger,
        )
        self.links[rid] = link
        self.link_zones[rid] = zones
        link.start()

    def _stop_link(self, receiver_id):
        link = self.links.pop(receiver_id, None)
        self.link_zones.pop(receiver_id, None)
        if link is not None:
            link.stop()

    # ------------------------------------------------------------------ events -> states

    def _drain_events(self, limit=500):
        for _ in range(limit):
            try:
                receiver_id, event = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                self._apply_event(receiver_id, event)
            except Exception as err:  # noqa: BLE001 - one bad event must not stop the loop
                self.logger.error(f"Could not apply {event!r}: {err}")

    def _apply_event(self, receiver_id, event):
        receiver = indigo.devices.get(receiver_id)
        if receiver is None or receiver_id not in self.started:
            return
        values = event.values or {}
        if event.kind == EVENT_CONNECTION:
            self._update(receiver, {"connected": bool(values.get("connected"))})
        elif event.kind == EVENT_INFO:
            self.sources[receiver_id] = dict(values.get("sources") or {})
            self.unavailable.discard(receiver_id)
            self._update(receiver, {"model": str(values.get("model") or "")})
            for dev in [receiver, *self._zone_devices(receiver_id)]:
                dev.setErrorStateOnServer(None)
                self._update(dev, {})
        elif event.kind == EVENT_ERROR:
            # The receiver cannot be reached at all (telnet down alone is not an error).
            self.unavailable.add(receiver_id)
            for dev in [receiver, *self._zone_devices(receiver_id)]:
                self._update(dev, {})
                dev.setErrorStateOnServer("no connection")
        elif event.kind == EVENT_ZONE:
            if receiver_id in self.unavailable:
                self.unavailable.discard(receiver_id)
                for dev in [receiver, *self._zone_devices(receiver_id)]:
                    dev.setErrorStateOnServer(None)
            for dev in self._devices_for_zone(receiver, event.zone):
                self._update(dev, values)

    def _devices_for_zone(self, receiver, zone):
        if zone == protocol.ALL:
            return [receiver, *self._zone_devices(receiver.id)]
        if zone == protocol.MAIN:
            return [receiver]
        return [dev for dev in self._zone_devices(receiver.id) if self._zone_of(dev) == zone]

    def _update(self, dev, values):
        """Write the values that differ from the device's current states (never a cache)."""
        changes = []
        for key, value in values.items():
            if key == "volume" and value is not None:
                value = round(float(value), 1)
            if dev.states.get(key) != value:
                entry = {"key": key, "value": value}
                if key == "volume":
                    entry["uiValue"] = f"{value:.1f} dB"
                    entry["decimalPlaces"] = 1
                changes.append(entry)
        power = values.get("power", dev.states.get("power"))
        if self._receiver_id(dev) in self.unavailable:
            state = STATE_UNAVAILABLE
        else:
            state = STATE_ON if power else STATE_OFF
        if dev.states.get("state") != state:
            changes.append({"key": "state", "value": state})
        if not changes:
            return
        dev.updateStatesOnServer(changes)
        if any(c["key"] == "state" for c in changes):
            dev.updateStateImageOnServer(
                indigo.kStateImageSel.PowerOn
                if state == STATE_ON
                else indigo.kStateImageSel.PowerOff
            )

    # ------------------------------------------------------------------ actions

    def validateActionConfigUi(self, valuesDict, typeId, devId):
        """Fill the description Indigo shows for the step in action lists."""
        dev = indigo.devices.get(int(devId)) if devId else None
        name = dev.name if dev is not None else "receiver"
        if typeId == "set_power":
            text = f"{name}: power {valuesDict.get('value', 'on')}"
        elif typeId == "set_source":
            code = valuesDict.get("source", "")
            if code == "other":
                code = str(valuesDict.get("source_other", "")).strip()
                if not code:
                    errors = indigo.Dict()
                    errors["source_other"] = "Enter the input code"
                    return (False, valuesDict, errors)
            text = f"{name}: input {code}"
        elif typeId == "set_volume":
            text = f"{name}: volume {valuesDict.get('volume', '')} dB"
        elif typeId == "adjust_volume":
            text = f"{name}: volume {valuesDict.get('direction', 'up')}"
        elif typeId == "set_mute":
            text = f"{name}: mute {valuesDict.get('value', 'on')}"
        elif typeId == "send_command":
            text = f"{name}: send {valuesDict.get('command', '')}"
        else:
            text = f"{name}: refresh"
        valuesDict["description"] = text
        return (True, valuesDict)

    def set_power(self, action, dev):
        value = str(action.props.get("value", "on")).lower()
        if value == "toggle":
            on = not bool(dev.states.get("power"))
        else:
            on = value == "on"
        self._send(dev, protocol.power_command(self._zone_of(dev), on))

    def set_source(self, action, dev):
        code = str(action.props.get("source", "")).strip()
        if code == "other":
            code = str(action.props.get("source_other", "")).strip()
        if not code:
            self.logger.error(f'"{dev.name}": set source has no input selected')
            return
        self._send(dev, protocol.source_command(self._zone_of(dev), code))

    def set_volume(self, action, dev):
        raw = self.substitute(str(action.props.get("volume", "")))
        try:
            db = float(raw)
        except (TypeError, ValueError):
            self.logger.error(f'"{dev.name}": volume "{raw}" is not a number (dB, e.g. -40)')
            return
        self._send(dev, protocol.volume_command(self._zone_of(dev), db))

    def adjust_volume(self, action, dev):
        up = str(action.props.get("direction", "up")).lower() == "up"
        self._send(dev, protocol.volume_step_command(self._zone_of(dev), up))

    def set_mute(self, action, dev):
        value = str(action.props.get("value", "on")).lower()
        muted = (not bool(dev.states.get("muted"))) if value == "toggle" else value == "on"
        self._send(dev, protocol.mute_command(self._zone_of(dev), muted))

    def send_command(self, action, dev):
        raw = self.substitute(str(action.props.get("command", ""))).strip()
        if not raw:
            self.logger.error(f'"{dev.name}": send command is empty')
            return
        self._send(dev, raw)

    def refresh(self, action, dev):
        link = self.links.get(self._receiver_id(dev))
        if link is not None:
            link.refresh()

    def _send(self, dev, raw):
        rid = self._receiver_id(dev)
        link = self.links.get(rid)
        if link is None:
            self.logger.error(f'"{dev.name}": receiver is not connected; "{raw}" not sent')
            return
        try:
            link.command(self._zone_of(dev), raw)
            self.logger.debug(f'"{dev.name}": sent {raw}')
        except Exception as err:  # noqa: BLE001
            self.logger.error(f'"{dev.name}": "{raw}" failed: {err}')

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _receiver_id(self, dev):
        if dev.deviceTypeId == RECEIVER:
            return dev.id
        return self._int(dev.pluginProps.get("receiver"))

    @staticmethod
    def _zone_of(dev):
        if dev.deviceTypeId == RECEIVER:
            return protocol.MAIN
        return protocol.zone_name(dev.pluginProps.get("zone", 2))
