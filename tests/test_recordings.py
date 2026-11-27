import hashlib
from pathlib import Path
import shutil
from tempfile import gettempdir
from uuid import uuid4
import wave

import pytest

from cielink.recordings import import_recordings, main
from cielink.review import ReviewStore


@pytest.fixture
def setup(monkeypatch):
    # Tiny PCM containers exercise the parser; these are not demo voice recordings.
    root = Path(gettempdir()) / ('wav-test-' + uuid4().hex)
    root.mkdir()
    try:
        monkeypatch.setenv('CIELINK_RECORDINGS_DIR', str(root))
        monkeypatch.setenv('CIELINK_REVIEW_DB', str(root / 'review.db'))
        monkeypatch.setenv('CIELINK_MODE', 'training')
        for role in ('caller', 'agent'):
            with wave.open(str(root / f'{role}.wav'), 'wb') as w:
                w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                w.writeframes(bytes(320))
        store = ReviewStore()
        store.start_session('test')
        yield root, store, dict(caller='caller.wav', agent='agent.wav', caller_start_ms=52,
                                agent_start_ms=370, test_voice_consent=True)
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize('finished', [False, True])
def test_import_preserves_session_and_audits_each_track(setup, finished):
    root, store, args = setup
    store.append_event('test', 'final', 62, 'test transcript')
    if finished:
        store.finish_session('test', 'done')
        store.analyze_next()
    before = store.detail('test')
    result = import_recordings(store, 'test', synthetic=True, **args)
    detail = store.detail('test')
    assert [(t['role'], t['start_ms']) for t in detail.pop('tracks')] == [('caller', 52), ('agent', 370)]
    assert before.pop('tracks') == [] and before == detail
    with store._connect() as c:
        audits = c.execute('SELECT * FROM recording_imports ORDER BY track_id').fetchall()
    for track, audit in zip(result, audits, strict=True):
        path = root / (track['role'] + '.wav')
        assert store.track_path('test', audit['track_id']) == path
        assert audit['imported_utc'] and audit['test_voice_consent'] == 1
        assert audit['session_status'] == before['status'] and audit['source'] == 'approved-synthetic'
        assert audit['duration_ms'] == 10 and audit['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='already has'):
        import_recordings(store, 'test', **args)
    assert len(store.detail('test')['tracks']) == 2


@pytest.mark.parametrize(('key', 'bad'), [('agent', '../outside.wav'), ('agent', r'..\outside.wav'),
    ('agent', r'C:\secret.wav'), ('agent', 'file.wav:stream.wav'), ('agent', 'caller.wav'),
    ('agent', 'notes.txt'), ('agent_start_ms', -1), ('caller_start_ms', 1.5),
    ('test_voice_consent', False), ('test_voice_consent', 'true')])
def test_reject_invalid_without_partial_pair(setup, key, bad):
    _, store, args = setup
    args[key] = bad
    with pytest.raises(ValueError):
        import_recordings(store, 'test', **args)
    assert store.detail('test')['tracks'] == []


@pytest.mark.parametrize('kind', ['invalid', 'truncated', 'missing'])
def test_bad_audio_not_linked(setup, kind):
    root, store, args = setup
    path = root / 'agent.wav'
    if kind == 'missing':
        path.unlink()
    else:
        path.write_bytes(path.read_bytes()[:-1] if kind == 'truncated' else b'not a recording')
    with pytest.raises(ValueError, match='agent: .*' + kind):
        import_recordings(store, 'test', **args)
    assert store.detail('test')['tracks'] == []


def test_cli_notifies_missing_and_requires_consent(setup, capsys):
    _, store, _ = setup
    argv = ['test', '--caller', 'caller.wav', '--agent', 'absent.wav', '--caller-start-ms', '0', '--agent-start-ms', '90']
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        main(argv + ['--test-voice-consent'])
    assert e.value.code == 1 and 'agent: recording missing' in capsys.readouterr().err
    assert store.detail('test')['tracks'] == []


def test_no_implicit_session_or_production_import(setup, monkeypatch):
    _, store, args = setup
    with pytest.raises(ValueError, match='session missing'):
        import_recordings(store, 'unknown', **args)
    assert store.detail('unknown') is None
    monkeypatch.setenv('CIELINK_MODE', 'production')
    with pytest.raises(ValueError, match='Training mode'):
        import_recordings(store, 'test', **args)
