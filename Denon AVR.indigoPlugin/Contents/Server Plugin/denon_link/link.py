"""ReceiverLink: one receiver's connection, on its own thread and asyncio event loop.

Indigo plugins are synchronous; the denonavr library is asyncio. Each ReceiverLink owns a
daemon thread running an event loop. The plugin talks to it through:

- ``sink(event)``: called from the link thread with plain tuples (see ``Event``). The plugin
  queues them and applies them on its own thread. The sink must not block.
- ``command(zone, raw)``: thread-safe; sends a raw protocol command. Telnet when the
  connection is healthy, otherwise HTTP (``/goform/formiPhoneAppDirect.xml?<cmd>``), so an
  action still works while telnet is down.
- ``refresh()``: thread-safe; re-reads every zone now.

Resync rule (the reason this plugin exists): after every connect and on every reconcile the
link reports the *complete* state of every zone, including off / muted False / 0. The plugin
compares each value against the Indigo device's current state, never against a cached copy,
so a value that changed while nobody was listening is always corrected.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, NamedTuple
from urllib.parse import quote

from . import protocol

EVENT_CONNECTION = "connection"  # value: bool (telnet healthy)
EVENT_ZONE = "zone"  # zone: zone name or protocol.ALL, values: dict of state values
EVENT_INFO = "info"  # values: {"model": ..., "sources": {code: name}}
EVENT_ERROR = "error"  # values: {"message": ...}

SETUP_RETRY_MAX = 300.0  # seconds; HTTP setup retry backoff cap
TELNET_RETRY_START = 5.0  # seconds; first-connect retry backoff start
TELNET_RETRY_MAX = 120.0  # seconds; first-connect retry backoff cap
HTTP_COMMAND_PATH = "/goform/formiPhoneAppDirect.xml?"


class Event(NamedTuple):
    kind: str
    zone: str = ""
    values: dict | None = None


def _default_avr_factory(host: str, zones: list[str]):
    import denonavr  # imported lazily: the plugin bundle installs it on first start

    return denonavr.DenonAVR(host, add_zones={z: None for z in zones} or None)


def _all_events_key():
    from denonavr.const import ALL_TELNET_EVENTS

    return ALL_TELNET_EVENTS


class ReceiverLink:
    def __init__(
        self,
        host: str,
        zones: list[str],
        sink: Callable[[Event], None],
        *,
        reconcile_seconds: float = 300.0,
        logger: logging.Logger | None = None,
        avr_factory=None,
        all_events_key=None,
    ):
        self.host = host
        self.zones = [z for z in zones if z in protocol.ZONE_NAMES]
        self.sink = sink
        self.reconcile_seconds = max(30.0, float(reconcile_seconds))
        self.log = logger or logging.getLogger("Plugin.link")
        self._avr_factory = avr_factory or _default_avr_factory
        self._all_events_key = all_events_key
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._started = threading.Event()
        self._avr = None
        self._was_healthy: bool | None = None
        self._refresh_now: asyncio.Event | None = None
        self._telnet_delay = TELNET_RETRY_START

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"denon-link-{self.host}", daemon=True
        )
        self._thread.start()
        self._started.wait(timeout=5)

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                fut.result(timeout=timeout)
            except Exception as err:  # noqa: BLE001 - shutdown must not raise into Indigo
                self.log.debug(f"{self.host}: shutdown: {err}")
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._refresh_now = asyncio.Event()
        loop.create_task(self._main())
        self._started.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                loop.close()
                self._loop = None

    async def _shutdown(self) -> None:
        avr = self._avr
        if avr is not None:
            try:
                await avr.async_telnet_disconnect()
            except Exception as err:  # noqa: BLE001
                self.log.debug(f"{self.host}: telnet disconnect: {err}")

    # ------------------------------------------------------------------ thread-safe API

    def command(self, zone: str, raw: str, timeout: float = 10.0) -> None:
        """Send a raw protocol command; raises on failure (the plugin logs it)."""
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError(f"{self.host}: not running")
        fut = asyncio.run_coroutine_threadsafe(self._send(raw), loop)
        fut.result(timeout=timeout)

    def refresh(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running() and self._refresh_now is not None:
            loop.call_soon_threadsafe(self._refresh_now.set)

    @property
    def healthy(self) -> bool:
        avr = self._avr
        return bool(avr is not None and getattr(avr, "telnet_healthy", False))

    # ------------------------------------------------------------------ main loop

    async def _main(self) -> None:
        avr = await self._setup()
        if avr is None:
            return
        await self._report_http_state()
        await self._try_telnet()
        loop = asyncio.get_running_loop()
        next_reconcile = loop.time() + self.reconcile_seconds
        next_telnet = loop.time() + self._telnet_delay
        while not self._stopping.is_set():
            # Telnet down: wake up for the next connect attempt too (denonavr only reconnects
            # by itself after a first successful connect).
            wake = next_reconcile
            if not self._telnet_enabled():
                wake = min(wake, next_telnet)
            try:
                await asyncio.wait_for(
                    self._refresh_now.wait(), timeout=max(0.0, wake - loop.time())
                )
            except asyncio.TimeoutError:
                pass
            if self._stopping.is_set():
                break
            requested = self._refresh_now.is_set()
            self._refresh_now.clear()
            try:
                if not self._telnet_enabled() and (requested or loop.time() >= next_telnet):
                    await self._try_telnet()
                    next_telnet = loop.time() + self._telnet_delay
                if requested or loop.time() >= next_reconcile:
                    await self._reconcile()
                    next_reconcile = loop.time() + self.reconcile_seconds
            except Exception as err:  # noqa: BLE001 - the loop must never die
                self.log.warning(f"{self.host}: reconcile failed: {err}")

    async def _setup(self):
        """HTTP setup (model, port, zones, input names), retried with backoff until it works."""
        delay = 5.0
        while not self._stopping.is_set():
            try:
                avr = self._avr_factory(self.host, self.zones)
                await avr.async_setup()
                self._avr = avr
                key = self._all_events_key or _all_events_key()
                avr.register_callback(key, self._on_telnet_event)
                self._emit(
                    Event(
                        EVENT_INFO,
                        protocol.MAIN,
                        {
                            "model": getattr(avr, "model_name", None) or "",
                            "sources": self._source_names(avr),
                        },
                    )
                )
                self.log.info(f"{self.host}: found {getattr(avr, 'model_name', 'receiver')}")
                return avr
            except Exception as err:  # noqa: BLE001
                self._emit(Event(EVENT_ERROR, values={"message": f"setup failed: {err}"}))
                self.log.warning(f"{self.host}: setup failed ({err}); retrying in {int(delay)} s")
                if await self._sleep(delay):
                    return None
                delay = min(SETUP_RETRY_MAX, delay * 2)
        return None

    def _telnet_enabled(self) -> bool:
        """True once a connect succeeded; from then on denonavr keeps reconnecting itself."""
        return bool(getattr(self._avr, "telnet_connected", False))

    async def _try_telnet(self) -> None:
        """One telnet connect attempt; on failure the main loop retries with backoff."""
        try:
            await self._avr.async_telnet_connect()
        except Exception as err:  # noqa: BLE001
            if self._telnet_delay == TELNET_RETRY_START:
                self.log.warning(
                    f"{self.host}: telnet connect failed ({err}); using HTTP until it connects"
                )
            else:
                self.log.debug(f"{self.host}: telnet connect failed again ({err})")
            self._telnet_delay = min(TELNET_RETRY_MAX, self._telnet_delay * 2)
        else:
            self._telnet_delay = TELNET_RETRY_START
        self._note_health()

    async def _reconcile(self) -> None:
        self._note_health()
        if self.healthy:
            # Replies come back through _on_telnet_event and restate every zone.
            await self._avr.async_send_telnet_commands(*protocol.status_queries(self.zones))
        else:
            await self._report_http_state()

    async def _report_http_state(self) -> None:
        """Read every zone over HTTP and report its complete state."""
        avr = self._avr
        try:
            await avr.async_update()
        except Exception as err:  # noqa: BLE001
            self.log.warning(f"{self.host}: HTTP status read failed: {err}")
            if not self.healthy:
                # Neither telnet nor HTTP answers: the receiver is unreachable.
                self._emit(Event(EVENT_ERROR, values={"message": f"status read failed: {err}"}))
            return
        zones = getattr(avr, "zones", {}) or {}
        for name, zone in zones.items():
            if name != protocol.MAIN and name not in self.zones:
                continue
            try:
                if name != protocol.MAIN:
                    await zone.async_update()
            except Exception as err:  # noqa: BLE001
                self.log.warning(f"{self.host}: HTTP status read of {name} failed: {err}")
                continue
            values = {}
            power = getattr(zone, "power", None)
            if power is not None:
                values["power"] = str(power).upper() == "ON"
            volume = getattr(zone, "volume", None)
            if volume is not None:
                values["volume"] = float(volume)
            muted = getattr(zone, "muted", None)
            if muted is not None:
                values["muted"] = bool(muted)
            name_shown = getattr(zone, "input_func", None)
            if name_shown:
                code = self._source_code(zone, name_shown)
                values["source"] = code
                values["source_name"] = name_shown
            self._emit(Event(EVENT_ZONE, name, values))

    async def _send(self, raw: str) -> None:
        avr = self._avr
        if avr is None:
            raise RuntimeError(f"{self.host}: receiver not set up yet")
        if self.healthy:
            await avr.async_send_telnet_commands(raw)
        else:
            await avr.async_get_command(HTTP_COMMAND_PATH + quote(raw, safe=""))
            # No telnet echo will arrive; read the result back over HTTP.
            await asyncio.sleep(1.0)
            await self._report_http_state()

    # ------------------------------------------------------------------ events

    def _on_telnet_event(self, zone: str, event: str, parameter: str) -> None:
        self._note_health()
        change = protocol.state_changes(zone, event, parameter)
        if change is None:
            return
        if change.zone != protocol.ALL and change.zone not in (protocol.MAIN,) + tuple(self.zones):
            return
        values = dict(change.values)
        if "source" in values:
            values["source_name"] = self._source_name_for_code(values["source"])
        self._emit(Event(EVENT_ZONE, change.zone, values))

    def _note_health(self) -> None:
        healthy = self.healthy
        if healthy != self._was_healthy:
            self._was_healthy = healthy
            if healthy:
                self.log.info(f"{self.host}: telnet connected")
            elif healthy is False and self._avr is not None:
                self.log.warning(f"{self.host}: telnet not connected")
            self._emit(Event(EVENT_CONNECTION, values={"connected": healthy}))

    def _emit(self, event: Event) -> None:
        try:
            self.sink(event)
        except Exception as err:  # noqa: BLE001 - a sink bug must not kill the link
            self.log.error(f"{self.host}: event sink failed: {err}")

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless stopping; True when stop was requested."""
        end = asyncio.get_running_loop().time() + seconds
        while not self._stopping.is_set():
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(1.0, remaining))
        return True

    # ------------------------------------------------------------------ input names

    @staticmethod
    def _input_map(zone) -> dict:
        """Display name -> protocol code, from denonavr.

        denonavr keeps this in a private attribute (``input._input_func_map``); it has no public
        accessor. tests/test_denonavr_contract.py pins the shape, so a library update that
        changes it fails CI instead of the house. Without it, names are used as codes.
        """
        try:
            mapping = zone.input._input_func_map
            return dict(mapping) if isinstance(mapping, dict) else {}
        except AttributeError:
            return {}

    def _source_code(self, zone, name: str) -> str:
        return self._input_map(zone).get(name, name)

    def _source_names(self, avr) -> dict:
        """Protocol code -> display name for the main zone (plus SOURCE for zones)."""
        names = {code: name for name, code in self._input_map(avr).items()}
        names.setdefault("SOURCE", "Follow main zone")
        return names

    def _source_name_for_code(self, code: str) -> str:
        if code == "SOURCE":
            return "Follow main zone"
        avr = self._avr
        if avr is None:
            return code
        return self._source_names(avr).get(code, code)
