import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const asset = name => readFileSync(new URL(`../cielink/static/voice.${name}`, import.meta.url), 'utf8');
function client({ stt = 'browser', tts = 'browser' } = {}) {
  const ui = Object.fromEntries(Array.from(asset('html').matchAll(/id="(\w+)"/g), ([, id]) =>
    [id, { textContent: '', append(s) { this.textContent += s; } }]));
  let ws, mic, utterance;
  const sent = [];
  class Socket {
    static OPEN = 1;
    readyState = 1;
    constructor(url) { assert.equal(url, 'wss://example.test/ws/voice'); ws = this; }
    send(json) { sent.push(JSON.parse(json)); }
    close() { this.readyState = 3; }
  }
  const window = {
    webkitSpeechRecognition: class { start() { mic = this; } abort() { this.aborted = true; } },
    SpeechSynthesisUtterance: class {},
    speechSynthesis: { speak(u) { utterance = u; }, cancel() { utterance.onend({ type: 'end' }); } },
    addEventListener() {},
  };
  vm.runInNewContext(asset('js'), { document: { getElementById: id => ui[id] }, performance,
    window, ...window, WebSocket: Socket, location: { protocol: 'https:', host: 'example.test' } });
  ui.connect.onclick();
  const receive = msg => ws.onmessage({ data: JSON.stringify(msg) });
  receive({ type: 'ready', session_id: 'test', config: { stt, tts, llm: 'rules' } });
  return { ui, sent, receive, ws, get mic() { return mic; }, get speech() { return utterance; } };
}

test('speech barge-in reports exactly one interruption and ignores obsolete callbacks', () => {
  const c = client();
  c.ui.mic.onclick();
  assert.equal(c.mic.lang, 'ru-RU');
  assert.equal(c.mic.interimResults, true);
  c.receive({ type: 'reply', reply_id: 'a', text: 'Первый' });
  const first = c.speech;
  assert.equal(first.lang, 'ru-RU');
  first.onstart();
  c.mic.onspeechstart();
  const result = [{ transcript: 'дом' }];
  c.mic.onresult({ resultIndex: 0, results: [result] });
  result.isFinal = true;
  c.mic.onresult({ resultIndex: 0, results: [result] });
  c.receive({ type: 'reply', reply_id: 'b', text: 'Второй' });
  first.onstart(); first.onend({ type: 'end' });
  c.speech.onstart(); c.speech.onend({ type: 'end' });
  assert.deepEqual(c.sent.map(m => [m.type, m.reply_id ?? m.text]), [
    ['playback_started', 'a'], ['playback_interrupted', 'a'], ['partial', 'дом'], ['final', 'дом'],
    ['playback_started', 'b'], ['playback_ended', 'b'],
  ]);
  assert.ok(c.sent.every(m => Number.isInteger(m.offset_ms) && m.offset_ms >= 0));
  c.ui.disconnect.onclick();
  assert.ok(c.mic.aborted);
  assert.equal(c.ws.readyState, 3);
  c.mic.onresult({ resultIndex: 0, results: [result] });
  assert.equal(c.sent.length, 6);
});

test('text mode supports manual input, literal replies and server timing details', () => {
  const c = client({ stt: 'text', tts: 'text' });
  assert.equal(c.ui.mic.disabled, true);
  c.ui.text.value = ' тест ';
  c.ui.partial.onclick(); c.ui.form.onsubmit({ preventDefault() {} });
  assert.deepEqual(c.sent.map(m => [m.type, m.text]), [['partial', 'тест'], ['final', 'тест']]);
  const text = '<img src=x onerror=alert(1)>';
  c.receive({ type: 'reply', reply_id: 'a', text });
  assert.equal(c.ui.reply.textContent, text);
  assert.equal(c.speech, undefined);
  c.receive({ type: 'event', event: { offset_ms: 42, duration_ms: 83 } });
  assert.match(c.ui.log.textContent, /"offset_ms":42,"duration_ms":83/);
});

test('permission failure leaves manual input available and preserves the error on end', () => {
  const c = client();
  c.ui.mic.onclick();
  c.mic.onerror({ error: 'not-allowed' }); c.mic.onend();
  assert.match(c.ui.micStatus.textContent, /Нет разрешения/);
  assert.equal(c.ui.final.disabled, false);
});
