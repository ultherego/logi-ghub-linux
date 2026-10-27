# PRO X2 RAPID: flash-write experiment (SUSPENDED after device rejection)

This is an **experimental-only branch**, separate from the functional read-only
GUI PR #7. The Logitech PRO X2 RAPID firmware (USB `046d:c364`) rejected the
first real write attempt on 2026-10-27.

## Hardware result

- Target: **profile 2**, global actuation tag `0x0120`, `1.00 mm → 1.50 mm`.
- Pre-write 3-profile known-tag snapshot was saved successfully.
- The first write request `0x8102 fn9 [01 20 00 02 00 96]` returned
  **HID++ 2.0 error `0x05 NOT_ALLOWED`**.
- The program stopped immediately. **No `fn11` commit was sent.**
- The hardware state after the rejection has not yet been established by
  a separate power-cycle readback.

Historically, some HID++ implementations call error `0x05` "Logitech
internal". The Linux HID++ driver calls it **NOT_ALLOWED**, reflecting
Logitech feature specifications:
https://lkml.iu.edu/hypermail/linux/kernel/2306.2/03845.html

The independent reverse engineering of G HUB identifies `fn9` (start
write), `fn10` (fragment) and `fn11` (commit), but **does not establish
how a destination hardware profile is selected**. The observed device might
require a different session/permission context. It is not safe to infer
such a context from unsuccessful reads or to retry with guessed arguments.

## What is enabled in this branch

- `onboard modular`: existing read-only 3-profile known-tag export.
- `onboard modular-actuation-experiment --slot N --mm X`: **preview only**,
  creating an owner-only read-only snapshot. `--apply` is **disabled**.
- `onboard modular-flash-audit --before PATH`: reads known tags again
  using **only HID++ fn2/fn6/fn7**, compares all three hardware profiles
  to the snapshot taken immediately before the failed `fn9`, and writes
  private after-snapshot and audit JSON files. Unavailable tags are
  reported separately rather than assumed identical.

## Audit after USB reconnection

Close other keyboard-control software, stop `logi-ghubd`, disconnect and
reconnect the keyboard, then:

```bash
cd ~/Pobrane/test/logi-ghub-linux-flash-experiment
git fetch origin experiment/pro-x2-rapid-flash-actuation
git switch --detach FETCH_HEAD  # worktree originally created in detached mode
systemctl --user stop logi-ghubd
.venv/bin/logi-ghub-cli -d 046d:c364 onboard modular-flash-audit \
  --before ~/.local/state/logi-ghub-linux/prewrite-known-tags-20261027T175332900490Z.json
```

The audit does **not** perform any flash writes. A comparison of all known
tags is not a full firmware/flash integrity scan or a restorable backup.
The two previously unsupported tags `0x0004` and `0x0101` remain
unverifiable on this keyboard.

## Required evidence before another write

Obtain a verified legitimate G HUB HID++ capture showing profile selection,
write authorization/session operations, exact `fn9/fn10/fn11` packets
and successful read-back/persistence on this model. Do not experiment
with random function numbers or values on the user's only keyboard.

Protocol research:
https://github.com/HolyJoey/Lghub/blob/main/device_settings.py
