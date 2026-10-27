import pytest

from logi_ghub.features import modular_flash_audit as audit


def snapshot(data='00 64', active=2):
    return {
        'device': '046d:c364', 'read_only': True, 'active_before': active, 'active_after': active,
        'profiles': {str(i): {'global_actuation': {
            'status': 'ok', 'complete': True, 'raw_hex': data,
            'decoded': {'mm': int(data.replace(' ', ''), 16)/100},
        }} for i in (1, 2, 3)},
    }


def test_same_profile_tags_pass():
    first = snapshot()
    result = audit.compare_known_tags(first, snapshot())
    assert not result['changed_tags']
    assert result['compared_tags'] == 3
    assert result['unverified_tags']
    assert result['all_comparable_tags_unchanged']
    assert not result['complete_known_tag_equality']


def test_detect_change_in_one_profile_only():
    first = snapshot()
    second = snapshot()
    second['profiles']['2']['global_actuation']['raw_hex'] = '00 96'
    second['profiles']['2']['global_actuation']['decoded'] = {'mm': 1.5}
    result = audit.compare_known_tags(first, second)
    assert len(result['changed_tags']) == 1
    assert result['changed_tags'][0]['profile'] == 2
    assert result['changed_tags'][0]['before_hex'] == '00 64'
    assert result['changed_tags'][0]['after_hex'] == '00 96'


def test_reject_incorrect_model():
    bad = snapshot()
    bad['device'] = '0000:0000'
    with pytest.raises(ValueError, match='expected PRO X2'):
        audit.compare_known_tags(bad, snapshot())


def test_reject_missing_slot():
    bad = snapshot()
    bad['profiles'].pop('3')
    with pytest.raises(ValueError, match='all three'):
        audit.compare_known_tags(bad, snapshot())


def test_reject_incomplete_tag_without_marking_changed():
    old = snapshot()
    new = snapshot()
    new['profiles']['2']['global_actuation']['complete'] = False
    result = audit.compare_known_tags(old, new)
    assert result['unverified_tags']
    assert not result['changed_tags']
    assert result['compared_tags'] == 2


def test_no_write_in_hardware_audit(tmp_path, monkeypatch):
    import json
    before = snapshot()
    path = tmp_path / 'before.json'
    path.write_text(json.dumps(before))
    saved = []
    monkeypatch.setattr(audit, 'export_profiles', lambda dev: snapshot())
    monkeypatch.setattr(audit, 'save_private_json', lambda path, data: saved.append((path, data)))
    monkeypatch.setattr(audit.Path, 'home', lambda: tmp_path)
    cmp, afterpath, auditpath = audit.audit_hardware(object(), path)
    assert len(saved) == 2
    assert cmp['all_comparable_tags_unchanged']
    assert afterpath.name.startswith('postwrite-known-tags')
    assert auditpath.name.startswith('postwrite-audit')
