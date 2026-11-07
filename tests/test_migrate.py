"""tools/migrate_from_cynical_denon.py against a small synthetic database with the live shapes."""

import importlib.util
import os
import xml.etree.ElementTree as ET

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "migrate_from_cynical_denon.py",
)
_spec = importlib.util.spec_from_file_location("migrate_tool", _PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

CYNIC = "org.cynic.indigo.denon-av"


def device(dev_id, name, type_id, props, states, plugin=CYNIC, ui="Cynical Denon"):
    props_xml = "\n".join(f"\t\t\t\t\t{p}" for p in props)
    states_xml = "\n".join(f"\t\t\t\t{s}" for s in states)
    return f"""\t\t<Device type="dict">
\t\t\t<Class type="integer">0</Class>
\t\t\t<DisplayStateId type="string">state</DisplayStateId>
\t\t\t<ID type="integer">{dev_id}</ID>
\t\t\t<MetaProps type="dict">
\t\t\t\t<{plugin} type="dict">
{props_xml}
\t\t\t\t</{plugin}>
\t\t\t</MetaProps>
\t\t\t<Name type="string">{name}</Name>
\t\t\t<PluginID type="string">{plugin}</PluginID>
\t\t\t<PluginUiName type="string">{ui}</PluginUiName>
\t\t\t<States type="dict">
{states_xml}
\t\t\t</States>
\t\t\t<TypeIdPlugin type="string">{type_id}</TypeIdPlugin>
\t\t\t<TypeName type="string">AV thing</TypeName>
\t\t</Device>"""


def step(type_id, props, delay=False, plugin=CYNIC, device_id=None):
    delay_xml = (
        '\t\t\t\t\t<DelayAction type="bool">true</DelayAction>\n'
        '\t\t\t\t\t<DelayAmount type="integer">3</DelayAmount>\n'
        if delay
        else ""
    )
    dev_xml = f'\t\t\t\t\t<DeviceID type="integer">{device_id}</DeviceID>\n' if device_id else ""
    props_xml = "\n".join(f"\t\t\t\t\t\t\t{p}" for p in props)
    return f"""\t\t\t\t<Action type="dict">
\t\t\t\t\t<Class type="integer">999</Class>
{delay_xml}{dev_xml}\t\t\t\t\t<MetaProps type="dict">
\t\t\t\t\t\t<{plugin} type="dict">
{props_xml}
\t\t\t\t\t\t</{plugin}>
\t\t\t\t\t</MetaProps>
\t\t\t\t\t<ObjVers type="integer">14</ObjVers>
\t\t\t\t\t<PluginID type="string">{plugin}</PluginID>
\t\t\t\t\t<TypeIdPlugin type="string">{type_id}</TypeIdPlugin>
\t\t\t\t\t<TypeLabelPlugin type="string">Old label</TypeLabelPlugin>
\t\t\t\t</Action>"""


RELOAD = """\t\t\t\t<Action type="dict">
\t\t\t\t\t<Class type="integer">999</Class>
\t\t\t\t\t<MetaProps type="dict">
\t\t\t\t\t\t<com.perceptiveautomation.indigoplugin.ActionCollection type="dict">
\t\t\t\t\t\t\t<description type="string">Reload plugin &apos;Cynical Denon&apos; (org.cynic.indigo.denon-av)</description>
\t\t\t\t\t\t\t<pluginId type="string">org.cynic.indigo.denon-av</pluginId>
\t\t\t\t\t\t</com.perceptiveautomation.indigoplugin.ActionCollection>
\t\t\t\t\t</MetaProps>
\t\t\t\t\t<PluginID type="string">com.perceptiveautomation.indigoplugin.ActionCollection</PluginID>
\t\t\t\t\t<TypeIdPlugin type="string">reloadPlugin</TypeIdPlugin>
\t\t\t\t</Action>"""

RECEIVER = device(
    100,
    "Denon AVR - TV Area (Zone 1)",
    "avreceiver",
    [
        '<address type="string">192.0.2.52</address>',
        '<keepalive type="bool">true</keepalive>',
    ],
    [
        '<muted type="bool" subtype="yesNo">no</muted>',
        '<power type="bool" subtype="onOff">on</power>',
        '<source type="string">SAT/CBL</source>',
        '<state type="string">on</state>',
        '<state.on type="bool">true</state.on>',
        '<volume type="real">-41.5</volume>',
    ],
)
ZONE = device(
    200,
    "Bar &amp; Lounge (Zone 2)",
    "avrzone",
    [
        '<address type="string">Denon Zone 2</address>',
        '<hostdev type="integer">100</hostdev>',
        '<subaddress type="string">2</subaddress>',
    ],
    [
        '<power type="bool">false</power>',
        '<source type="string">SLPOFF</source>',
        '<state type="string">ready</state>',
        '<volume type="real">-46</volume>',
    ],
)
OTHER_DEVICE = device(
    300,
    "Sonos",
    "ZonePlayer",
    ['<address type="string">192.0.2.68</address>'],
    ['<ZP_STATE type="string">STOPPED</ZP_STATE>'],
    plugin="com.ssi.indigoplugin.Sonos",
    ui="Sonos",
)
OTHER_STEP = step(
    "actionPlay",
    ['<mode type="string">Play</mode>'],
    plugin="com.ssi.indigoplugin.Sonos",
    device_id=300,
)
CONDITION = """\t\t\t\t<Condition type="dict">
\t\t\t\t\t<DevID type="integer">200</DevID>
\t\t\t\t\t<DevState type="string">power</DevState>
\t\t\t\t</Condition>"""


def database(*action_steps, devices=(RECEIVER, ZONE, OTHER_DEVICE)):
    steps = "\n".join(action_steps)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Database type="dict">
\t<DeviceList type="vector">
{chr(10).join(devices)}
\t</DeviceList>
\t<ActionGroupList type="vector">
\t\t<ActionGroup type="dict">
\t\t\t<ActionSteps type="vector">
{steps}
\t\t\t</ActionSteps>
{CONDITION}
\t\t</ActionGroup>
\t</ActionGroupList>
</Database>
"""


STANDARD_STEPS = (
    step(
        "set_zone_power", ['<device type="string">200</device>', '<value type="string">on</value>']
    ),
    step(
        "set_source",
        [
            '<device type="string">200</device>',
            '<value type="string">SOURCE</value>',
            '<value_other type="string">MPLAY</value_other>',
        ],
        delay=True,
    ),
    step(
        "set_volume",
        ['<device type="string">100</device>', '<value type="string">-6</value>'],
        delay=True,
    ),
    RELOAD,
    OTHER_STEP,
)


def test_migrates_devices_steps_and_reloads():
    before = database(*STANDARD_STEPS)
    after, counts = mig.migrate(before)
    assert counts == {"devices": 2, "steps": 3, "reloads": 1}
    assert CYNIC not in after
    ET.fromstring(after.encode())

    root = ET.fromstring(after.encode())
    devs = {int(d.findtext("ID")): d for d in root.iter("Device")}
    receiver, zone = devs[100], devs[200]
    assert receiver.findtext("PluginID") == "com.vtmikel.denon"
    assert receiver.findtext("TypeIdPlugin") == "receiver"
    assert receiver.findtext("PluginUiName") == "Denon AVR"
    assert receiver.find("MetaProps/com.vtmikel.denon/address").text == "192.0.2.52"
    assert receiver.find("MetaProps/com.vtmikel.denon/keepalive") is None
    assert zone.findtext("TypeIdPlugin") == "zone"
    props = zone.find("MetaProps/com.vtmikel.denon")
    assert (props.findtext("receiver"), props.findtext("zone")) == ("100", "2")
    states = zone.find("States")
    assert states.findtext("source") == "SOURCE"  # Cynical's misread sleep reply corrected
    assert states.findtext("state") == "off"
    assert states.findtext("state.off") == "true"
    assert states.findtext("volume") == "-46"
    assert receiver.find("States").findtext("power") == "true"

    # The unrelated plugin's device and step are byte-for-byte unchanged.
    assert OTHER_DEVICE in after
    assert OTHER_STEP in after
    assert CONDITION in after


def test_steps_get_device_id_in_sorted_position_and_new_props():
    after, _ = mig.migrate(database(*STANDARD_STEPS))
    root = ET.fromstring(after.encode())
    steps = [a for a in root.iter("Action") if a.findtext("PluginID") == "com.vtmikel.denon"]
    power, source, volume = steps
    assert [c.tag for c in power] == [
        "Class",
        "DeviceID",
        "MetaProps",
        "ObjVers",
        "PluginID",
        "TypeIdPlugin",
        "TypeLabelPlugin",
    ]
    assert [c.tag for c in source][:4] == ["Class", "DelayAction", "DelayAmount", "DeviceID"]
    assert power.findtext("DeviceID") == "200"
    assert power.findtext("TypeIdPlugin") == "set_power"
    p = power.find("MetaProps/com.vtmikel.denon")
    assert p.findtext("value") == "on"
    assert p.findtext("description") == "Bar & Lounge (Zone 2): power on"
    s = source.find("MetaProps/com.vtmikel.denon")
    assert s.findtext("source") == "SOURCE"  # value_other ignored unless value is "other"
    assert volume.findtext("DeviceID") == "100"
    assert volume.find("MetaProps/com.vtmikel.denon").findtext("volume") == "-6"
    assert "Bar &amp; Lounge (Zone 2): power on" in after  # names are XML-escaped


def test_reload_step_names_the_new_plugin():
    after, _ = mig.migrate(database(*STANDARD_STEPS))
    assert "Reload plugin &apos;Denon AVR&apos; (com.vtmikel.denon)" in after
    assert '<pluginId type="string">com.vtmikel.denon</pluginId>' in after


def test_second_run_finds_nothing():
    after, _ = mig.migrate(database(*STANDARD_STEPS))
    with pytest.raises(mig.MigrationError, match="already migrated"):
        mig.migrate(after)


def test_unknown_action_aborts():
    bad = step(
        "set_tuner", ['<device type="string">100</device>', '<value type="string">FM</value>']
    )
    with pytest.raises(mig.MigrationError, match="unsupported Cynical action"):
        mig.migrate(database(bad))


def test_step_for_unknown_device_aborts():
    bad = step(
        "set_zone_power", ['<device type="string">999</device>', '<value type="string">on</value>']
    )
    with pytest.raises(mig.MigrationError, match="not a migrated Cynical device"):
        mig.migrate(database(bad))


def test_leftover_reference_aborts():
    leftover = '\t\t\t\t<Trigger type="dict"><PluginID type="string">org.cynic.indigo.denon-av</PluginID></Trigger>'
    with pytest.raises(mig.MigrationError, match="left"):
        mig.migrate(database(*STANDARD_STEPS, leftover))


def test_cli_dry_run_and_expected_counts(tmp_path, capsys):
    db = tmp_path / "Test.indiDb"
    db.write_text(database(*STANDARD_STEPS))
    assert mig.main([str(db), "--quiet", "--expect-devices", "2", "--expect-steps", "3"]) == 0
    assert (tmp_path / "Test.indiDb.migrated").exists()
    assert db.read_text() == database(*STANDARD_STEPS)  # dry run leaves the source alone
    assert mig.main([str(db), "--quiet", "--expect-steps", "14"]) == 1


def test_cli_in_place_refuses_while_server_runs(tmp_path, monkeypatch):
    db = tmp_path / "Test.indiDb"
    db.write_text(database(*STANDARD_STEPS))
    monkeypatch.setattr(mig, "_server_running", lambda: True)
    assert mig.main([str(db), "--in-place", "--quiet"]) == 2
    assert db.read_text() == database(*STANDARD_STEPS)


def test_cli_in_place_keeps_a_backup(tmp_path, monkeypatch):
    db = tmp_path / "Test.indiDb"
    original = database(*STANDARD_STEPS)
    db.write_text(original)
    monkeypatch.setattr(mig, "_server_running", lambda: False)
    assert mig.main([str(db), "--in-place", "--quiet"]) == 0
    backups = list(tmp_path.glob("Test.indiDb.pre-denon-migration-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert CYNIC not in db.read_text()
