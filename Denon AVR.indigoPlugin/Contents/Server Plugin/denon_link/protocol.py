"""Denon/Marantz control protocol: events in, commands out. Pure functions, no I/O.

Events arrive from the denonavr library's telnet callback as ``(zone, event, parameter)``,
already split by the library:

    "Z2ON"      -> ("Zone2", "Z2", "ON")
    "Z2SOURCE"  -> ("Zone2", "SI", "SOURCE")
    "Z234"      -> ("Zone2", "MV", "34")
    "Z2SLPOFF"  -> ("Zone2", "SLP", "OFF")        # sleep timer, NOT a source
    "PWSTANDBY" -> ("All", "PW", "STANDBY")

``state_changes`` turns one event into the plugin's state values. Commands are the raw protocol
strings the receiver accepts over telnet (or over HTTP via formiPhoneAppDirect.xml).
"""

from __future__ import annotations

from dataclasses import dataclass

MAIN = "Main"
ALL = "All"
ZONE_NAMES = ("Zone2", "Zone3")

# Absolute volume: protocol value 80 is 0 dB; 0 is -80 dB (the receiver's "---").
_DB_OFFSET = 80.0
MIN_DB = -80.0
MAX_DB = 18.0


def zone_name(zone_number: int | str) -> str:
    """1 -> "Main", 2 -> "Zone2", 3 -> "Zone3"."""
    n = int(zone_number)
    if n == 1:
        return MAIN
    if n in (2, 3):
        return f"Zone{n}"
    raise ValueError(f"unsupported zone number {zone_number!r}")


def zone_prefix(zone: str) -> str:
    """Command prefix for a zone: "Z2"/"Z3"; the main zone has none."""
    if zone == MAIN:
        return ""
    if zone in ZONE_NAMES:
        return "Z" + zone[-1]
    raise ValueError(f"unsupported zone {zone!r}")


def parse_volume(parameter: str) -> float | None:
    """Protocol volume ("405", "40", "00") -> dB (-39.5, -40.0, -80.0). None when not numeric."""
    p = parameter.strip()
    if not p.isdigit():
        return None
    raw = int(p) / 10.0 if len(p) == 3 else float(int(p))
    return raw - _DB_OFFSET


def format_volume(db: float, half_steps: bool) -> str:
    """dB -> protocol value. Half-dB steps ("405") only where the zone supports them."""
    db = max(MIN_DB, min(MAX_DB, float(db)))
    raw = db + _DB_OFFSET
    if half_steps:
        rounded = round(raw * 2) / 2
        if rounded != int(rounded):
            return f"{int(rounded * 10):03d}"
        return f"{int(rounded):02d}"
    return f"{int(round(raw)):02d}"


@dataclass(frozen=True)
class Change:
    """State values one event sets. ``zone`` is MAIN, a ZONE_NAMES entry, or ALL."""

    zone: str
    values: dict


def state_changes(zone: str, event: str, parameter: str) -> Change | None:
    """Map one telnet event to state values; None for events the plugin does not track."""
    param = parameter.strip()
    if event == "PW":
        # Whole-unit power. STANDBY switches every zone off; ON says nothing about the zones,
        # which report their own power (ZM / Z2 / Z3) right after.
        if param == "STANDBY":
            return Change(ALL, {"power": False})
        return None
    if event == "ZM" and zone == MAIN:
        if param in ("ON", "OFF"):
            return Change(MAIN, {"power": param == "ON"})
        return None
    if event in ("Z2", "Z3") and zone in ZONE_NAMES:
        if param in ("ON", "OFF"):
            return Change(zone, {"power": param == "ON"})
        return None
    if event == "SI" and zone in (MAIN,) + ZONE_NAMES:
        if param:
            return Change(zone, {"source": param})
        return None
    if event == "MV" and zone in (MAIN,) + ZONE_NAMES:
        db = parse_volume(param)
        if db is None:
            return None
        return Change(zone, {"volume": db})
    if event == "MU" and zone in (MAIN,) + ZONE_NAMES:
        if param in ("ON", "OFF"):
            return Change(zone, {"muted": param == "ON"})
        return None
    return None


def power_command(zone: str, on: bool) -> str:
    if zone == MAIN:
        return "ZMON" if on else "ZMOFF"
    return zone_prefix(zone) + ("ON" if on else "OFF")


def source_command(zone: str, source_code: str) -> str:
    code = source_code.strip()
    if not code:
        raise ValueError("empty source")
    return ("SI" if zone == MAIN else zone_prefix(zone)) + code


def volume_command(zone: str, db: float) -> str:
    if zone == MAIN:
        return "MV" + format_volume(db, half_steps=True)
    return zone_prefix(zone) + format_volume(db, half_steps=False)


def volume_step_command(zone: str, up: bool) -> str:
    return ("MV" if zone == MAIN else zone_prefix(zone)) + ("UP" if up else "DOWN")


def mute_command(zone: str, muted: bool) -> str:
    return ("MU" if zone == MAIN else zone_prefix(zone) + "MU") + ("ON" if muted else "OFF")


def status_queries(zones: list[str]) -> list[str]:
    """Queries whose replies re-state power, source, volume and mute for every zone."""
    queries = ["ZM?", "SI?", "MV?", "MU?"]
    for zone in zones:
        prefix = zone_prefix(zone)
        queries += [f"{prefix}?", f"{prefix}MU?"]  # "Z2?" answers power, source and volume
    return queries
