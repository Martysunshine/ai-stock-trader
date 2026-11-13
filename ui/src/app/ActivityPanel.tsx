import { useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { AnimatePresence, motion } from 'motion/react';
import { Bell, BellOff, X } from 'lucide-react';
import { clock, day, tzLabel } from '../design/format';
import { Segmented, SPRING, styles as ps } from '../design/primitives';
import { ActivityIcon, useLive } from './Live';
import { useConsole } from './store';
import s from './live.module.css';

type Filter = 'all' | 'trading' | 'health';
const LIMIT = 300;

/** A docked, always-current log of everything the bot did, newest first. */
export function ActivityPanel() {
  const open = useConsole((st) => st.activityOpen);
  const setOpen = useConsole((st) => st.setActivityOpen);
  const notify = useConsole((st) => st.notify);
  const setNotify = useConsole((st) => st.setNotify);
  const source = useConsole((st) => st.source);
  const tz = useConsole((st) => st.tz);
  const { activity } = useLive();
  const navigate = useNavigate();
  const [filter, setFilter] = useState<Filter>('all');
  // Items newer than the moment the panel opened get a soft highlight as they arrive.
  const openedAt = useRef<string>('');
  if (open && !openedAt.current) openedAt.current = activity[0]?.at ?? '';
  if (!open) openedAt.current = '';

  const rows = useMemo(() => activity.filter((a) => filter === 'all' || a.group === filter).slice(0, LIMIT), [activity, filter]);
  const grouped = useMemo(() => {
    const out: { day: string; items: typeof rows }[] = [];
    for (const a of rows) {
      const d = day(a.at, tz);
      if (!out.length || out[out.length - 1].day !== d) out.push({ day: d, items: [] });
      out[out.length - 1].items.push(a);
    }
    return out;
  }, [rows, tz]);

  const toggleNotify = async () => {
    if (notify) return setNotify(false);
    if (!('Notification' in window)) return;
    const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    setNotify(perm === 'granted');
  };

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.aside
          className={s.panel}
          aria-label="Activity"
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 380, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={SPRING}
        >
          <div className={s.panelInner}>
            <div className={s.panelHead}>
              <div className={s.panelTitleRow}>
                <span className={s.panelTitle}>Activity</span>
                <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <button
                    className={`${s.bell} ${notify ? s.bellOn : ''}`}
                    onClick={toggleNotify}
                    title={
                      notify
                        ? 'Desktop notifications on: fills, approvals, pauses and alerts while this tab is in the background (live data only)'
                        : 'Notify me on the desktop when the console is in the background'
                    }
                  >
                    {notify ? <Bell size={13} /> : <BellOff size={13} />}
                    {notify ? 'Notifying' : 'Notify'}
                  </button>
                  <button className={ps.iconButton} aria-label="Close activity" onClick={() => setOpen(false)}>
                    <X size={16} />
                  </button>
                </span>
              </div>
              <Segmented
                label="Activity filter"
                value={filter}
                onChange={setFilter}
                options={[
                  { value: 'all', label: 'Everything' },
                  { value: 'trading', label: 'Trading' },
                  { value: 'health', label: 'Health' },
                ]}
              />
              {notify && source !== 'live' && <span style={{ fontSize: 12, color: 'var(--ink-3)' }}>Notifications fire for live data only, never for replays.</span>}
            </div>
            <div className={s.panelList}>
              {rows.length === 0 ? (
                <div className={s.empty}>Nothing yet. Decisions, orders, fills, pauses and alerts appear here the moment they happen.</div>
              ) : (
                grouped.map((g) => (
                  <div key={g.day}>
                    <div className={s.dayHeader}>{g.day}</div>
                    {g.items.map((a) => (
                      <button
                        key={a.id}
                        className={`${s.item} ${openedAt.current && a.at > openedAt.current ? s.itemNew : ''}`}
                        onClick={() => a.link && navigate(a.link)}
                      >
                        <ActivityIcon a={a} size={15} />
                        <span className={s.itemText}>
                          <span className={s.itemTitle}>
                            {a.title}
                            <span className={s.itemTime}>
                              {clock(a.at, tz, true)} {tzLabel(tz)}
                            </span>
                          </span>
                          <span className={s.itemDetail}>{a.detail}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                ))
              )}
              {activity.length > LIMIT && <div className={s.empty}>Showing the latest {LIMIT} of {activity.length}.</div>}
            </div>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
