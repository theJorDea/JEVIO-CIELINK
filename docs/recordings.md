# Local two-track training review

For **authorized test calls only**. Obtain explicit permission from both speakers for recording and local review, or use a locally approved synthetic voice for both roles. `--test-voice-consent` records the operator’s attestation, not proof of a speaker’s permission. Production mode refuses import. No microphones, APIs, downloads, transcription or uploads are involved.

## Import an existing call

Place the original, separate caller and agent PCM WAVs in a private `CIELINK_RECORDINGS_DIR` (default `local-data/recordings`). Use the existing session ID from the review list. Offsets are milliseconds from the **same session origin to the first frame of each file**, not the first spoken word; don't double-count silence already present in a track. Provide each offset independently. Synchronized full-length recordings may both start at zero.

```powershell
python -m cielink.recordings test-call-17 --caller test-call-17/caller.wav --caller-start-ms 250 --agent test-call-17/agent.wav --agent-start-ms 3700 --test-voice-consent
```

`--db` overrides `CIELINK_REVIEW_DB`. Missing inputs, invalid WAVs, escaping paths, and conflicting tracks fail the import without registering a partial pair. A successful command prints duration, offset and SHA-256 for each track. This registers files **in place**; restrict write access, don't move/replace them, and keep the review server on the same recording root. A subsequently missing file returns 404 on playback. No audio is substituted. The SQLite `recording_imports` table records consent, source label, UTC attachment time, digest, duration and session state at import; join `track_id` to `tracks.id`. Attaching to a finished call doesn't reopen it, change its timeline events, or re-run analysis.

## Repeatable spoken timeline (optional, Windows PowerShell 5.1)

From the project root, with the project Python environment active, run the block below **only if the installed Windows desktop speech voice is approved for local testing**. It asks for confirmation. It creates a separate private demo DB and a fresh synthetic session per run. Both roles deliberately use the installed default synthetic voice, not an imitation or recording of a real caller. The script places the agent 750 ms after the end of the caller WAV, so different voice speeds do not break alignment. No generated audio should be committed or published.

```powershell
$ErrorActionPreference = 'Stop'
if ((Read-Host 'Both roles use an approved synthetic test voice. Type APPROVE to continue') -cne 'APPROVE') { throw 'Consent not given' }
Add-Type -AssemblyName System.Speech  # requires Windows desktop TTS; no network fallback
$env:CIELINK_MODE = 'training'
$base = Join-Path (Get-Location) 'local-data/synthetic-timeline'
$env:CIELINK_REVIEW_DB = Join-Path $base 'demo.sqlite3'
$env:CIELINK_RECORDINGS_DIR = Join-Path $base 'recordings'
$sid = 'synthetic-' + [guid]::NewGuid().ToString('N')
$dir = Join-Path $env:CIELINK_RECORDINGS_DIR $sid
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$lines = @{caller='Synthetic caller. Can we review my appointment?'; agent='Synthetic agent. Yes, let us review it together.'}
foreach ($role in 'caller','agent') {
    $tts = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $tts.SetOutputToWaveFile((Join-Path $dir "$role.wav"))
        $tts.Speak($lines[$role])
    } finally { $tts.Dispose() }
}
$agentMs = python -c "import sys,wave; w=wave.open(sys.argv[1]); print(250+(1000*w.getnframes()+w.getframerate()-1)//w.getframerate()+750)" (Join-Path $dir 'caller.wav')
if ($LASTEXITCODE -ne 0) { throw 'Cannot read synthesized speech' }
python -c "import sys; from cielink.review import ReviewStore; s=ReviewStore(); i=s.start_session(sys.argv[1]); s.append_event(i,'final',int(sys.argv[2])-750,sys.argv[3]); s.append_event(i,'agent_speech',int(sys.argv[2]),sys.argv[4]); s.finish_session(i,'synthetic_demo')" $sid $agentMs $lines.caller $lines.agent
if ($LASTEXITCODE -ne 0) { throw 'Session creation failed' }
python -m cielink.recordings $sid --caller "$sid/caller.wav" --caller-start-ms 250 --agent "$sid/agent.wav" --agent-start-ms $agentMs --test-voice-consent --synthetic
if ($LASTEXITCODE -ne 0) { throw 'Import failed' }
python -m cielink.review
Write-Output "Local synthetic session: $sid (caller: 250 ms, agent: $agentMs ms)"
```

Start the local review service with the same DB/root settings. Raw playback stays off unless you explicitly set `CIELINK_ALLOW_RAW_RECORDINGS=1` in the local training service's environment before starting it. The caller final event marks the end of its clip; the agent event marks the start of its clip. The demo is a scripted timeline, **not** evidence from an ASR service. On systems without Windows desktop speech, use the existing-call import with two authorized test WAVs instead. Stop the demo service and remove `local-data/synthetic-timeline` when done; do not remove a shared recording root.
