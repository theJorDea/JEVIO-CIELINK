import * as React from 'react';

export interface EvidenceEvent {
  type: string;
  offset_ms?: number | null;
  text?: string | null;
  [key: string]: unknown;
}

export interface Track {
  role: string;
  url?: string | null;
  start_ms?: number | null;
  [key: string]: unknown;
}

export interface Finding {
  start_ms?: number | null;
  end_ms?: number | null;
  message: string;
  [key: string]: unknown;
}

type Props = {
  events?: EvidenceEvent[] | null;
  tracks?: Track[] | null;
  findings?: Finding[] | null;
};

function hasTime(ms: unknown): ms is number {
  return typeof ms === 'number' && Number.isFinite(ms) && ms >= 0;
}

function time(ms: unknown) {
  return hasTime(ms) ? `${ms} мс` : 'не указано';
}

function extraValue(value: unknown) {
  if (typeof value === 'string' && value !== '') return value;
  try { return JSON.stringify(value, null, 2) ?? String(value); }
  catch { return 'Значение не удалось отобразить'; }
}

function PlaybackLog({ track, events, findings }: { track?: Track; events: EvidenceEvent[]; findings: Finding[] }) {
  const audio = React.useRef<HTMLAudioElement>(null);
  const [duration, setDuration] = React.useState<number | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [notice, setNotice] = React.useState('');
  const url = track?.url?.trim();
  const state = !track ? 'missing' : track.status === 'failed' || failed ? 'failed'
    : track.status === 'pending' ? 'pending' : !url ? 'missing' : duration === null ? 'pending' : 'valid';
  const status = !track ? 'Аудиодорожек нет.' : state === 'failed' ? 'Не удалось загрузить аудио.'
    : track.status === 'pending' ? 'Запись ещё не готова.' : !url ? 'Ссылка на запись отсутствует.'
    : state === 'pending' ? 'Загрузка аудио…' : 'Запись доступна.';
  const start = track?.start_ms;
  function syncDuration(el: HTMLAudioElement) {
    if (el.readyState >= 1) setDuration(el.duration);
  }

  function seekTarget(offset: unknown) {
    const seconds = hasTime(offset) && hasTime(start) ? (offset - start) / 1000 : null;
    const reason = !hasTime(offset) ? 'Время неизвестно' : state !== 'valid' ? status
      : seconds === null ? 'Начало дорожки неизвестно' : duration === null || !Number.isFinite(duration) ? 'Длительность неизвестна'
      : seconds < 0 || seconds >= duration ? 'Вне дорожки' : '';
    function seek() {
      if (!audio.current || seconds === null || reason) return;
      try {
        audio.current.currentTime = seconds;
        setNotice(`Позиция в записи: ${time(seconds * 1000)}.`);
      } catch { setNotice('Переход к записи недоступен.'); }
    }
    return { reason, seek };
  }

  return <>
    <p className="evidence__status" data-state={state} role="status">{status}</p>
    {url && track?.status !== 'failed' && track?.status !== 'pending' && <audio
      className="evidence__audio" controls preload="metadata" src={url} ref={audio}
      aria-label={`Аудиозапись: ${track?.role || 'роль не указана'}`}
      onLoadedMetadata={e => syncDuration(e.currentTarget)} onDurationChange={e => syncDuration(e.currentTarget)}
      onError={() => { setFailed(true); setNotice(''); }} />}
    {track && <p className="evidence__time">Начало дорожки: {time(start)}</p>}
    <p className="evidence__notice" role="status">{notice}</p>
    <section className="evidence__events">
      <h3>События</h3>
      {events.length === 0 ? <p className="evidence__empty">Событий нет.</p> : <ol>
        {events.map((event, i) => {
          const offset = event.offset_ms;
          const { reason, seek } = seekTarget(offset);
          const extras = Object.entries(event).filter(([k]) => !['type', 'offset_ms', 'text'].includes(k));
          return <li className="evidence__event" key={i}>
            <button type="button" className="event-link" disabled={!!reason} title={reason || 'Перейти к событию в записи'} onClick={seek}><strong>{event.type || 'Тип не указан'}</strong>{' · '}<span className="evidence__time">{time(offset)}</span>
              {event.text && <span className="utterance">{event.text}</span>}
              {!reason && <span className="action-label">Перейти к записи</span>}
            </button>{reason && <small className="evidence__reason">{reason}</small>}
            {extras.length > 0 && <details className="evidence__extras"><summary>Поля события</summary><dl>
              {extras.map(([k, v]) => <React.Fragment key={k}><dt>{k}</dt><dd><pre>{extraValue(v)}</pre></dd></React.Fragment>)}
            </dl></details>}
          </li>;
        })}
      </ol>}
    </section>
    <section className="evidence__findings"><h3>Выводы</h3>
      {!findings.length ? <p className="evidence__empty">Выводов нет.</p> : <ul>
        {findings.map((finding, i) => {
          const { reason, seek } = seekTarget(finding.start_ms);
          return <li className="evidence__finding" key={i}>
            <button type="button" className="event-link" disabled={!!reason} title={reason || 'Перейти к началу интервала'} onClick={seek}>
              <span>{finding.message || 'Сообщение отсутствует.'}</span>
              <span className="evidence__time">Начало: {time(finding.start_ms)} · Конец: {time(finding.end_ms)}</span>
              {!reason && <span className="action-label">Перейти к записи</span>}
            </button>{reason && <small className="evidence__reason">{reason}</small>}
          </li>;
        })}
      </ul>}
    </section>
  </>;
}

export default function Evidence({ events, tracks, findings }: Props) {
  const [selection, setSelection] = React.useState('');
  const choices = (tracks ?? []).map((track, i) => ({ track, key: JSON.stringify([i, track.role, track.url, track.start_ms]) }));
  const active = choices.find(c => c.key === selection) ?? choices[0];
  return <section className="evidence" aria-label="Материалы сессии">
    <h3>Аудиодорожки</h3>
    {active && <label className="evidence__track">Дорожка по роли{' '}
      <select value={active.key} onChange={e => setSelection(e.target.value)}>
        {choices.map(({ track, key }, i) => <option key={key} value={key}>{i + 1}. {track.role || 'Роль не указана'}</option>)}
      </select>
    </label>}
    <PlaybackLog key={`${active?.key}:${active?.track.status}`} track={active?.track} events={events ?? []} findings={findings ?? []} />
  </section>;
}
