"""logi-ghub-cli: command-line access to everything the GUI does."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__, keys, profiles
from .actions import Action, parse_action
from .device import LogiDevice, find_devices, select_device
from .features import lighting
from .features import onboard as ob
from .hidpp import DeviceOffline, HIDPPError
from .hidpp import features as F
from .macros import Macro


def _devices(args) -> list[LogiDevice]:
    if getattr(args, "fake", False):
        from .hidpp.fake import fake_devices

        return fake_devices()
    return find_devices()


def _device(args) -> LogiDevice:
    dev = select_device(_devices(args), args.device)
    if not dev.online:
        raise SystemExit(f"{dev.name}: {dev.error}")
    return dev


def _need(obj, what: str, dev: LogiDevice):
    if obj is None:
        raise SystemExit(f"{dev.name} does not support {what}")
    return obj


def _rgb(hexstr: str) -> int:
    return int(hexstr.lstrip("#"), 16)


# --- commands ----------------------------------------------------------------
def cmd_list(args) -> None:
    for d in _devices(args):
        state = "online" if d.online else f"offline ({d.error})"
        print(f"{d.key}  {d.info.path:14s} {d.kind:9s} {d.name}  [{state}]")


def cmd_info(args) -> None:
    dev = _device(args)
    info = dev.describe()
    if dev.dpi:
        cur, default = dev.dpi.get()
        values = dev.dpi.dpi_list()
        info["dpi"] = {"current": cur, "default": default, "min": values[0], "max": values[-1]}
    if dev.report_rate:
        info["report_rate_hz"] = {"current": dev.report_rate.get_hz(), "supported": dev.report_rate.supported_hz()}
    if dev.onboard:
        o = dev.onboard
        info["onboard"] = {
            "mode": {1: "on-board", 2: "host"}.get(o.get_mode(), "?"),
            "active_profile": o.get_current_profile(),
            "profiles": o.info.profile_count,
            "buttons": o.info.button_count,
            "sector_size": getattr(o.info, "sector_size", None),
            "format": getattr(o.info, "profile_format", None),
        }
    if dev.rgb:
        info["lighting_zones"] = [{"zone": z.index, "location": z.name, "effects": [e.name for e in z.effects]} for z in dev.rgb.zones]
    if dev.per_key:
        info["per_key_zones"] = len(dev.per_key.zones)
    print(json.dumps(info, indent=2))


def cmd_probe(args) -> None:
    """Read-only dump of a device's feature table, for adding support for new models."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    devices = _devices(args)
    if args.device:
        devices = [select_device(devices, args.device)]
    for dev in devices:
        if not dev.online or dev.hidpp is None:
            print(f"skip {dev.name}: {dev.error or 'not a HID++ device'}")
            continue
        h = dev.hidpp
        data = dev.describe()
        data.pop("path", None)
        data["features"] = [
            {"index": idx, "id": f"{fid:#06x}", "name": F.short_name(fid), "type": h.feature_meta[idx][1], "version": h.feature_meta[idx][2]}
            for fid, idx in sorted(h.features.items(), key=lambda x: x[1])
        ]
        raw = {}

        def grab(label, fid, fn, *params):
            if fid in h.features:
                try:
                    raw[label] = h.call(fid, fn, *params).hex()
                except Exception as e:  # noqa: BLE001 - record whatever the device says
                    raw[label] = f"error: {e}"

        grab("dpi.sensor_count", F.ADJUSTABLE_DPI, 0)
        grab("dpi.list", F.ADJUSTABLE_DPI, 1, 0)
        grab("dpi.get", F.ADJUSTABLE_DPI, 2, 0)
        grab("report_rate.list", F.REPORT_RATE, 0)
        grab("ext_report_rate.list", F.EXTENDED_ADJUSTABLE_REPORT_RATE, 1)
        grab("onboard.description", F.ONBOARD_PROFILES, 0)
        grab("onboard.mode", F.ONBOARD_PROFILES, 2)
        grab("onboard.current", F.ONBOARD_PROFILES, 4)
        grab("button_spy.count", F.MOUSE_BUTTON_SPY, 0)
        grab("gkey.count", F.GKEY, 0)
        grab("rgb.info", F.RGB_EFFECTS, 0, 0xFF, 0xFF, 0)
        grab("rgb.sw_control", F.RGB_EFFECTS, 5, 0)
        grab("color_led.info", F.COLOR_LED_EFFECTS, 0)
        grab("brightness.info", F.BRIGHTNESS_CONTROL, 0)
        grab("brightness.get", F.BRIGHTNESS_CONTROL, 1)
        for page in range(3):
            grab(f"per_key.info.{page}", F.PER_KEY_LIGHTING_V2, 0, 0, page)
        grab("reprog.count", F.REPROG_CONTROLS_V4, 0)
        if F.RGB_EFFECTS in h.features and dev.rgb:
            data["rgb_zones"] = [
                {"zone": z.index, "location": z.location, "effects": [{"index": e.index, "id": e.effect_id, "name": e.name, "caps": e.capabilities, "period": e.period} for e in z.effects]}
                for z in dev.rgb.zones
            ]
        data["raw"] = raw
        path = out_dir / f"{dev.key.replace(':', '-')}.json"
        path.write_text(json.dumps(data, indent=2))
        print(f"wrote {path}")


