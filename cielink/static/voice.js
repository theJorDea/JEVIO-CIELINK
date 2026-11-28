"use strict";

const el = id => document.getElementById(id);
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let socket, config, recognition, playback, origin, lastOffset = -1, latestInput = -1;
const elapsed = () => Math.round(performance.now() - origin);

function controls() {
  el("connect").disabled = !!socket;
  el("disconnect").disabled = !socket;
  el("partial").disabled = el("final").disabled = !config;
  el("mic").disabled = config?.stt !== "browser" || !Recognition;
  el("mic").textContent = recognition ? "Выключить микрофон" : "Включить микрофон";
  el("stop").disabled = !playback;
}

function log(direction, message) {
  el("log").append(`${elapsed()} мс ${direction} ${JSON.stringify(message)}\n`);
}

function send(type, fields) {
  if (!config || socket?.readyState !== WebSocket.OPEN) return;
  lastOffset = Math.max(elapsed(), lastOffset + 1);
  if (type === "partial" || type === "final") latestInput = lastOffset;
  const message = { type, ...fields, offset_ms: lastOffset };
  socket.send(JSON.stringify(message));
  log("→", message);
}

function interrupt() {
  if (!playback) return;
  const { reply_id } = playback;
  playback = null; // cancel() may dispatch stale end/error callbacks.
  window.speechSynthesis.cancel();
  if (reply_id) send("playback_interrupted", { reply_id });
  el("ttsStatus").textContent = "Озвучивание остановлено.";
  controls();
}

function reply({ reply_id, text, input_offset_ms }) {
  // A correction may already be on its way while the old server read is finishing.
  if (input_offset_ms !== undefined && input_offset_ms < latestInput) return;
  interrupt();
  el("reply").textContent = text;
  if (config.tts === "text") { if (reply_id) send("text_displayed", { reply_id }); return; }
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "ru-RU";
  playback = { reply_id, utterance };
  controls();
  utterance.onstart = () => {
    if (playback?.utterance !== utterance) return;
    el("ttsStatus").textContent = "Идёт озвучивание.";
    if (reply_id) send("playback_started", { reply_id });
  };
  utterance.onend = utterance.onerror = event => {
    if (playback?.utterance !== utterance) return;
    playback = null;
    const failed = event.type === "error";
    el("ttsStatus").textContent = failed ? `Ошибка озвучивания: ${event.error}.` : "Озвучивание завершено.";
    if (reply_id) send(failed ? "playback_interrupted" : "playback_ended", { reply_id });
    controls();
  };
  window.speechSynthesis.speak(utterance);
}

function transcript(type, value) {
  const text = value.trim();
  if (!text) return;
  interrupt();
  el("heard").textContent = text;
  send(type, { text });
}

function stopMic(message = "Микрофон выключен.") {
  const old = recognition;
  recognition = null;
  old?.abort();
  el("micStatus").textContent = message;
  controls();
}

el("mic").onclick = () => {
  if (recognition) return stopMic();
  const mic = recognition = new Recognition();
  mic.lang = "ru-RU";
  mic.continuous = mic.interimResults = true;
  controls();
  el("micStatus").textContent = "Ожидаем доступ к микрофону…";
  mic.onstart = () => {
    if (recognition === mic) el("micStatus").textContent = "Микрофон включён. Говорите.";
  };
  mic.onspeechstart = () => { if (recognition === mic) interrupt(); };
  mic.onresult = event => {
    if (recognition !== mic) return;
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      transcript(result.isFinal ? "final" : "partial", result[0].transcript);
    }
  };
  mic.onerror = event => {
    if (recognition !== mic) return;
    const denied = ["not-allowed", "service-not-allowed"].includes(event.error);
    stopMic((denied ? "Нет разрешения на распознавание речи." : `Ошибка распознавания: ${event.error}.`) + " Доступен ручной ввод.");
  };
  mic.onend = () => {
    if (recognition !== mic) return;
    recognition = null;
    el("micStatus").textContent = "Распознавание завершено. Можно снова включить микрофон.";
    controls();
  };
  try { mic.start(); } catch { stopMic("Не удалось включить микрофон. Доступен ручной ввод."); }
};

function shutdown(message) {
  interrupt();
  config = null;
  const old = socket;
  socket = null;
  stopMic();
  old?.close();
  el("status").textContent = message;
  el("ttsStatus").textContent = "Озвучивание не активно.";
}

el("connect").onclick = () => {
  origin = performance.now();
  latestInput = lastOffset = -1;
  el("log").textContent = "";
  el("heard").textContent = el("reply").textContent = "—";
  el("status").textContent = "Подключение…";
  const ws = socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/voice`);
  controls();
  ws.onmessage = ({ data }) => {
    if (ws !== socket) return;
    const msg = JSON.parse(data);
    log("←", msg);
    if (msg.type === "ready") {
      config = msg.config;
      el("status").textContent = `Сеанс ${msg.session_id} открыт.`;
      el("micStatus").textContent = config.stt === "text" ? "Режим текстового ввода." : Recognition ? "Микрофон выключен." : "Распознавание недоступно. Используйте ручной ввод.";
      el("ttsStatus").textContent = config.tts === "text" ? "Ответы без озвучивания." : window.speechSynthesis && window.SpeechSynthesisUtterance ? "Озвучивание готово." : "Озвучивание недоступно. Ответы будут показаны текстом.";
      if (msg.greeting) reply({ text: msg.greeting });
      controls();
    } else if (msg.type === "reply" && config) reply(msg);
    else if (msg.type === "error") el("status").textContent = `Ошибка: ${msg.message}`;
  };
  ws.onclose = () => { if (ws === socket) shutdown("Соединение закрыто."); };
  ws.onerror = () => { if (ws === socket) shutdown("Ошибка соединения."); };
};
el("partial").onclick = () => transcript("partial", el("text").value);
el("form").onsubmit = event => {
  event.preventDefault();
  if (!config) return;
  transcript("final", el("text").value);
  el("text").value = "";
};
el("stop").onclick = interrupt;
el("disconnect").onclick = () => shutdown("Сеанс завершён.");
window.addEventListener("pagehide", () => shutdown("Сеанс завершён."));
