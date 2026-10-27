"""Read-only post-write audit for PRO X2 RAPID known hardware profile tags.

Checks a saved prewrite known-tag snapshot against a fresh hardware read.
No writes, selections or HID++ session requests. A snapshot is NOT a backup.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .modular_profiles import DEVICE_KEY, TAGS, export_profiles, save_private_json


def compare_known_tags(before: dict, after: dict) -> dict:
    """Compare exact raw tag bytes, preserving inaccessible-tag information."""
    if before.get('device') != DEVICE_KEY or after.get('device') != DEVICE_KEY:
        raise ValueError('expected PRO X2 RAPID snapshots (046d:c364)')
    expected = {str(slot) for slot in (1, 2, 3)}
    if set(before.get('profiles', {})) != expected or set(after.get('profiles', {})) != expected:
        raise ValueError('both snapshots must contain all three hardware profiles')
    changes, unverified = [], []
    compared = 0
    for slot in (1, 2, 3):
        initial = before['profiles'][str(slot)]
        current = after['profiles'][str(slot)]
        for label in TAGS.values():
            old = initial.get(label, {})
            new = current.get(label, {})
            if not (old.get('status') == 'ok' and old.get('complete') is True
                    and new.get('status') == 'ok' and new.get('complete') is True):
                unverified.append({'profile': slot, 'tag': label,
                                   'before_status': old.get('status', 'missing'),
                                   'after_status': new.get('status', 'missing')})
                continue
            if not isinstance(old.get('raw_hex'), str) or not isinstance(new.get('raw_hex'), str):
                unverified.append({'profile': slot, 'tag': label, 'error': 'raw_hex missing'})
                continue
            compared += 1
            if old['raw_hex'] != new['raw_hex']:
                changes.append({'profile': slot, 'tag': label,
                                'before_hex': old['raw_hex'], 'after_hex': new['raw_hex'],
                                'before_decoded': old.get('decoded', {}),
                                'after_decoded': new.get('decoded', {})})
    return {
        'device': DEVICE_KEY,
        'before_captured_utc': before.get('captured_utc'),
        'before_active': before.get('active_after'),
        'after_active': after.get('active_after'),
        'after_captured_utc': after.get('captured_utc'),
        'compared_tags': compared,
        'changed_tags': changes,
        'unverified_tags': unverified,
        'all_comparable_tags_unchanged': not changes,
        'complete_known_tag_equality': not changes and not unverified,
        'note': 'Known tags only. Cannot prove entire flash or RGB memory is unchanged.',
    }


def audit_hardware(dev, before_path: Path) -> tuple[dict, Path, Path]:
    path = Path(before_path).expanduser()
    with path.open('r', encoding='utf-8') as f:
        before = json.load(f)
    if before.get('device') != DEVICE_KEY or before.get('read_only') is not True:
        raise ValueError('prewrite snapshot is not a read-only PRO X2 RAPID known-tag export')
    after = export_profiles(dev)  # read-only fn2/fn6/fn7
    comparison = compare_known_tags(before, after)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out_dir = Path.home() / '.local' / 'state' / 'logi-ghub-linux'
    after_path = out_dir / f'postwrite-known-tags-{stamp}.json'
    audit_path = out_dir / f'postwrite-audit-{stamp}.json'
    save_private_json(after_path, after)
    save_private_json(audit_path, comparison)
    return comparison, after_path, audit_path