def cmd_dpi(args) -> None:
    dev = _device(args)
    dpi = _need(dev.dpi, "DPI changes", dev)
    if args.value is None:
        cur, default = dpi.get()
        print(f"{cur} (default {default})")
        return
    values = dpi.dpi_list()
    if args.value not in values:
        raise SystemExit(f"{args.value} DPI is not supported; range {values[0]}-{values[-1]}")
    dpi.set(args.value)
    print(f"DPI set to {args.value} (until the next profile or DPI button change)")


def cmd_rate(args) -> None:
    dev = _device(args)
    rr = _need(dev.report_rate, "report rate changes", dev)
    if args.hz is None:
        print(f"{rr.get_hz()} Hz (supported: {', '.join(map(str, rr.supported_hz()))})")
        return
    rr.set_hz(args.hz)
    print(f"report rate set to {args.hz} Hz")


def _print_profile(dev: LogiDevice, p: ob.OnboardProfile, active: int) -> None:
    flag = "*" if p.sector == active else " "
    state = "enabled" if p.enabled else "disabled"
    print(f"{flag} slot {p.sector}: {p.name or '(unnamed)'} [{state}]")
    print(f"    report rate {1000 // max(1, p.report_rate_ms)} Hz, DPI {p.active_dpis} default #{p.default_dpi_index + 1}, shift #{p.shift_dpi_index + 1}")
    count = dev.binding_count()
    gshift = dev.onboard.info.has_gshift
    for i in range(count):
        line = f"    {i + 1:2d} {dev.button_name(i):28s} {p.button(i).label()}"
        if gshift and p.button(i, True).to_bytes() != p.button(i).to_bytes():
            line += f"   | G-Shift: {p.button(i, True).label()}"
        print(line)


