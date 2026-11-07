#!/usr/bin/env python3
"""Move Cynical Denon devices and action steps onto the Denon AVR plugin, in the Indigo database.

Indigo keeps its whole configuration in one XML file (e.g. ~/Documents/Home.indiDb). While
IndigoServer runs it holds everything in memory and rewrites that file every few minutes, so this
script only edits the file with the server stopped.

What it changes (and nothing else; every other byte stays as it was):

- Devices owned by org.cynic.indigo.denon-av: plugin id, device type (avreceiver -> receiver,
  avrzone -> zone), type and plugin names, the device's plugin settings, and its states (reset to
  the new plugin's set; the plugin re-reads the receiver on start). Device ids do not change, so
  triggers, conditions, scripts and other plugins that refer to them keep working.
- Action steps of Cynical Denon: plugin id, action type (set_zone_power -> set_power,
  set_source / set_volume unchanged), settings, and the device, which Cynical kept inside its
  settings and Indigo keeps in the step's own <DeviceID>.
- "Reload plugin" steps that name Cynical Denon.

State names need no rewrite: the new plugin keeps power / source / volume / muted and the list
state `state` with its state.on / state.off / state.unavailable sub-states.

Usage:
    migrate_from_cynical_denon.py DB [--output OUT]       # dry run: write OUT, print the diff
    migrate_from_cynical_denon.py DB --in-place            # IndigoServer stopped; keeps a backup
Run the dry run first and pass the counts it reports as --expect-devices / --expect-steps /
--expect-reloads on the in-place run, so the database cannot change shape in between.
"""

from __future__ import annotations

import argparse
import difflib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

OLD_ID = "org.cynic.indigo.denon-av"
NEW_ID = "com.vtmikel.denon"
NEW_UI_NAME = "Denon AVR"
OLD_UI_NAME = "Cynical Denon"

DEVICE_TYPES = {
    "avreceiver": ("receiver", "Receiver (main zone)"),
    "avrzone": ("zone", "Zone (Zone 2 / Zone 3)"),
}
ACTION_TYPES = {
    "set_zone_power": ("set_power", "Power"),
    "set_source": ("set_source", "Select Input"),
    "set_volume": ("set_volume", "Set Volume"),
}

_PID = re.escape(OLD_ID)


class MigrationError(Exception):
    pass


def _xml(text: str) -> str:
    return escape(str(text), {"'": "&apos;", '"': "&quot;"})


