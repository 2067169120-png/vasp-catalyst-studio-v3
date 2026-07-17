"""campaign.ledger 测试:append-only 决策/事件、过滤、sanitize 拦密钥。"""
import pytest

from vcstudio.campaign import ledger


def test_record_decision_returns_id_and_persists(tmp_path):
    did = ledger.record_decision(str(tmp_path), 'auto-default',
                                 {'encut': 500}, 'auto', context={'task': 't1'})
    assert did.startswith('dec-')
    rows = ledger.read_decisions(str(tmp_path))
    assert len(rows) == 1
    assert rows[0]['id'] == did
    assert rows[0]['made_by'] == 'auto'
    assert ledger.find_decision(str(tmp_path), did)['detail'] == {'encut': 500}


def test_decision_filter_by_kind_and_made_by(tmp_path):
    ledger.record_decision(str(tmp_path), 'sign-off', 'ok', 'user')
    ledger.record_decision(str(tmp_path), 'proposal', 'try X', 'ai-proposal')
    ledger.record_decision(str(tmp_path), 'sign-off', 'ok2', 'user')
    assert len(ledger.read_decisions(str(tmp_path), kind='sign-off')) == 2
    assert len(ledger.read_decisions(str(tmp_path), made_by='ai-proposal')) == 1


def test_made_by_must_be_valid(tmp_path):
    with pytest.raises(ValueError, match='made_by 非法'):
        ledger.record_decision(str(tmp_path), 'k', 'd', 'robot')


def test_events_round_trip_and_filter(tmp_path):
    ledger.record_event(str(tmp_path), 'submitted', 't1', {'cluster': 'hpcA'})
    ledger.record_event(str(tmp_path), 'validated', 't1')
    ledger.record_event(str(tmp_path), 'submitted', 't2')
    assert len(ledger.read_events(str(tmp_path), task_id='t1')) == 2
    assert len(ledger.read_events(str(tmp_path), kind='submitted')) == 2


def test_sanitize_blocks_secrets(tmp_path):
    for payload in ({'password': 'hunter2'},
                    {'note': 'export GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUV12345'},
                    {'api_key': 'x'},
                    {'creds': 'aws AKIAIOSFODNN7EXAMPLE'}):
        with pytest.raises(ValueError, match='敏感信息'):
            ledger.record_decision(str(tmp_path), 'k', payload, 'user')
    # 干净内容照常写入,且未被上面的失败污染
    ledger.record_decision(str(tmp_path), 'k', {'note': '一切正常'}, 'user')
    assert len(ledger.read_decisions(str(tmp_path))) == 1


def test_sanitize_scans_nested_and_keys(tmp_path):
    with pytest.raises(ValueError, match='敏感信息'):
        ledger.record_event(str(tmp_path), 'evt', 't', {'deep': [{'secret_value': 'zzz'}]})
    # 键名命中也拦(password 作为 key)
    with pytest.raises(ValueError, match='敏感信息'):
        ledger.record_event(str(tmp_path), 'evt', 't', {'password': 'anything'})


def test_is_sensitive_direct():
    assert ledger.is_sensitive('my password is x')
    assert ledger.is_sensitive('ghp_ABCDEFGHIJKLMNOPQRST0000') is not None
    assert ledger.is_sensitive('普通中文描述') is None


def test_read_missing_files_empty(tmp_path):
    assert ledger.read_decisions(str(tmp_path)) == []
    assert ledger.read_events(str(tmp_path)) == []
    assert ledger.find_decision(str(tmp_path), 'dec-x') is None


def test_malformed_line_skipped(tmp_path):
    ledger.record_event(str(tmp_path), 'ok', 't1')
    with open(tmp_path / ledger.EVENTS_NAME, 'a', encoding='utf-8') as f:
        f.write('{ this is not json\n')
    ledger.record_event(str(tmp_path), 'ok2', 't2')
    rows = ledger.read_events(str(tmp_path))
    assert [r['kind'] for r in rows] == ['ok', 'ok2']