def cmd_onboard(args) -> None:
    dev = _device(args)
    sub = args.onboard_cmd
    if sub == "modular-actuation-experiment":
        from .features.modular_flash_experiment import preview, apply_experimental

        plan = preview(dev, args.slot, args.mm)
        print(f"PRO X2 RAPID experimental flash write, profile {plan['slot']} (active)")
        print(f"  current: {plan['old_mm']:.2f} mm [{plan['old_raw_hex']}]")
        print(f"  proposed: {plan['new_mm']:.2f} mm [{plan['new_raw_hex']}]")
        print(f"  known-tag snapshot: {plan['snapshot_path']} (NOT restorable)")
        if plan['unchanged']:
            print("Already identical; no flash write needed")
        elif not args.apply:
            print("PREVIEW ONLY: no HID++ write was sent. --apply enables an interactive EXPERIMENT.")
        else:
            result = apply_experimental(dev, plan)
            print(f"Result: {result['status']} — disconnect/reconnect USB and independently re-read to verify persistence")
        return
    if sub == "modular":
        from .features.modular_profiles import (
            export_profiles, profile_overview, sanitized_summary, save_private_json,
        )

        report = export_profiles(dev, slots=(args.slot,) if args.slot else (1, 2, 3))
        for entry in profile_overview(report):
            active = " [active]" if entry["slot"] == report["active_before"] else ""
            rate = entry["wired_report_rate_hz"]
            actuation = entry["global_actuation_mm"]
            print(f"Profile {entry['slot']}: {entry['name'] or '(unnamed)'}{active}")
            print(f"  USB report rate: {rate if rate is not None else '?'} Hz (stored, not measured)")
            print(f"  Actuation point: {actuation if actuation is not None else '?'} mm")
            print(f"  Rapid Trigger: {entry['rapid_trigger']}  Key Priority: {entry['key_priority']}")
            print(f"  Priority pairs: {entry['key_priority_pairs']}")
        failures = report["statistics"]["tag_failures"]
        print(f"Known tags: {report['statistics']['tags_attempted'] - failures}/"
              f"{report['statistics']['tags_attempted']} read; this is not a restorable flash/RGB backup")
        if args.out:
            path = Path(args.out).expanduser()
            save_private_json(path, report)
            summary = path.with_name(path.stem + "_summary.json")
            save_private_json(summary, sanitized_summary(report))
            print(f"Private known-tag export: {path}; redacted summary: {summary}")
        return
    if dev.onboard is None and dev.has(F.PROFILE_MANAGEMENT) and sub == "select":  # newer keyboards (G915 X)
        from .features.profile_fs import ProfileFS

        fs = ProfileFS(dev.hidpp)
        try:
            fs.select(args.slot)
        except ValueError as e:
            raise SystemExit(str(e)) from None
        print(f"profile {args.slot} in use" + ("" if fs.active()[0] else " (the computer still controls the lighting)"))
        return
    memory = _need(dev.onboard, "on-board profiles", dev)
    if sub == "show":
        active = memory.get_current_profile()
        mode = {1: "on-board", 2: "host"}.get(memory.get_mode(), "?")
        print(f"{dev.name}: mode {mode}, {memory.info.profile_count} slots, active slot {active}")
        for p in memory.read_profiles():
            if args.slot and p.sector != args.slot:
                continue
            _print_profile(dev, p, active)
    elif sub == "backup":
        path = Path(args.out) if args.out else None
        data = memory.backup()
        if path is None:
            path = ob.save_backup(memory, dev.key)
        else:
            path.write_text(json.dumps(data, indent=1))
        print(f"backup written to {path}")
    elif sub == "restore":
        data = json.loads(Path(args.file).read_text())
        if not args.yes:
            raise SystemExit("restore rewrites on-board memory; add --yes to confirm")
        path = ob.save_backup(memory, dev.key)
        changed = memory.restore(data)
        print(f"safety backup of the current state: {path}")
        print(f"restored sectors: {', '.join(hex(s) for s in changed) or 'none (already identical)'}")
    elif sub == "mode":
        if args.mode is None:
            print({1: "onboard", 2: "host"}.get(memory.get_mode(), "?"))
        else:
            memory.set_mode(ob.MODE_ONBOARD if args.mode == "onboard" else ob.MODE_HOST)
            print(f"mode set to {args.mode}")
    elif sub == "select":
        memory.set_current_profile(args.slot)
        print(f"slot {args.slot} active")
    elif sub == "set":
        _onboard_set(dev, memory, args)


def _onboard_set(dev: LogiDevice, memory: ob.OnboardProfiles, args) -> None:
    profiles_now = memory.read_profiles()
    if not 1 <= args.slot <= len(profiles_now):
        raise SystemExit(f"slot must be 1-{len(profiles_now)}")
    p = profiles_now[args.slot - 1].copy()
    slots = dev.binding_count()
    prof = profiles.Profile.from_onboard(dev.key, p, slots, memory)
    if args.dpi:
        prof.dpi_stages = [int(x) for x in args.dpi.split(",")]
        if prof.shift_dpi not in prof.dpi_stages:
            prof.shift_dpi = prof.dpi_stages[0]
    if args.default_dpi is not None:
        prof.default_dpi = args.default_dpi - 1
    if args.shift_dpi is not None:
        prof.shift_dpi = args.shift_dpi
    if args.rate:
        prof.report_rate = args.rate
    if args.name:
        prof.name = args.name
    for spec, layer in [(b, prof.buttons) for b in args.button or []] + [(b, prof.gshift_buttons) for b in args.gshift or []]:
        idx, _, action = spec.partition("=")
        i = int(idx) - 1
        if not 0 <= i < slots:
            raise SystemExit(f"button number must be 1-{slots}")
        layer[i] = parse_action(action)
    for spec in args.macro or []:
        name, _, text = spec.partition("=")
        prof.macros = [m for m in prof.macros if m.name != name] + [Macro.parse(name, text)]
    if not args.yes:
        print("This would write on-board slot", args.slot, "with:")
        print(json.dumps(prof.to_dict(), indent=2))
        raise SystemExit("re-run with --yes to write it (a backup is taken first)")
    backup = ob.save_backup(memory, dev.key)
    print(f"backup: {backup}")
    for change in profiles.write_to_onboard(memory, prof, args.slot, slots, enable=True):
        print(" -", change)


