# PRO X2 RAPID: EXPERIMENTAL flash write of global actuation

**Not verified on a physical keyboard. This is not part of the upstream read-only PR.**

The confirmed hardware read-only implementation remains on
`feat/pro-x2-rapid-modular-profiles-gui`. This separate experiment branch
adds an opt-in **single-tag** flash transaction for USB `046d:c364`, HID++
feature `0x8102`, tag `0x0120` (global actuation point). It is not a
general hardware profile editor.

## Technical uncertainty — read this before attempting

- A separate reverse engineering project documents G HUB's `0x8102`
  `fn9` start-write / `fn10` fragment / `fn11` CRC-32 commit sequence.
- **Its write function does not contain a destination profile number**.
  This implementation assumes writes apply to the **currently active**
  hardware slot and insists that the requested slot is active.
  This **has not been proven on PRO X2 RAPID hardware**.
- Our keyboard rejects `fn4 END`. Since read operations worked without
  any session, this experiment tries **one `fn9` plus one `fn11` without
  `fn4` or fallbacks**. The write might be rejected (in that case, stop).
- A successful immediate read-back does **not** prove data survived USB
  disconnect/power cycle. A test must re-read after reconnection.
- No known complete flash image or verified restore protocol is available.
  The saved JSON captures **only known tags** and is **NOT RESTORABLE**.
  An unsuccessful write could change the wrong profile or corrupt settings.

## Requirements

1. Use the directly attached USB PRO X2 RAPID. Close all other
   applications that own the keyboard, including the GTK GUI.
2. Stop `logi-ghubd` to prevent concurrent HID++ traffic.
3. Physically select the profile to edit with the keyboard's controls.
4. Use only a disposable test setting, such as changing 1.00 mm to 1.50 mm.
   Actuation values are limited to 0.10–4.00 mm, on exact 0.05 mm increments.

## Preview (always read-only)

```bash
systemctl --user stop logi-ghubd
.venv/bin/logi-ghub-cli -d 046d:c364 onboard modular-actuation-experiment --slot 2 --mm 1.50
```

This reads all known tags from profiles 1–3, verifies the active slot,
and saves an owner-only pre-write JSON snapshot in
`~/.local/state/logi-ghub-linux/prewrite-known-tags-*.json`.

## One-shot experimental commit

**Only after inspecting the preview and accepting that rollback is NOT
available:**

```bash
.venv/bin/logi-ghub-cli -d 046d:c364 onboard modular-actuation-experiment --slot 2 --mm 1.50 --apply
```

The CLI additionally requires a real interactive terminal and an exact
typed confirmation. It then:

1. Rechecks that the active slot still matches.
2. Re-reads tag `0x0120` and refuses a stale preview.
3. Sends **only** `0x8102 fn9` with `[0x01,0x20,0x00,0x02, mm_hi, mm_lo]`.
4. If `fn9` answered, sends **once** `fn11` with big-endian CRC-32 of
   those two value bytes. No `fn10` is necessary for a two-byte tag.
5. Reads tag `0x0120` for all three profiles and checks that only the
   selected one changed, then reads the active slot again.

**Never retry automatically** after an error (especially once `fn9`
has answered). The device's transaction/flash state may be unknown.
Do not use the JSON snapshot as a restore input.

If acknowledged and read-back matches, reconnect USB and separately run
the existing read-only `onboard modular` command to check persistence.
The GUI intentionally provides **no Flash Save** button until the protocol
is independently verified on real hardware.

Protocol source: https://github.com/HolyJoey/Lghub/blob/main/device_settings.py
