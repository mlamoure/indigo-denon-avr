# CLAUDE.md — Denon AVR plugin

Rules for AI agents working in this repo. End-user docs live in README.md. This repo is public on
GitHub: keep household details (hostnames, IPs, device names, people) out of every file.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r "Denon AVR.indigoPlugin/Contents/Server Plugin/requirements.txt"
.venv/bin/black . && .venv/bin/ruff check --select E9,F . && .venv/bin/python -m pytest   # before every commit
# after copying the bundle to the Indigo server's Plugins folder:
/usr/local/bin/indigo-restart-plugin com.vtmikel.denon      # on the Indigo server
```

Verify deploys in `/Library/Application Support/Perceptive Automation/Indigo <version>/Logs/com.vtmikel.denon/plugin.log`.
Ship the bundle without `Contents/Packages`; Indigo installs `requirements.txt` into it on the
next plugin start. Never commit `Packages/` (a stale `pip-install-log-success.txt` stops Indigo
installing).

## Architecture

```
Denon AVR.indigoPlugin/Contents/Server Plugin/
  plugin.py          # Indigo adapter: one ReceiverLink per receiver device (started/restarted in
                     # runConcurrentThread by _sync_links), events -> states, actions
  Devices.xml        # receiver, zone; list state `state` (on/off/unavailable) -> state.on etc.
  Actions.xml        # set_power, set_source, set_volume, adjust_volume, set_mute, refresh, send_command
  requirements.txt   # exact pins (denonavr + its packages); Indigo installs them
  denon_link/        # Indigo-free (never `import indigo`)
    protocol.py      #   telnet event -> state values; commands; volume dB <-> protocol
    link.py          #   ReceiverLink: thread + asyncio loop around denonavr.DenonAVR
tools/migrate_from_cynical_denon.py   # one-time Indigo database migration (server stopped)
tools/make_icon.py   # draws Contents/Resources/icon.png and ./icon.png (Pillow)
tests/               # conftest stubs `indigo`; fakes.FakeAVR / FakeLink; contract tests use real denonavr
```

## Invariants

- **Never rename** the plugin id `com.vtmikel.denon`, the device types `receiver`/`zone`, any
  state id, any action id or action prop key. The Indigo database stores them for every device and
  action step; triggers, conditions, other plugins (Group Change Listener groups, Timed Devices)
  and scripts calling `executeAction` refer to them.
- `state` stays a **list** state with values on/off/unavailable: Indigo derives `state.on`,
  `state.off`, `state.unavailable` from it and existing automations watch those. `unavailable`
  means unreachable (setup or HTTP read failed with telnet down), never "telnet down" alone.
- **Resync rule:** state values are compared with the Indigo device's current states, never with
  a cache, and the link reports complete zone state (including False/off) after every connect and
  on every reconcile. This is the bug the plugin exists to fix; tests cover it.
- `source` holds the receiver's raw input code (what automations compare against); the renamed
  display name goes in `source_name`. The name -> code map comes from denonavr's private
  `input._input_func_map`; `tests/test_denonavr_contract.py` pins it.
- The receiver allows one telnet client. The link connects once; denonavr then reconnects by
  itself. Until the first connect succeeds the link retries with backoff and keeps working over
  HTTP. Never add a blocking wait to the link's loop.
- Runtime dependencies are **exact pins** (Indigo installs once per plugin version and never
  updates). Renovate opens the bump PRs; CI's contract tests decide. Bump `denonavr` in the bundle
  `requirements.txt` and the dev `requirements.txt` together. CI uses Python 3.13 because Indigo
  2026.1 does.
- The migration tool edits the database only with IndigoServer stopped, keeps a backup, edits
  exact anchored text (no XML re-serialisation), and aborts on unexpected shapes or leftovers.
