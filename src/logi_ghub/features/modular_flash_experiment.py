"""Experimental single-tag writes for Logitech PRO X2 RAPID.

This is NOT a supported flash editor and NOT a restorable backup. The 0x8102
fn9/fn11 protocol is reverse-engineered but NOT yet verified on this keyboard.
Only an explicit, interactive, one-shot write to the physically active slot is
allowed. This module never selects a profile or sends a session operation.
"""
from __future__ import annotations

import struct
import sys
import zlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .modular_profiles import (
    CallBudget,
    DEVICE_KEY,
    FEATURE,
    export_profiles,
    read_tag,
    save_private_json,
)

WRITE_TAG = 0x0120
CONFIRM_TOKEN = 'WRITE FLASH'


class ExperimentalWriteError(ValueError):
    pass


def encode_actuation(value: str) -> bytes:
    """Only physical switch settings confirmed by Logitech: 0.10–4.00 mm, 0.05 steps."""
    try:
        mm = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('actuation must be a decimal distance in mm') from exc
    if not mm.is_finite() or not Decimal('0.10') <= mm <= Decimal('4.00'):
        raise ValueError('actuation must be between 0.10 and 4.00 mm')
    hundredths = mm * 100
    if hundredths != hundredths.to_integral_value() or int(hundredths) % 5:
        raise ValueError('actuation must be a multiple of 0.05 mm')
    return struct.pack('>H', int(hundredths))


def _raw_tag(report: dict, slot: int) -> bytes:
    row = report['profiles'][str(slot)]['global_actuation']
    if row.get('status') != 'ok' or not row.get('complete') or row.get('actual_length') != 2:
        raise ExperimentalWriteError(f'profile {slot} actuation tag is not complete; refusing write')
    return bytes.fromhex(row['raw_hex'])


def _device_guard(dev) -> None:
    info = dev.info
    if (dev.key.lower() != DEVICE_KEY or getattr(info, 'vid', None) != 0x046d
            or getattr(info, 'pid', None) != 0xc364 or dev.hidpp is None
            or not dev.has(FEATURE)):
        raise ExperimentalWriteError('requires the directly attached USB PRO X2 RAPID (046d:c364)')


def _snapshot_file() -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return Path.home() / '.local' / 'state' / 'logi-ghub-linux' / f'prewrite-known-tags-{stamp}.json'


def preview(dev, slot: int, mm: str) -> dict:
    """Preflight and snapshot. No HID++ writes under any condition."""
    _device_guard(dev)
    if slot not in (1, 2, 3):
        raise ValueError('hardware slot must be 1, 2 or 3')
    target = encode_actuation(mm)
    report = export_profiles(dev)
    if report.get('active_before') != slot or report.get('active_after') != slot:
        raise ExperimentalWriteError(
            f'physically select profile {slot} on the keyboard first; '
            f'active before/after was {report.get("active_before")}/{report.get("active_after")}')
    old = _raw_tag(report, slot)
    snapshot = _snapshot_file()
    save_private_json(snapshot, report)
    return {
        'slot': slot,
        'old_raw_hex': old.hex(' '),
        'new_raw_hex': target.hex(' '),
        'old_mm': int.from_bytes(old, 'big') / 100,
        'new_mm': int.from_bytes(target, 'big') / 100,
        'unchanged': old == target,
        'baseline_all_slots': {str(i): _raw_tag(report, i).hex(' ') for i in (1, 2, 3)},
        'snapshot_path': str(snapshot),
        'warning': 'Snapshot is not a restorable flash/RGB backup.',
    }


def _verify_against_preflight(hidpp, slot: int, original: bytes) -> CallBudget:
    budget = CallBudget()
    reply = budget.call(hidpp, 2)
    if not reply or reply[0] != slot:
        raise ExperimentalWriteError('active profile changed; refusing write')
    tag = read_tag(hidpp, budget, slot, WRITE_TAG)
    if not tag.get('complete') or bytes.fromhex(tag['raw_hex']) != original:
        raise ExperimentalWriteError('stored actuation changed since preflight; refusing write')
    return budget


