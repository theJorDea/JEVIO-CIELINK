# Inbound Asterisk lab extension

Opt-in, single-host Linux example. No SIP account, trunk, listener, outbound dial or live CRM configuration is included. Use only consenting lab callers and the fixture catalog. The script runs as the local Asterisk user; both processes must see the same `/tmp`. Keep it outside production contexts.

## Install on an isolated PBX

1. Stage this checkout (including fixtures and SQL) at `/opt/cielink`, read-only for Asterisk. Install Python 3.11+, SoX with WAV support, espeak-ng with Russian voice data, and Asterisk with `res_agi` and `format_wav`. Example Python setup as the lab administrator:

   ```sh
   cd /opt/cielink
   python3 -m venv .venv
   .venv/bin/pip install 'fastapi>=0.115,<1' 'pydantic>=2.10,<3' vosk
   ```

2. Unpack a compatible Russian [Vosk model](https://alphacephei.com/vosk/models) in `/opt/cielink/models/vosk-ru` (model root, not its parent). No model downloads during calls. Default speech is `espeak-ng -v ru`; optionally install the `piper-tts` CLI in the venv, stage a Russian `.onnx` voice and matching `.onnx.json` sidecar, and export `CIELINK_PIPER_MODEL` with its absolute `.onnx` path in the wrapper. Ensure models are readable; check each model's license.
3. Check the actual AGI directory (`asterisk -rx 'core show settings'`). Example for a PBX running as `asterisk:asterisk`:

   ```sh
   install -d -o asterisk -g asterisk -m 0700 /var/lib/cielink-lab
   install -m 0755 telephony/cielink-agi /var/lib/asterisk/agi-bin/cielink-agi
   ```

4. In a copy of `extensions-lab.conf`, replace `existing-operator,s,1` with an **existing, tested local operator destination** (not this extension). Include that copy in the **lab** dialplan and make extension 7616 reachable only from a test phone's inbound context. Verify there is no numbering collision. Reload the lab dialplan and check `dialplan show 7616@cielink-lab`. Remove the include and reload to roll back. No service changes are made by the repository.

## Integration contract and limits

`Answer()` → default `CIELINK_RESULT=OPERATOR` → `Set(EAGI_AUDIO_FORMAT=slin)` → `EAGI(cielink-agi)` (no arguments). For ordinary AGI, remove the format-setting line and replace `EAGI(cielink-agi)` with `AGI(cielink-agi)`; this keeps prompt-then-record turns but disables voice interruption. This is a dialplan choice, not a retry of a failed call. `AGISIGHUP=no` lets the worker observe dead-channel responses and finalize review instead of dying on SIGHUP. `DONE` with `AGISTATUS=SUCCESS` ends the call. Confirmed handoff returns `OPERATOR`; startup, speech, DB, or AGI failures leave the default route. A dead caller cannot be transferred. Fallback itself creates no CRM action. A receipt already committed before a later failure/hangup remains valid; check review before taking another action.

Python: per call `SessionContext(channel='sip', session_id=...)`, `handle_input(state, FinalInput(...)).state`, `await ConfirmedDialogue(Consultation(FixtureQuoteAdapter(), 'sequential'), store).respond(state, text)`. The **same dialogue instance** receives `delivered()` only after the entire reply finishes without interruption (not after each successful segment). [STREAM FILE](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AGI_Commands/stream_file/) returns `0` on uninterrupted completion. This means Asterisk completed transmission, not proof the caller understood it. AGI does not report a precise audible-start timestamp. Review stores final transcripts, generated replies, delivery/interruption and final outcome/receipt, not raw tracks. Protect both SQLite files and remove lab data after testing.

**Interruptible prompts.** Local [EAGI](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/EAGI/) supplies incoming audio on file descriptor 3. The detector expects Linux `slin`: raw mono 8 kHz signed 16-bit linear PCM in native byte order. It plays interruptible prompts with successive 200 ms `STREAM FILE` segments and checks fd 3 between segments. Three consecutive 20 ms frames (160 samples each) with RMS ≥ 500 trigger a stop before the next segment. The worker plays the noninterruptible prompt «После сигнала повторите фразу с самого начала.» It then issues a fresh [RECORD FILE](https://docs.asterisk.org/Latest_API/API_Documentation/AGI_Commands/record_file/) with the `BEEP` option: the tone cues the caller to repeat the entire phrase for the new recording. The overlapping words are discarded, not transcribed or counted as consent. Receipt and final announcements also play without voice interruption.

This is an energy check at segment boundaries, not continuous full-duplex recognition. Noise or speakerphone echo can trigger it; quiet or brief speech may not. Scheduling and audio buffering affect response time; 200 ms is a segment length, not a latency guarantee. There is no streaming ASR, partial transcript or DTMF consent. Check the actual PBX build and channel: Asterisk 22's [`res/res_agi.c`](https://github.com/asterisk/asterisk/blob/22/res/res_agi.c) implements EAGI and `STREAM FILE`; [`main/file.c`](https://github.com/asterisk/asterisk/blob/22/main/file.c) handles playback waits and forwarding incoming voice to the audio fd. In ordinary AGI there is no fd 3 audio feed; wait for each prompt to end before speaking.

**Recording and storage.** Records 8 kHz mono WAV until two seconds of silence or a 12-second limit; SoX resamples to 16 kHz for offline recognition. Silence, suspected truncation or 20 unsuccessful turns fall back rather than inferring consent. Models load per call; benchmark latency and memory locally. ASR errors remain possible, including on affirmative phrases. Private temporary audio is removed on normal exits; check stale `/tmp/cielink-*` directories after a hard kill, only removing those belonging to inactive calls.

## Verify before routing callers

* From the checkout run `python -m pytest -q tests/test_asterisk.py`. These protocol tests do not verify codecs, model quality, PBX routing or sound on a handset.
* On the isolated test phone, call 7616, say «Учебная улица дом восемнадцать корпус один», wait for the entire offer, then «да подключайте». Expect a spoken receipt and exactly one `order` in the lab CRM; inspect review for a completed offer before the affirmative final transcript.
* With EAGI enabled, speak «да подключайте» clearly over the middle of the offer. Expect playback to stop and a request to repeat. Talk briefly over that request: it should finish and be followed by the recording beep. Stay silent after the beep; expect no order and no affirmative transcript from the overlap. On a separate successful call, speak over the receipt: it should finish without another repeat request.
* Switch the isolated extension to ordinary AGI as described above, reload and check `dialplan show 7616@cielink-lab`. Speech over the offer should not interrupt it. Give a new answer after playback and verify normal recording and routing. Restore EAGI and the format setting, then reload.
* Repeat with «нет», then hang up mid-offer on another call: no new CRM operation. Stay silent on another call: the existing operator route should answer. Say «хочу оператора», then «да передайте специалисту» after the question: expect one handoff receipt and operator routing.
* Temporarily point the **lab wrapper** at a missing model and call again. Verify operator fallback even without a worker result. Restore the model; no outbound call should have been originated by the adapter. Configure the existing review tool to use `/var/lib/cielink-lab/review.sqlite3` to inspect finished sessions; startup failures before a session begins have no review row.
