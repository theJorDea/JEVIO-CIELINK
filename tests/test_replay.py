"""Acceptance harness: actual websocket, HTTP review API and SQLite CRM."""
import json
from cielink import replay
from cielink.replay import CORPUS, MODES, run


def test_replay_corpus_passes_all_web_modes():
    report = run()
    assert set(report['aggregate']) == set(MODES)
    for mode, aggregate in report['aggregate'].items():
        assert aggregate['calls'] == 10
        assert aggregate['passed'] == aggregate['calls']
        assert aggregate['acoustic_latency_ms'] is None
        assert aggregate['mobile_latency_ms'] is None
        assert aggregate['local_final_to_reply_p50_ms'] is not None
        assert aggregate['local_final_to_reply_p95_ms'] is not None
        assert all(not item['failures'] for item in report['results'][mode])


def test_fixture_names_cover_required_acceptance_edges():
    fixture = json.loads(CORPUS.read_text(encoding='utf-8'))
    assert {item['id'] for item in fixture} == {
        'order', 'unknown', 'decline', 'think_it_over', 'ack', 'duplicate', 'error', 'changed',
        'followup_correction', 'no_router'
    }


def test_demo_retains_review_card_from_the_same_call(tmp_path, monkeypatch):
    import sys
    from cielink.review import ReviewStore

    directory = tmp_path / 'demo'
    output = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['replay', '--demo-dir', str(directory), '--output', str(output)])
    assert replay.main() == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    card = ReviewStore(directory / 'review.db').detail(report['call']['session_id'])
    assert report['card'] == card
    assert card['receipt']['operation_type'] == 'coverage_request'
    assert card['receipt']['confirmed_terms']['address']['corpus'] == '2'
    assert card['tracks'] == []  # text replay has no acoustic evidence


def test_wrong_expected_house_is_reported(tmp_path, monkeypatch):
    case = json.loads(CORPUS.read_text(encoding='utf-8'))[0]
    case['house'] = 'not-the-confirmed-house'
    corpus = tmp_path / 'bad-expectation.json'
    corpus.write_text(json.dumps([case]), encoding='utf-8')
    monkeypatch.setattr(replay, 'CORPUS', corpus)
    monkeypatch.setattr(replay, 'MODES', ('sequential',))
    report = run()
    assert report['aggregate']['sequential']['passed'] == 0
    assert 'wrong confirmed house' in report['results']['sequential'][0]['failures']