def _commit_single_tag(hidpp, data: bytes) -> dict:
    """Unverified transfer + CRC commit; no fallback or guessed session calls.

    Executes real flash modification requests. Do not call this function without
    complete preflight and interactive confirmation. Never retry automatically.
    """
    if len(data) != 2:
        raise ValueError('only the 2-byte global actuation tag is supported')
    start = struct.pack('>HH', WRITE_TAG, len(data)) + data
    response = hidpp.call(FEATURE, 9, start)
    if not response:
        raise ExperimentalWriteError('fn9 returned an empty response: transaction state unknown')
    crc = zlib.crc32(data) & 0xffffffff
    end = hidpp.call(FEATURE, 11, struct.pack('>I', crc))
    if not end or end[0] != 0:
        raise ExperimentalWriteError(
            'fn11 commit returned no acknowledged success; flash state unknown: '
            + (end.hex(' ') if end else '(empty)'))
    return {'write_reply_hex': response.hex(' '), 'commit_reply_hex': end.hex(' '), 'crc32': f'{crc:08x}'}


def apply_experimental(dev, plan: dict, *, input_func=input, output_func=print) -> dict:
    """Interactive, opt-in and single-shot; no auto-retry or rollback guesses."""
    _device_guard(dev)
    if plan['unchanged']:
        return {'status': 'unchanged', 'message': 'Skipped: flash is already set to that value.'}
    if not sys.stdin.isatty() and input_func is input:
        raise ExperimentalWriteError('interactive terminal required; refusing unattended flash write')
    token = f'P{plan["slot"]} {plan["new_mm"]:.2f} {CONFIRM_TOKEN}'
    output_func('UNVERIFIED FLASH WRITE. This can change the wrong slot or make its settings unusable.')
    output_func(f'Known-tag snapshot (NOT restorable): {plan["snapshot_path"]}')
    output_func('No automatic rollback exists. If the protocol fails, DO NOT immediately retry.')
    typed = input_func(f'Type exactly "{token}" to proceed: ')
    if typed != token:
        raise ExperimentalWriteError('write cancelled: confirmation did not match')
    hidpp = dev.hidpp
    previous = hidpp.long_only
    old = bytes.fromhex(plan['old_raw_hex'])
    new = bytes.fromhex(plan['new_raw_hex'])
    if len(old) != 2 or len(new) != 2:
        raise ExperimentalWriteError('preflight tag size mismatch')
    if encode_actuation(f'{plan["new_mm"]:.2f}') != new:
        raise ExperimentalWriteError('preflight value mismatch')
    if bytes.fromhex(plan['baseline_all_slots'][str(plan['slot'])]) != old:
        raise ExperimentalWriteError('baseline mismatch; refusing write')
    try:
        hidpp.long_only = True
        _verify_against_preflight(hidpp, plan['slot'], old)
        try:
            transaction = _commit_single_tag(hidpp, new)
        except Exception as exc:
            raise ExperimentalWriteError(
                f'Experimental fn9/fn11 failed: {exc}. Device state may be unknown; '
                'stop and inspect before any further attempt.') from exc
        budget = CallBudget()
        after = {i: read_tag(hidpp, budget, i, WRITE_TAG) for i in (1, 2, 3)}
        active = budget.call(hidpp, 2)
        if (not active or active[0] != plan['slot'] or not after[plan['slot']].get('complete')
                or bytes.fromhex(after[plan['slot']]['raw_hex']) != new):
            raise ExperimentalWriteError(
                'Commit was acknowledged but immediate read-back did not match; '
                'do not retry or assume that data persisted.')
        for i in (1, 2, 3):
            if i != plan['slot'] and (not after[i].get('complete') or
                    bytes.fromhex(after[i]['raw_hex']) != bytes.fromhex(plan['baseline_all_slots'][str(i)])):
                raise ExperimentalWriteError(
                    f'non-target profile {i} changed unexpectedly; stop and inspect, do not retry')
        return {'status': 'readback_ok_not_yet_powercycle_verified', 'slot': plan['slot'],
                'new_mm': plan['new_mm'], 'snapshot_path': plan['snapshot_path'], **transaction}
    finally:
        hidpp.long_only = previous
