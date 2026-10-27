from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from logi_ghub.features import modular_flash_experiment as fw


@pytest.mark.parametrize('mm,raw', [('0.10', b'\x00\x0a'), ('1.50', b'\x00\x96'), ('4.00', b'\x01\x90')])
def test_encode_good(mm, raw):
    assert fw.encode_actuation(mm) == raw


@pytest.mark.parametrize('mm', ['0.00', '4.05', '1.51', 'NaN', 'Infinity', 'abc', '2.001', '-1'])
def test_encode_invalid(mm):
    with pytest.raises(ValueError):
        fw.encode_actuation(mm)


def fake_dev():
    h = SimpleNamespace(long_only=False, call=Mock(return_value=b'\x00' * 16))
    return SimpleNamespace(key='046d:c364', info=SimpleNamespace(vid=0x046d, pid=0xc364),
                           hidpp=h, has=lambda f: f == 0x8102)


def test_preview_refuses_wrong_hardware():
    dev = fake_dev()
    dev.info.pid = 0x1234
    with pytest.raises(fw.ExperimentalWriteError):
        fw.preview(dev, 1, '1.50')


def test_preview_refuses_inactive(monkeypatch):
    dev = fake_dev()
    monkeypatch.setattr(fw, 'export_profiles',
                        lambda d: {'active_before': 1, 'active_after': 1})
    with pytest.raises(fw.ExperimentalWriteError, match='physically select'):
        fw.preview(dev, 2, '1.50')
    dev.hidpp.call.assert_not_called()


def test_preview_uses_only_read_and_saves_snapshot(monkeypatch):
    dev = fake_dev()
    saved = []
    monkeypatch.setattr(fw, 'export_profiles', lambda d: {
        'active_before': 2, 'active_after': 2,
        'profiles': {str(n): {'global_actuation': {
            'status': 'ok', 'complete': True, 'actual_length': 2, 'raw_hex': '00 64'
        }} for n in (1, 2, 3)},
    })
    monkeypatch.setattr(fw, 'save_private_json', lambda p, r: saved.append((p, r)))
    result = fw.preview(dev, 2, '1.50')
    assert result['old_mm'] == 1.0 and result['new_mm'] == 1.5
    assert result['unchanged'] is False and len(saved) == 1
    dev.hidpp.call.assert_not_called()


def test_writer_fn9_and_fn11_only_crc():
    h = Mock()
    h.call.side_effect = [b'\x03' + b'\x00' * 15, b'\x00' + b'\x00' * 15]
    result = fw._commit_single_tag(h, b'\x00\x96')
    assert result['commit_reply_hex'].startswith('00')
    assert h.call.call_args_list[0].args == (0x8102, 9, b'\x01\x20\x00\x02\x00\x96')
    assert h.call.call_args_list[1].args[0:2] == (0x8102, 11)
    assert len(h.call.call_args_list[1].args[2]) == 4


def test_writer_rejects_invalid_commit():
    h = Mock()
    h.call.side_effect = [b'\x03' + b'\x00' * 15, b'\x01' + b'\x00' * 15]
    with pytest.raises(fw.ExperimentalWriteError, match='unknown'):
        fw._commit_single_tag(h, b'\x00\x96')


def test_no_change_skips_everything():
    dev = fake_dev()
    result = fw.apply_experimental(dev, {'unchanged': True})
    assert result['status'] == 'unchanged'
    dev.hidpp.call.assert_not_called()


def test_wrong_confirmation_never_writes():
    dev = fake_dev()
    plan = {'unchanged': False, 'slot': 2, 'new_mm': 1.5, 'old_raw_hex': '00 64',
            'new_raw_hex': '00 96', 'snapshot_path': 'example.json'}
    with pytest.raises(fw.ExperimentalWriteError, match='cancelled'):
        fw.apply_experimental(dev, plan, input_func=lambda x: 'no',
                              output_func=lambda x: None)
    dev.hidpp.call.assert_not_called()


def test_apply_success_with_three_profile_readback(monkeypatch):
    dev = fake_dev()
    dev.hidpp.call.side_effect = [
        b'\x02' + b'\x00' * 15, b'\x03' + b'\x00' * 15,
        b'\x00' + b'\x00' * 15, b'\x02' + b'\x00' * 15,
    ]

    def read_tag(_h, _budget, slot, _tag):
        calls = read_tag.calls
        read_tag.calls += 1
        return {'complete': True, 'raw_hex': '00 64' if calls == 0 or slot != 2 else '00 96'}

    read_tag.calls = 0
    monkeypatch.setattr(fw, 'read_tag', read_tag)
    plan = {'unchanged': False, 'slot': 2, 'new_mm': 1.5, 'old_raw_hex': '00 64',
            'new_raw_hex': '00 96', 'snapshot_path': 'known-tags.json',
            'baseline_all_slots': {str(i): '00 64' for i in (1, 2, 3)}}
    result = fw.apply_experimental(
        dev, plan, input_func=lambda q: 'P2 1.50 WRITE FLASH', output_func=lambda s: None,
    )
    assert result['status'] == 'readback_ok_not_yet_powercycle_verified'
    assert [c.args[1] for c in dev.hidpp.call.call_args_list] == [2, 9, 11, 2]
    assert dev.hidpp.long_only is False


def test_apply_refuses_active_mismatch_before_flash(monkeypatch):
    dev = fake_dev()
    dev.hidpp.call.return_value = b'\x01' + b'\x00' * 15
    plan = {'unchanged': False, 'slot': 2, 'new_mm': 1.5, 'old_raw_hex': '00 64',
            'new_raw_hex': '00 96', 'snapshot_path': 'known-tags.json',
            'baseline_all_slots': {str(i): '00 64' for i in (1, 2, 3)}}
    with pytest.raises(fw.ExperimentalWriteError, match='active profile changed'):
        fw.apply_experimental(
            dev, plan, input_func=lambda q: 'P2 1.50 WRITE FLASH', output_func=lambda s: None,
        )
    assert [c.args[1] for c in dev.hidpp.call.call_args_list] == [2]
    assert dev.hidpp.long_only is False
