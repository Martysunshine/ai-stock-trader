import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { AnimatePresence, motion } from 'motion/react';
import {
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  Building2,
  CircleDot,
  PauseCircle,
  PlayCircle,
  RefreshCw,
  Send,
  TrendingDown,
  Wallet,
  X,
  type LucideIcon,
} from 'lucide-react';
import { deriveActivity, type Activity, type ActivityTone } from './activity';
import { useConsole } from './store';
import { clock, tzLabel } from '../design/format';
import { SPRING } from '../design/primitives';
import s from './live.module.css';

export const TONE_COLOR: Record<ActivityTone, string> = {
  neutral: 'var(--ink-3)',
  accent: 'var(--accent)',
  gain: 'var(--gain-mark)',
  loss: 'var(--loss-mark)',
  warning: 'var(--warning)',
  critical: 'var(--critical)',
  good: 'var(--good)',
};

export const KIND_ICON: Record<Activity['kind'], LucideIcon> = {
  decision: CircleDot,
  order: Send,
  broker: Building2,
  fill: ArrowDownLeft,
  pause: PauseCircle,
  resume: PlayCircle,
  alert: AlertTriangle,
  recon: RefreshCw,
  loss: TrendingDown,
  flow: Wallet,
};

export function ActivityIcon({ a, size = 16 }: { a: Activity; size?: number }) {
  const Icon = a.kind === 'fill' && a.title.startsWith('Sold') ? ArrowUpRight : KIND_ICON[a.kind];
  return (
    <span className={s.icon} style={{ color: TONE_COLOR[a.tone], background: `color-mix(in srgb, ${TONE_COLOR[a.tone]} 14%, transparent)` }}>
      <Icon size={size} strokeWidth={2} />
    </span>
  );
}

interface LiveValue {
  activity: Activity[];
  unread: number;
}
const LiveContext = createContext<LiveValue>({ activity: [], unread: 0 });
export const useLive = () => useContext(LiveContext);

interface Toast {
  key: number;
  a: Activity;
  extra?: number;
}

/**
 * Watches the stream for new activity. Rows that arrive with a snapshot (load, reconnect, source
 * switch, replay seek) are history, not news: they are marked seen without toasting.
 */
export function LiveProvider({ children }: { children: ReactNode }) {
  const tables = useConsole((st) => st.tables);
  const snapshotVersion = useConsole((st) => st.snapshotVersion);
  const source = useConsole((st) => st.source);
  const activityOpen = useConsole((st) => st.activityOpen);
  const seenAt = useConsole((st) => st.activitySeenAt);
  const markSeen = useConsole((st) => st.markActivitySeen);
  const notify = useConsole((st) => st.notify);
  const tz = useConsole((st) => st.tz);
  const activity = useMemo(() => deriveActivity(tables), [tables]);
  const known = useRef<Set<string>>(new Set());
  const lastSnapshot = useRef(-1);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastKey = useRef(0);

  useEffect(() => {
    if (snapshotVersion !== lastSnapshot.current) {
      lastSnapshot.current = snapshotVersion;
      known.current = new Set(activity.map((a) => a.id));
      if (activity[0]) markSeen(activity[0].at);
      setToasts([]);
      return;
    }
    const fresh = activity.filter((a) => !known.current.has(a.id));
    for (const a of fresh) known.current.add(a.id);
    if (activityOpen && activity[0]) markSeen(activity[0].at);
    const notable = fresh.filter((a) => a.notable);
    if (!notable.length || activityOpen) return;
    const next: Toast[] =
      notable.length > 3
        ? [{ key: ++toastKey.current, a: notable[0], extra: notable.length - 1 }]
        : notable.map((a) => ({ key: ++toastKey.current, a }));
    setToasts((cur) => [...next, ...cur].slice(0, 4));

    // Desktop notifications: live data only, and only while the console is in the background.
    if (notify && source === 'live' && document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      for (const a of notable.slice(0, 3)) {
        new Notification(a.title, { body: `${a.detail} · ${clock(a.at, tz)} ${tzLabel(tz)}`, tag: a.id, icon: '/assets/mark.svg' });
      }
    }
  }, [activity, snapshotVersion, activityOpen, markSeen, notify, source, tz]);

  const unread = useMemo(() => (seenAt ? activity.filter((a) => a.at > seenAt).length : 0), [activity, seenAt]);

  return (
    <LiveContext.Provider value={{ activity, unread }}>
      {children}
      <Toasts toasts={toasts} dismiss={(k) => setToasts((cur) => cur.filter((t) => t.key !== k))} />
    </LiveContext.Provider>
  );
}

function Toasts({ toasts, dismiss }: { toasts: Toast[]; dismiss: (key: number) => void }) {
  return (
    <div className={s.toasts} aria-live="polite">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <ToastCard key={t.key} t={t} dismiss={() => dismiss(t.key)} />
        ))}
      </AnimatePresence>
    </div>
  );
}

function ToastCard({ t, dismiss }: { t: Toast; dismiss: () => void }) {
  const navigate = useNavigate();
  const tz = useConsole((st) => st.tz);
  const setActivityOpen = useConsole((st) => st.setActivityOpen);
  const [hover, setHover] = useState(false);
  useEffect(() => {
    if (hover) return;
    const timer = setTimeout(dismiss, 6500);
    return () => clearTimeout(timer);
  }, [hover, dismiss]);
  return (
    <motion.div
      layout
      className={s.toast}
      initial={{ opacity: 0, y: -14, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, x: 40, transition: { duration: 0.18 } }}
      transition={SPRING}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      role="status"
    >
      <button
        className={s.toastBody}
        onClick={() => {
          if (t.extra) setActivityOpen(true);
          else if (t.a.link) navigate(t.a.link);
          dismiss();
        }}
      >
        <ActivityIcon a={t.a} />
        <span className={s.toastText}>
          <span className={s.toastTitle}>
            {t.a.title}
            <span className={s.toastTime}>
              {clock(t.a.at, tz, true)} {tzLabel(tz)}
            </span>
          </span>
          <span className={s.toastDetail}>{t.extra ? `and ${t.extra} more · open Activity` : t.a.detail}</span>
        </span>
      </button>
      <button className={s.toastClose} aria-label="Dismiss" onClick={dismiss}>
        <X size={14} />
      </button>
    </motion.div>
  );
}
