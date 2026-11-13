import { useEffect, useState } from 'react';
import { useConsole } from './store';

/** Wall-clock time that re-renders every `ms`. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** Whether the dark appearance is in effect (theme toggle or OS). */
export function useIsDark(): boolean {
  const theme = useConsole((s) => s.theme);
  const [osDark, setOsDark] = useState(() => matchMedia('(prefers-color-scheme: dark)').matches);
  useEffect(() => {
    const mq = matchMedia('(prefers-color-scheme: dark)');
    const on = () => setOsDark(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return theme === 'dark' || (theme === 'system' && osDark);
}
