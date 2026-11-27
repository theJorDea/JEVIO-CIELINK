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

`Answer()` → default `CIELINK_RESULT=OPERATOR` → `AGI(cielink-agi)` (no arguments). `AGISIGHUP=no` lets the worker observe dead-channel responses and finalize review instead of dying on SIGHUP. `DONE` with `AGISTATUS=SUCCESS` ends the call. Confirmed handoff returns `OPERATOR`; startup, speech, DB, or AGI failures leave the default route. A dead caller cannot be transferred. Fallback itself creates no CRM action. A receipt already committed before a later failure/hangup remains valid; check review before taking another action.

Python: per call `SessionContext(channel='sip', session_id=...)`, `handle_input(state, FinalInput(...)).state`, `await ConfirmedDialogue(Consultation(FixtureQuoteAdapter(), 'sequential'), store).respond(state, text)`. The **same dialogue instance** receives `delivered()` only when [STREAM FILE](https://docs.asterisk.org/Latest_API/API_Documentation/AGI_Commands/stream_file/) returns `0`. This means Asterisk completed transmission, not proof the caller understood it. AGI does not report a precise audible-start timestamp. Review stores final transcripts, generated replies, delivery/interruption and final outcome/receipt, not raw tracks. Protect both SQLite files and remove lab data after testing.

Uses [RECORD FILE](https://docs.asterisk.org/Latest_API/API_Documentation/AGI_Commands/record_file/) rather than fd 3; ordinary AGI is preferred, but the worker also accepts local `EAGI(cielink-agi)` on a compatible answered channel. No duplex, partials, barge-in, DTMF consent or streaming ASR. Wait for the prompt to end before speaking. Records 8 kHz mono WAV until two seconds of silence or a 12-second limit; SoX resamples to 16 kHz for offline recognition. Silence, suspected truncation or 20 unsuccessful turns fall back rather than inferring consent. Models load per call; benchmark latency and memory locally. ASR errors remain possible, including on affirmative phrases. Private temporary audio is removed on normal exits; check stale `/tmp/cielink-*` directories after a hard kill, only removing those belonging to inactive calls.

## Verify before routing callers

* From the checkout run `python -m pytest -q tests/test_asterisk.py`. These protocol tests do not verify codecs, model quality, PBX routing or sound on a handset.
* On the isolated test phone, call 7616, say «Учебная улица дом восемнадцать корпус один», wait for the entire offer, then «да подключайте». Expect a spoken receipt and exactly one `order` in the lab CRM; inspect review for a completed offer before the affirmative final transcript.
* Repeat with «нет», then hang up mid-offer on another call: no new CRM operation. Stay silent on another call: the existing operator route should answer. Say «хочу оператора», then «да передайте специалисту» after the question: expect one handoff receipt and operator routing.
* Temporarily point the **lab wrapper** at a missing model and call again. Verify operator fallback even without a worker result. Restore the model; no outbound call should have been originated by the adapter. Configure the existing review tool to use `/var/lib/cielink-lab/review.sqlite3` to inspect finished sessions; startup failures before a session begins have no review row.