def cmd_lighting(args) -> None:
    dev = _device(args)
    sub = args.lighting_cmd
    if sub == "preset":
        from . import presets
        from .features import rgb_store

        if args.name not in presets.PRESETS:
            raise SystemExit("presets: " + ", ".join(presets.PRESETS))
        if not args.yes:
            raise SystemExit("this replaces the custom animation on brightness + 9; add --yes to confirm")
        title, make, period = presets.PRESETS[args.name]
        data = rgb_store.build_file(title, make(dev.per_key.zones, period_ms=period), period_ms=period)
        rgb_store.Uploader(dev.hidpp).upload(1, data)
        rgb_store.save_local(dev.key, 1, data)
        print(f"saved '{title}' to brightness + 9")
        return
    if sub == "store":
        from .features import rgb_store

        if not args.yes:
            raise SystemExit("this replaces the custom animation on brightness + 9; add --yes to confirm")
        prof = profiles.load_profile(dev.key, args.profile)
        info = rgb_store.store_lighting(dev, prof.lighting, (args.name or prof.name)[:31])
        print(f"saved '{info['name']}' to brightness + 9 ({len(info['frames'])} frames)")
        return
    if sub == "saved":
        from .features.profile_fs import ProfileFS

        if not dev.has(F.PROFILE_MANAGEMENT):
            raise SystemExit(f"{dev.name} has no on-board profile storage of this kind")
        names = dev.rgb.onboard_effect_names() if isinstance(dev.rgb, lighting.RGBEffects) else []
        fs = ProfileFS(dev.hidpp)
        onboard, in_use = fs.active()
        for sp in fs.profiles(0):
            desc = sp.lighting.describe()
            if sp.lighting.effect_id == 0x0C and sp.lighting.params[0] < len(names):
                desc = f"custom animation '{names[sp.lighting.params[0]]}'"
            mark = " [in use]" if sp.number == in_use else ""
            print(f"profile {sp.number} {sp.name!r}{mark}: {desc} (power save: {sp.powersave_lighting.describe()})")
        if not onboard:
            print("(the computer currently controls the keyboard)")
        return
    if sub == "info":
        rgb = _need(dev.rgb, "lighting", dev)
        for z in rgb.zones:
            print(f"zone {z.index} ({z.name}): " + ", ".join(f"{e.name} [{e.effect_id:#04x}]" for e in z.effects))
        if dev.per_key:
            print(f"per-key: {len(dev.per_key.zones)} keys")
        if dev.brightness:
            print(f"brightness: {dev.brightness.get()} ({dev.brightness.min}-{dev.brightness.max})")
        return
    if sub == "brightness":
        b = _need(dev.brightness, "brightness control", dev)
        if args.value is None:
            print(b.get())
        else:
            b.set(args.value)
        return
    rgb = _need(dev.rgb, "lighting", dev)
    if sub == "sequence":
        from . import lightfx

        _need(dev.per_key, "per-key lighting", dev)
        light = {"animation": {"enabled": True, "colors": [_rgb(c) for c in args.colors.split(",")], "step_ms": args.step,
                               "fade": not args.no_fade, "pattern": args.pattern, "keep_painted": False}}
        anim = lightfx.apply(dev, light)
        print("playing; Ctrl+C to stop (to keep it running, save it in a profile and use host mode)")
        try:
            while anim.is_alive():
                anim.join(0.5)
        except KeyboardInterrupt:
            anim.stop()
        return
    if sub == "release":
        rgb.release()
        if dev.onboard:
            dev.onboard.set_mode(ob.MODE_ONBOARD)
        print("lighting handed back to the device")
        return
    if dev.onboard and dev.onboard.get_mode() != ob.MODE_HOST:
        dev.onboard.set_mode(ob.MODE_HOST)
    if hasattr(rgb, "claim"):
        rgb.claim()
    if sub == "effect":
        match = {v[0].lower(): k for k, v in lighting.EFFECTS.items()}
        name = args.effect.lower()
        eid = int(name, 0) if name[:1].isdigit() else match.get(name)
        if eid is None:
            raise SystemExit(f"unknown effect {args.effect!r}")
        eff = lighting.Effect(eid)
        if args.color:
            eff.color = _rgb(args.color)
        if args.period:
            eff.period = args.period
        if args.brightness is not None:
            eff.intensity = args.brightness
        zones = range(len(rgb.zones)) if args.zone is None else [args.zone]
        for z in zones:
            if rgb.zones[z].effect_by_id(eid):
                rgb.set_effect(z, eff)
                print(f"zone {z}: {eff.name}")
    elif sub == "keys":
        pk = _need(dev.per_key, "per-key lighting", dev)
        if args.all:
            rgb.hold_static(_rgb(args.all))
            pk.fill(_rgb(args.all))
        colors = {}
        from .macros import resolve_key

        for spec in args.key or []:
            k, _, c = spec.partition("=")
            zone = lighting.SPECIAL_ZONES.get(k) or lighting.zone_for_hid(keys.hid_for(resolve_key(k)))
            if zone is None:
                raise SystemExit(f"no lighting zone for {k}")
            colors[zone] = _rgb(c)
        if colors:
            pk.set_colors(colors)
        print("per-key colours set")
    print("Note: the device returns to its on-board lighting when the daemon is not running and it wakes from sleep.")


