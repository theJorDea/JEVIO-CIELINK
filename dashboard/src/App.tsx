import { useCallback, useEffect, useState } from 'react';
import Evidence from './Evidence';
import { sessionDetail, sessionList, useResource } from './sessions';
import type { Summary } from './sessions';

const missing = 'Не указано';
function Value({value, fallback = missing}: {value: unknown; fallback?: string}) {
  if (value == null || value === '') return <span className="muted">{fallback}</span>;
  if (typeof value === 'object') return <pre>{JSON.stringify(value, null, 2)}</pre>;
  return <>{String(value)}</>;
}
function Problem({text, stale}: {text: string; stale: boolean}) {
  return <div className="problem" role="status"><strong>Ошибка обновления</strong><p>{text}</p>{stale && <small>Показаны последние полученные данные.</small>}</div>;
}
function RecordView({summary,refresh}: {summary: Summary; refresh:number}) {
  const parse = useCallback((x: unknown) => {
    const detail = sessionDetail(x);
    if (detail.id !== summary.id) throw new Error('API вернул другую сессию.');
    return detail;
  }, [summary.id]);
  const {data,error} = useResource('/api/sessions/' + encodeURIComponent(summary.id),parse,refresh);
  const record = data ?? summary;
  return <article className="record">
    <p className="label">Карточка сессии</p><h2 className="session-id">{summary.id}</h2>
    <dl className="facts">{[['Статус',record.status],['Исход',record.outcome],['Анализ',record.analysis_status]].map(([label,value]) => <div key={label}><dt>{label}</dt><dd><Value value={value}/></dd></div>)}</dl>
    {error && <Problem text={error} stale={!!data}/>}
    {!data && !error && <p role="status">Загрузка материалов…</p>}
    {data && <>
      {(['error','analysis_error'] as const).map(k => data[k] != null && <div key={k} className="problem"><strong>{k === 'error' ? 'Ошибка сессии' : 'Ошибка анализа'}</strong><div><Value value={data[k]}/></div></div>)}
      <Evidence events={data.events} tracks={data.tracks} findings={data.findings}/>
      <section><h3>Результат консультации</h3><dl className="results">{[
        ['Адрес',data.address,'Адрес не зафиксирован.'],['Покрытие',data.coverage_status,'Статус покрытия не передан.'],['Предложение',data.offer,'Нет данных о предложении.'],['Квитанция CRM',data.receipt,'Квитанция не получена.']
      ].map(([label,value,fallback]) => <div key={String(label)}><dt>{String(label)}</dt><dd><Value value={value} fallback={String(fallback)}/></dd></div>)}</dl></section>
    </>}
  </article>;
}
export default function App() {
  const [refresh,setRefresh] = useState(0);
  const [choice,setChoice] = useState<string>();
  const { data,error,updated } = useResource('/api/sessions',sessionList,refresh);
  const selected = data?.find(s => s.id === choice) ?? data?.[0];
  useEffect(() => { if (selected && selected.id !== choice) setChoice(selected.id); }, [choice, selected]);
  return <><header className="masthead"><div className="mast-inner"><strong className="brand"><i aria-hidden="true"/>CIELINK</strong><span>Рабочее место оператора</span></div></header>
    <main><div className="title-row"><div><p className="label">Контроль звонков</p><h1>Журнал сессий</h1><p className="intro">Записи разговоров, события и результат консультации.</p></div>
    <div className="sync"><button className="refresh" onClick={() => setRefresh(n => n+1)}>Обновить</button>{updated && <small>Список на {updated.toLocaleTimeString('ru-RU')}</small>}</div></div>
    <div className="workspace"><aside aria-label="Список сессий"><div className="aside-title"><h2>Сессии</h2>{data && <span className="count">{data.length}</span>}</div>
      {error && <Problem text={error} stale={!!data}/>}
      {!data && !error && <p role="status">Загрузка списка…</p>}
      {data?.length === 0 && <p className="muted">Список пуст.</p>}
      {!!data?.length && <ul className="sessions">{data.map(s => <li key={s.id}><button aria-pressed={s.id === selected?.id} onClick={() => setChoice(s.id)}><strong>{s.id}</strong><span>{s.status || 'Статус не указан'} · {s.outcome || 'Исход не указан'}</span></button></li>)}</ul>}
    </aside>{selected ? <RecordView key={selected.id} summary={selected} refresh={refresh}/> : <div className="blank"><h2>{error && !data ? 'Список сессий недоступен' : data ? 'Пока нет сессий' : 'Получаем данные'}</h2><p>{error && !data ? 'Проверьте доступность API. Журнал повторит запрос автоматически.' : data ? 'Когда API вернёт первую сессию, здесь появятся её материалы.' : 'Запрашиваем журнал сессий у API.'}</p></div>}</div></main></>;
}
