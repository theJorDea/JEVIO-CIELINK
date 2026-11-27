# Offline web acceptance replay (#15)

From the repository root on Linux, use a native Python environment (do not reuse a Windows `.venv`). With `uv` and a supported Python installed, prepare the dependencies once with `uv sync --locked --group dev` while online, or add `--offline` if the Linux wheels are already cached. This replay uses only `uv.lock`: Node, npm, Vite, and the dashboard lockfile are not involved. Then run offline:

```sh
uv run --offline --locked python -m cielink.replay --output replay.json
uv run --offline --locked python -m pytest -q tests/test_replay.py
```

Alternatively use the existing virtual environment's Python. Omit `--output` for JSON on stdout. Exit 0 means every case passed in every mode; otherwise inspect `results[mode][].failures`. Run as a standalone process: it temporarily sets training configuration. For each mode the runner starts Uvicorn on a free IPv4 loopback port, opens a real `/ws/voice` connection per case, selects text input/display, and deletes both temporary SQLite databases after shutdown. No microphone, remote provider, persistent CRM, credentials or running service required.

## Acceptance oracle

[The corpus](../fixtures/replay-scenarios.json) supplies the same ordered turns to all modes. `partials` precede the final; `display: false` withholds delivery evidence. `receipt: true` marks turns that must return the **committed** receipt number. The runner counts session CRM rows after every turn and compares the finished `GET /api/sessions/{id}` transcript, coverage, outcome and receipt with the fixture and persisted operation. It compares the house, offer and monthly budget rather than treating a reassuring reply as proof of a sale.

| Case | Expected result |
|---|---|
| order | Stable partials resolve to K1; delivered offer and consent produce one K1 order. |
| unknown | K2 produces one coverage request, not an order. |
| decline | Explicit refusal after the offer ends without a write. |
| think_it_over | After a delivered K1 offer, «я подумаю» closes the call as `no_action`; even a later «да подключайте» cannot revive the proposal. No CRM row or receipt. |
| ack | Explicit words before delivery, then “угу” after delivery, never write. Hanging up leaves `disconnected`, not an explicit refusal. |
| duplicate | Repeating the confirmation in the same session returns the original number, one row. |
| error | Fixture coverage error and consent produce a handoff, not an unavailable house or a sale. |
| changed | A delivered K1 offer, misleading partials and a K2 correction invalidate the old order scope. Only the K2 check is saved; the 2200 monthly limit survives. |

## Reading the benchmark

`local_final_to_reply_ms` is measured by the client from sending a final to receiving the complete reply frame, including loopback transport and server work. `local_server_read_ms` pairs server trace `start` and `ready` offsets by revision and operation (integer ms; a real read can round to zero). Neither measure uses the synthetic transcript offsets. `unused_reads` counts started tool reads not consumed by any final in the recorded trace; cancellation is not required for a read to be unused. The trace is bounded in the application; these short scenarios fit. Summaries use the median and nearest-rank p95 over measured final turns; failed cases remain in the call denominator. A transport exception before case completion contributes no timings.

Sample `aggregate` object from one local run, not a performance target:

```json
{
  "sequential": {"calls":8,"passed":8,"measured_turns":21,"read_starts":15,"unused_reads":0,"local_final_to_reply_p50_ms":14.827,"local_final_to_reply_p95_ms":20.025,"acoustic_latency_ms":null,"mobile_latency_ms":null},
  "early": {"calls":8,"passed":8,"measured_turns":21,"read_starts":15,"unused_reads":0,"local_final_to_reply_p50_ms":13.27,"local_final_to_reply_p95_ms":17.6,"acoustic_latency_ms":null,"mobile_latency_ms":null},
  "speculative": {"calls":8,"passed":8,"measured_turns":21,"read_starts":17,"unused_reads":2,"local_final_to_reply_p50_ms":14.831,"local_final_to_reply_p95_ms":18.39,"acoustic_latency_ms":null,"mobile_latency_ms":null}
}
```

The modes are the web adapter's `sequential`, `early` (prepares address resolution), and `speculative` (also starts coverage/quote reads). They are not LiveKit A/B/C. The replay pins Uvicorn to asyncio on both Linux and Windows. At 21 turns per mode, nearest-rank p95 is the second-slowest observed turn. These numbers do not establish a speedup, acoustic interruption time or mobile latency.

## Business and evidence gaps

- `text_displayed` acknowledges a test client event, not hearing or understanding prices. The rules accept bare “да” after delivery; whether that suffices for each operation needs a business decision.
- A handoff means a saved training queue record, not a connected or available specialist. The duplicate case covers repeated consent in one session, not reconnection or cross-session deduplication.
- A fixture `error` is a returned coverage status, not a real provider timeout. Late provider completion and failure after CRM commit require separate fault-injection tests. Actual acoustics, SIP and mobile behavior need the [separate validation route](validation.md).