def cmd_profile(args) -> None:
    sub = args.profile_cmd
    if sub == "list":
        dev = _device(args)
        active = profiles.get_active().get(dev.key)
        for p in profiles.list_profiles(dev.key):
            print(("* " if p.name == active else "  ") + p.name)
        return
    if sub == "import":
        dev = _device(args)
        memory = _need(dev.onboard, "on-board profiles", dev)
        p = memory.read_profiles()[args.slot - 1]
        prof = profiles.Profile.from_onboard(dev.key, p, dev.binding_count(), memory, name=args.name)
        print(f"saved {profiles.save_profile(prof)}")
        return
    if sub == "show":
        dev = _device(args)
        print(json.dumps(profiles.load_profile(dev.key, args.name).to_dict(), indent=2))
        return
    if sub == "write":
        dev = _device(args)
        memory = _need(dev.onboard, "on-board profiles", dev)
        prof = profiles.load_profile(dev.key, args.name)
        if not args.yes:
            raise SystemExit(f"this rewrites on-board slot {args.slot}; add --yes to confirm (a backup is taken first)")
        print(f"backup: {ob.save_backup(memory, dev.key)}")
        for change in profiles.write_to_onboard(memory, prof, args.slot, dev.binding_count()):
            print(" -", change)
        return
    if sub == "activate":
        dev = _device(args)
        if args.name.lower() in ("none", "onboard"):
            profiles.set_active(dev.key, None)
            print("device will use its on-board profiles (daemon releases it)")
        else:
            profiles.load_profile(dev.key, args.name)
            profiles.set_active(dev.key, args.name)
            print(f"daemon will run '{args.name}' in host mode on {dev.name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="logi-ghub-cli", description="Configure Logitech G devices on Linux")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-d", "--device", help="device: model id (046d:409f), hidraw path, 'mouse'/'keyboard' or part of the name")
    ap.add_argument("--fake", action="store_true", help="use simulated devices (no hardware needed)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("list", help="list devices")
    sp.add_parser("info", help="show device details")
    p = sp.add_parser("probe", help="dump a read-only feature report to JSON")
    p.add_argument("--out", default="data/devices")

    p = sp.add_parser("dpi", help="get or set the current DPI")
    p.add_argument("value", nargs="?", type=int)
    p = sp.add_parser("rate", help="get or set the report rate")
    p.add_argument("hz", nargs="?", type=int)

    p = sp.add_parser("onboard", help="on-board memory profiles")
    osp = p.add_subparsers(dest="onboard_cmd", required=True)
    q = osp.add_parser("show")
    q.add_argument("--slot", type=int)
    q = osp.add_parser("modular", help="Read PRO X2 RAPID modular on-board profiles (no writes)")
    q.add_argument("--slot", type=int, choices=(1, 2, 3))
    q.add_argument("--out", help="write a private known-tag JSON export and redacted summary")
    q = osp.add_parser("modular-actuation-experiment", help="UNVERIFIED single-tag flash write on active PRO X2 RAPID (preview by default)")
    q.add_argument("--slot", type=int, choices=(1, 2, 3), required=True)
    q.add_argument("--mm", required=True, help="new actuation in mm: 0.10–4.00, steps of 0.05")
    q.add_argument("--apply", action="store_true", help="EXPERIMENT: interactive flash write; risk of configuration damage")
    q = osp.add_parser("backup")
    q.add_argument("--out")
    q = osp.add_parser("restore")
    q.add_argument("file")
    q.add_argument("--yes", action="store_true")
    q = osp.add_parser("mode")
    q.add_argument("mode", nargs="?", choices=["onboard", "host"])
    q = osp.add_parser("select")
    q.add_argument("slot", type=int)
    q = osp.add_parser("set", help="edit one slot: DPI, report rate, buttons, macros")
    q.add_argument("--slot", type=int, required=True)
    q.add_argument("--dpi", help="comma separated DPI stages, e.g. 800,1600,3200")
    q.add_argument("--default-dpi", type=int, help="1-based default stage")
    q.add_argument("--shift-dpi", type=int, help="DPI while the sniper button is held (must be one of the stages)")
    q.add_argument("--rate", type=int, help="report rate in Hz")
    q.add_argument("--name")
    q.add_argument("--button", action="append", help="N=ACTION, e.g. 4=key:Ctrl+C, 6=consumer:Mute, 9=macro:hello")
    q.add_argument("--gshift", action="append", help="N=ACTION for the G-Shift layer")
    q.add_argument("--macro", action="append", help='NAME=STEPS, e.g. hello="Hello", Enter')
    q.add_argument("--yes", action="store_true")

    p = sp.add_parser("lighting", help="lighting effects and per-key colours")
    lsp = p.add_subparsers(dest="lighting_cmd", required=True)
    lsp.add_parser("info")
    lsp.add_parser("saved", help="lighting saved in the keyboard's on-board profiles (read-only)")
    q = lsp.add_parser("preset", help="store a ready-made animation on brightness + 9 (python-romania)")
    q.add_argument("name")
    q.add_argument("--yes", action="store_true")
    q = lsp.add_parser("store", help="save a computer profile's lighting as the keyboard's brightness + 9 animation")
    q.add_argument("profile", help="computer profile whose painted keys / colour sequence to store")
    q.add_argument("--name", help="name to store it under (31 characters)")
    q.add_argument("--yes", action="store_true")
    lsp.add_parser("release", help="give lighting control back to the device")
    q = lsp.add_parser("brightness")
    q.add_argument("value", nargs="?", type=int)
    q = lsp.add_parser("effect")
    q.add_argument("effect", help="Off, Static, Breathing, Color cycle, Color wave, Ripple, ...")
    q.add_argument("--zone", type=int)
    q.add_argument("--color", help="RRGGBB")
    q.add_argument("--period", type=int, help="milliseconds")
    q.add_argument("--brightness", type=int)
    q = lsp.add_parser("sequence", help="play your own colour sequence until Ctrl+C")
    q.add_argument("colors", help="comma separated RRGGBB colours, in order")
    q.add_argument("--step", type=int, default=1000, help="milliseconds per colour")
    q.add_argument("--no-fade", action="store_true")
    q.add_argument("--pattern", default="all", choices=["all", "wave_right", "wave_left", "wave_down", "wave_up", "center_out"])
    q = lsp.add_parser("keys")
    q.add_argument("key", nargs="*", help="KEY=RRGGBB, e.g. W=ff0000 Esc=00ff00")
    q.add_argument("--all", help="fill all keys with RRGGBB first")

    p = sp.add_parser("profile", help="profiles saved on this computer")
    psp = p.add_subparsers(dest="profile_cmd", required=True)
    psp.add_parser("list")
    q = psp.add_parser("import", help="save an on-board slot as a computer profile")
    q.add_argument("--slot", type=int, required=True)
    q.add_argument("--name", required=True)
    q = psp.add_parser("show")
    q.add_argument("name")
    q = psp.add_parser("write", help="write a computer profile into an on-board slot")
    q.add_argument("name")
    q.add_argument("--slot", type=int, required=True)
    q.add_argument("--yes", action="store_true")
    q = psp.add_parser("activate", help="have the daemon run a profile in host mode ('none' to stop)")
    q.add_argument("name")

    sp.add_parser("daemon", help="run the host-mode daemon in the foreground")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.fake:
        from .config import use_sandbox

        use_sandbox()
    try:
        if args.cmd == "daemon":
            from .daemon import main as daemon_main

            return daemon_main([])
        handler = {"list": cmd_list, "info": cmd_info, "probe": cmd_probe, "dpi": cmd_dpi, "rate": cmd_rate,
                   "onboard": cmd_onboard, "lighting": cmd_lighting, "profile": cmd_profile}[args.cmd]
        handler(args)
    except (LookupError, ValueError, HIDPPError, DeviceOffline, TimeoutError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
