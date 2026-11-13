import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { StreamMessage } from '../../shared/protocol';
import { API } from './api';
import { useConsole } from './store';

/** Keeps one EventSource open for the selected source; reconnects with backoff. */
export function useStream(): void {
  const source = useConsole((s) => s.source);
  const receive = useConsole((s) => s.receive);
  const setConnection = useConsole((s) => s.setConnection);
  const qc = useQueryClient();

  useEffect(() => {
    let es: EventSource | null = null;
    let retry: number | undefined;
    let attempt = 0;
    let closed = false;

    const handle = (ev: MessageEvent<string>) => {
      const m = JSON.parse(ev.data) as StreamMessage;
      if (m.type === 'files') {
        for (const scope of m.scopes) void qc.invalidateQueries({ queryKey: [scope] });
        return;
      }
      receive(m);
    };

    const connect = () => {
      setConnection(attempt === 0 ? 'connecting' : 'retrying');
      es = new EventSource(`${API}/api/stream?source=${encodeURIComponent(source)}`);
      for (const type of ['snapshot', 'delta', 'status', 'files']) es.addEventListener(type, handle as EventListener);
      es.onopen = () => {
        attempt = 0;
        setConnection('open');
      };
      es.onerror = () => {
        es?.close();
        if (closed) return;
        setConnection('retrying');
        attempt += 1;
        retry = window.setTimeout(connect, Math.min(10_000, 500 * 2 ** attempt));
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      es?.close();
    };
  }, [source, receive, setConnection, qc]);
}
