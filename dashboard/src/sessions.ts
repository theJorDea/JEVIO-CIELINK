import { useEffect, useState } from 'react';
import type { EvidenceEvent, Finding, Track } from './Evidence';

export type Summary = { id: string; status?: string | null; outcome?: string | null; analysis_status?: string | null };
export type Session = Summary & { events?: EvidenceEvent[] | null; tracks?: Track[] | null; findings?: Finding[] | null; receipt?: unknown; offer?: unknown; address?: string | null; coverage_status?: unknown; error?: unknown; analysis_error?: unknown };
const record = (x: unknown): x is Record<string, unknown> => x !== null && typeof x === 'object' && !Array.isArray(x);
const summary = (x: unknown): x is Summary => record(x) && typeof x.id === 'string' && x.id.length > 0 && ['status','outcome','analysis_status'].every(k => x[k] == null || typeof x[k] === 'string');
const invalid = () => new Error('Формат ответа не соответствует API.');
export function sessionList(x: unknown): Summary[] {
  if (!Array.isArray(x) || !x.every(summary)) throw invalid();
  return x;
}
export function sessionDetail(x: unknown): Session {
  if (!summary(x) || !record(x)) throw invalid();
  for (const [list,field] of [['events','type'],['tracks','role'],['findings','message']]) {
    const items = (x as Record<string, unknown>)[list];
    if (items != null && (!Array.isArray(items) || !items.every(i => record(i) && typeof i[field] === 'string'))) throw invalid();
  }
  return x;
}

// Schedule from completion, not from request start. Hidden tabs do not poll.
export function useResource<T>(url: string, parse: (x: unknown) => T, refresh: number) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState('');
  const [updated, setUpdated] = useState<Date>();
  useEffect(() => {
    let stopped = false, busy = false, delay = 5000;
    let timer: number | undefined;
    const controller = new AbortController();
    async function load() {
      window.clearTimeout(timer);
      if (stopped || busy || document.hidden) return;
      busy = true;
      try {
        const res = await fetch(url, {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]), cache:'no-store', headers:{Accept:'application/json'}});
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          throw new Error(`HTTP ${res.status}${typeof body?.detail === 'string' ? ': ' + body.detail : ''}`);
        }
        const value = parse(await res.json());
        if (!stopped) { setData(value); setUpdated(new Date()); setError(''); }
        delay = 5000;
      } catch (e) {
        if (!stopped) setError(e instanceof Error ? e.message : String(e));
        delay = Math.min(delay * 2, 60000);
      } finally {
        busy = false;
        if (!stopped && !document.hidden) timer = window.setTimeout(load, delay);
      }
    }
    void load();
    document.addEventListener('visibilitychange', load);
    return () => { stopped = true; controller.abort(); window.clearTimeout(timer); document.removeEventListener('visibilitychange',load); };
  }, [url, parse, refresh]);
  return {data,error,updated};
}