def _unxml(text: str) -> str:
    return (
        text.replace("&apos;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def _child_indent(block: str) -> str:
    """Indentation of the block's direct children (the line after the opening tag)."""
    second = block.split("\n", 2)[1]
    return second[: len(second) - len(second.lstrip("\t"))]


def _value(block: str, key: str) -> str | None:
    m = re.search(rf"<{re.escape(key)} type=\"[a-z]+\"[^>]*>([^<]*)</{re.escape(key)}>", block)
    return _unxml(m.group(1)) if m else None


def _replace_one(block: str, old: str, new: str, what: str) -> str:
    if block.count(old) != 1:
        raise MigrationError(f"{what}: expected exactly one {old!r}, found {block.count(old)}")
    return block.replace(old, new)


def _props_block(indent: str, props: dict) -> str:
    """A <com.vtmikel.denon> settings dict at `indent`, keys sorted like Indigo writes them."""
    lines = [f'{indent}<{NEW_ID} type="dict">']
    for key in sorted(props):
        lines.append(f'{indent}\t<{key} type="string">{_xml(props[key])}</{key}>')
    lines.append(f"{indent}</{NEW_ID}>")
    return "\n".join(lines)


def _replace_props(block: str, new_props: dict, what: str) -> str:
    m = re.search(rf"(\t*)<{_PID} type=\"dict\">.*?</{_PID}>", block, re.S)
    if not m:
        raise MigrationError(f"{what}: no {OLD_ID} settings")
    return block[: m.start()] + _props_block(m.group(1), new_props) + block[m.end() :]


def _old_props(block: str) -> dict:
    m = re.search(rf"<{_PID} type=\"dict\">(.*?)</{_PID}>", block, re.S)
    if not m:
        return {}
    return {
        k: _unxml(v)
        for k, v in re.findall(r"<([A-Za-z0-9_]+) type=\"[a-z]+\"[^>]*>([^<]*)</\1>", m.group(1))
    }


# ---------------------------------------------------------------------------------- devices


def _states_block(indent: str, power: bool, old_states: dict) -> str:
    state = "on" if power else "off"
    values = {
        "muted": ("bool", "true" if old_states.get("muted") in ("true", "yes", "on") else "false"),
        "power": ("bool", "true" if power else "false"),
        "source": (
            "string",
            old_states.get("source", "") if old_states.get("source") != "SLPOFF" else "SOURCE",
        ),
        "state": ("string", state),
        "state.off": ("bool", "true" if state == "off" else "false"),
        "state.on": ("bool", "true" if state == "on" else "false"),
        "state.unavailable": ("bool", "false"),
    }
    if "volume" in old_states:
        values["volume"] = ("real", old_states["volume"])
    lines = [f'{indent}<States type="dict">']
    for key in sorted(values):
        typ, val = values[key]
        lines.append(f'{indent}\t<{key} type="{typ}">{_xml(val)}</{key}>')
    lines.append(f"{indent}</States>")
    return "\n".join(lines)


def migrate_device(block: str) -> tuple[str, int, str]:
    """Returns (new block, device id, device name)."""
    dev_id = int(_value(block, "ID") or 0)
    name = _value(block, "Name") or str(dev_id)
    what = f"device {dev_id} ({name})"
    old_type = _value(block, "TypeIdPlugin")
    if old_type not in DEVICE_TYPES:
        raise MigrationError(f"{what}: unsupported Cynical device type {old_type!r}")
    new_type, type_name = DEVICE_TYPES[old_type]
    if _value(block, "DisplayStateId") != "state":
        raise MigrationError(f"{what}: display state is not 'state'")
    props = _old_props(block)
    if new_type == "receiver":
        if not props.get("address"):
            raise MigrationError(f"{what}: no address")
        new_props = {"address": props["address"]}
    else:
        if not props.get("hostdev") or props.get("subaddress") not in ("2", "3"):
            raise MigrationError(f"{what}: missing hostdev/subaddress")
        new_props = {
            "address": props.get("address", ""),
            "receiver": props["hostdev"],
            "zone": props["subaddress"],
        }

    m = re.search(r"(\t*)<States type=\"dict\">(.*?)\n\t*</States>", block, re.S)
    if not m:
        raise MigrationError(f"{what}: no States")
    old_states = {
        k: v
        for k, v in re.findall(r"<([A-Za-z0-9_.]+) type=\"[a-z]+\"[^>]*>([^<]*)</\1>", m.group(2))
    }
    power = old_states.get("power") in ("true", "on", "yes")

    out = _replace_props(block, new_props, what)
    m = re.search(r"(\t*)<States type=\"dict\">.*?\n\t*</States>", out, re.S)
    out = out[: m.start()] + _states_block(m.group(1), power, old_states) + out[m.end() :]
    out = _replace_one(
        out,
        f'<PluginID type="string">{OLD_ID}</PluginID>',
        f'<PluginID type="string">{NEW_ID}</PluginID>',
        what,
    )
    out = _replace_one(
        out,
        f'<PluginUiName type="string">{OLD_UI_NAME}</PluginUiName>',
        f'<PluginUiName type="string">{NEW_UI_NAME}</PluginUiName>',
        what,
    )
    out = _replace_one(
        out,
        f'<TypeIdPlugin type="string">{old_type}</TypeIdPlugin>',
        f'<TypeIdPlugin type="string">{new_type}</TypeIdPlugin>',
        what,
    )
    out = re.sub(
        r'<TypeName type="string">[^<]*</TypeName>',
        f'<TypeName type="string">{_xml(type_name)}</TypeName>',
        out,
        count=1,
    )
    return out, dev_id, name


# ---------------------------------------------------------------------------------- action steps


def _insert_sorted_child(block: str, line_key: str, line: str) -> str:
    """Insert `line` among the block's direct children, keeping Indigo's sorted key order."""
    indent = _child_indent(block)
    lines = block.split("\n")
    child = re.compile(rf"^{indent}<([A-Za-z0-9_.]+) ")
    for i, text in enumerate(lines[1:], start=1):
        m = child.match(text)
        if m and m.group(1) == line_key:
            raise MigrationError(f"step already has <{line_key}>")
        if m and m.group(1) > line_key:
            lines.insert(i, indent + line)
            return "\n".join(lines)
    lines.insert(len(lines) - 1, indent + line)
    return "\n".join(lines)


def migrate_step(block: str, devices: dict) -> str:
    old_type = _value(block, "TypeIdPlugin")
    props = _old_props(block)
    what = f"action step {old_type} on device {props.get('device')}"
    if old_type not in ACTION_TYPES:
        raise MigrationError(f"{what}: unsupported Cynical action {old_type!r}")
    try:
        dev_id = int(props.get("device", ""))
    except ValueError as err:
        raise MigrationError(f"{what}: no device") from err
    if dev_id not in devices:
        raise MigrationError(f"{what}: device {dev_id} is not a migrated Cynical device")
    name = devices[dev_id]
    value = props.get("value", "")
    new_type, label = ACTION_TYPES[old_type]
    if new_type == "set_power":
        if value.lower() not in ("on", "off", "toggle"):
            raise MigrationError(f"{what}: power value {value!r}")
        new_props = {"value": value.lower(), "description": f"{name}: power {value.lower()}"}
    elif new_type == "set_source":
        code = props.get("value_other", "") if value.lower() == "other" else value
        if not code:
            raise MigrationError(f"{what}: no input code")
        new_props = {"source": code, "source_other": "", "description": f"{name}: input {code}"}
    else:
        try:
            float(value)
        except ValueError as err:
            raise MigrationError(f"{what}: volume {value!r} is not a number") from err
        new_props = {"volume": value, "description": f"{name}: volume {value} dB"}

    out = _replace_props(block, new_props, what)
    out = _replace_one(
        out,
        f'<PluginID type="string">{OLD_ID}</PluginID>',
        f'<PluginID type="string">{NEW_ID}</PluginID>',
        what,
    )
    out = _replace_one(
        out,
        f'<TypeIdPlugin type="string">{old_type}</TypeIdPlugin>',
        f'<TypeIdPlugin type="string">{new_type}</TypeIdPlugin>',
        what,
    )
    out = re.sub(
        r'<TypeLabelPlugin type="string">[^<]*</TypeLabelPlugin>',
        f'<TypeLabelPlugin type="string">{_xml(label)}</TypeLabelPlugin>',
        out,
        count=1,
    )
    return _insert_sorted_child(out, "DeviceID", f'<DeviceID type="integer">{dev_id}</DeviceID>')


# ---------------------------------------------------------------------------------- whole file

_DEVICE_RE = re.compile(r'<Device type="dict">\n.*?\n\t*</Device>', re.S)
_ACTION_RE = re.compile(
    r'(?<=\t)<Action type="dict">\n(?:(?!<Action type="dict">).)*?\n\t*</Action>', re.S
)
_RELOAD_OLD = (
    f'<description type="string">Reload plugin &apos;{OLD_UI_NAME}&apos; ({OLD_ID})</description>',
    f'<pluginId type="string">{OLD_ID}</pluginId>',
)
_RELOAD_NEW = (
    f'<description type="string">Reload plugin &apos;{NEW_UI_NAME}&apos; ({NEW_ID})</description>',
    f'<pluginId type="string">{NEW_ID}</pluginId>',
)


def migrate(text: str) -> tuple[str, dict]:
    devices = {}  # id -> name
    counts = {"devices": 0, "steps": 0, "reloads": 0}

    def on_device(m):
        block = m.group(0)
        if f'<PluginID type="string">{OLD_ID}</PluginID>' not in block:
            return block
        new, dev_id, name = migrate_device(block)
        devices[dev_id] = name
        counts["devices"] += 1
        return new

    text = _DEVICE_RE.sub(on_device, text)

    def on_action(m):
        block = m.group(0)
        if f'<PluginID type="string">{OLD_ID}</PluginID>' in block:
            counts["steps"] += 1
            return migrate_step(block, devices)
        if _RELOAD_OLD[1] in block:
            counts["reloads"] += 1
            for old, new in zip(_RELOAD_OLD, _RELOAD_NEW):
                block = block.replace(old, new)
            return block
        return block

    text = _ACTION_RE.sub(on_action, text)

    leftovers = [m.start() for m in re.finditer(re.escape(OLD_ID), text)]
    if leftovers:
        where = [text[max(0, i - 120) : i + 60].replace("\n", " ") for i in leftovers[:5]]
        raise MigrationError(
            f"{len(leftovers)} reference(s) to {OLD_ID} left:\n  " + "\n  ".join(where)
        )
    if counts["devices"] == 0:
        raise MigrationError("no Cynical Denon devices found (already migrated?)")
    return text, counts


def _server_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-x", "IndigoServer"], capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return out.returncode == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("db", help="Indigo database file (.indiDb)")
    ap.add_argument("--output", help="dry run: write the migrated copy here (default: DB.migrated)")
    ap.add_argument(
        "--in-place", action="store_true", help="rewrite DB itself (server must be stopped)"
    )
    ap.add_argument("--expect-devices", type=int)
    ap.add_argument("--expect-steps", type=int)
    ap.add_argument("--expect-reloads", type=int)
    ap.add_argument("--quiet", action="store_true", help="no diff, summary only")
    args = ap.parse_args(argv)

    if args.in_place and _server_running():
        print(
            "IndigoServer is running: stop it first (/usr/local/indigo/indigo-stop).",
            file=sys.stderr,
        )
        return 2
    with open(args.db, encoding="utf-8", newline="") as fh:
        original = fh.read()
    try:
        ET.fromstring(original.encode("utf-8"))
    except ET.ParseError as err:
        print(f"{args.db} is not well-formed XML before migration: {err}", file=sys.stderr)
        return 2
    try:
        migrated, counts = migrate(original)
        ET.fromstring(migrated.encode("utf-8"))
    except (MigrationError, ET.ParseError) as err:
        print(f"ABORT: {err}", file=sys.stderr)
        return 1

    for key, expected in (
        ("devices", args.expect_devices),
        ("steps", args.expect_steps),
        ("reloads", args.expect_reloads),
    ):
        if expected is not None and counts[key] != expected:
            print(f"ABORT: expected {expected} {key}, found {counts[key]}", file=sys.stderr)
            return 1

    diff = list(
        difflib.unified_diff(
            original.splitlines(), migrated.splitlines(), "before", "after", lineterm="", n=2
        )
    )
    if not args.quiet:
        print("\n".join(diff))
    changed = sum(1 for d in diff if d.startswith(("+", "-")) and not d.startswith(("+++", "---")))
    print(
        f"\n{counts['devices']} devices, {counts['steps']} action steps, {counts['reloads']} reload"
        f" steps migrated; {changed} diff lines; result is well-formed XML; no {OLD_ID} left.",
        file=sys.stderr,
    )

    if args.in_place:
        backup = f"{args.db}.pre-denon-migration-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.db, backup)
        with open(args.db, "w", encoding="utf-8", newline="") as fh:
            fh.write(migrated)
        print(f"Wrote {args.db}; backup {backup}", file=sys.stderr)
    else:
        out = args.output or args.db + ".migrated"
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write(migrated)
        print(f"Dry run: wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
