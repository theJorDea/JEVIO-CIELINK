"""Attach a pair of authorized, locally staged training WAVs to a review session."""
import argparse
import hashlib
import json
import os
from pathlib import PureWindowsPath
import sqlite3
import struct
import wave

from .review import ReviewStore, _recording_root, _wav


def _inspect(root, role, name, start):
    if type(start) is not int or not 0 <= start <= 2**53 - 1:
        raise ValueError(f'{role}: start_ms must be a nonnegative timeline integer')
    # Reject Windows drive, UNC, device and alternate-stream syntax on all hosts.
    win = PureWindowsPath(name)
    if (win.anchor or '..' in win.parts or ':' in str(name)
            or (os.path.isreserved(name) if hasattr(os.path, 'isreserved') else win.is_reserved())
            or any(p.endswith((' ', '.')) for p in win.parts)):
        raise ValueError(f'{role}: expected a relative WAV path without traversal')
    path = _wav(root, name)
    if not path.is_file():
        raise ValueError(f'{role}: recording missing (expected a regular WAV file)')
    with path.open('rb') as src:
        try:
            with wave.open(src, 'rb') as wav:
                n, hz = wav.getnframes(), wav.getframerate()
                frame_size = wav.getnchannels() * wav.getsampwidth()
                if not n or not hz or wav.getcomptype() != 'NONE' or wav.getsampwidth() > 4:
                    raise ValueError(f'{role}: expected nonempty PCM WAV')
                remaining = n
                while remaining:
                    count = min(remaining, max(1, 65536 // frame_size))
                    if len(wav.readframes(count)) != count * frame_size:
                        raise ValueError(f'{role}: truncated WAV')
                    remaining -= count
        except (EOFError, wave.Error, struct.error) as exc:
            raise ValueError(f'{role}: invalid PCM WAV') from exc
        src.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: src.read(65536), b''):
            digest.update(block)
    return path, {'role': role, 'start_ms': start, 'duration_ms': (1000*n + hz-1)//hz,
                  'sha256': digest.hexdigest()}


def import_recordings(store, session_id, *, caller, caller_start_ms, agent, agent_start_ms,
                      test_voice_consent=False, synthetic=False):
    """Register both files or neither. Does not reopen the session or modify its events."""
    if test_voice_consent is not True or os.getenv('CIELINK_MODE', 'training') != 'training':
        raise ValueError('Training mode and explicit authorized test voice consent are required')
    root = _recording_root()
    pair = [_inspect(root, 'caller', caller, caller_start_ms),
            _inspect(root, 'agent', agent, agent_start_ms)]
    if pair[0][0].samefile(pair[1][0]):
        raise ValueError('Expected separate caller and agent WAV files')
    with store._connect() as c:
        c.execute('BEGIN IMMEDIATE')
        session = c.execute('SELECT status FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not session or session['status'] not in ('active', 'finished'):
            raise ValueError('Active or finished session required; session missing or unsupported')
        if c.execute("SELECT 1 FROM tracks WHERE session_id=? AND role IN ('caller','customer','agent')",
                     (session_id,)).fetchone():
            raise ValueError('Session already has caller or agent audio; not replacing evidence')
        c.execute('''CREATE TABLE IF NOT EXISTS recording_imports (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id), session_status TEXT NOT NULL,
            imported_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, test_voice_consent INTEGER NOT NULL,
            source TEXT NOT NULL, duration_ms INTEGER NOT NULL, sha256 TEXT NOT NULL)''')
        for path, track in pair:
            tid = c.execute('INSERT INTO tracks(session_id, role, path, start_ms) VALUES (?,?,?,?)',
                            (session_id, track['role'], str(path.relative_to(root)), track['start_ms'])).lastrowid
            c.execute('''INSERT INTO recording_imports
                (track_id, session_status, test_voice_consent, source, duration_ms, sha256)
                VALUES (?,?,1,?,?,?)''', (tid, session['status'], 'approved-synthetic' if synthetic else
                                          'authorized-test-recording', track['duration_ms'], track['sha256']))
    return [track for _, track in pair]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session_id')
    parser.add_argument('--db', help='Defaults to CIELINK_REVIEW_DB')
    for role in ('caller', 'agent'):
        parser.add_argument('--' + role, required=True, help='WAV relative to CIELINK_RECORDINGS_DIR')
        parser.add_argument('--' + role + '-start-ms', required=True, type=int)
    parser.add_argument('--test-voice-consent', required=True, action='store_true',
                        help='I confirm both voices are explicitly authorized for local test review')
    parser.add_argument('--synthetic', action='store_true', help='Label BOTH voices as approved synthetic speech')
    args = vars(parser.parse_args(argv))
    try:
        tracks = import_recordings(ReviewStore(args.pop('db')), **args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Import failed: {exc}\n')
    print(json.dumps(tracks, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
