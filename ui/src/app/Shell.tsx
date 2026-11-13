import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { NavLink, useLocation } from 'react-router';
import { motion } from 'motion/react';
import {
  Activity as PulseIcon,
  CandlestickChart,
  FlaskConical,
  LayoutGrid,
  ListTree,
  Moon,
  NotebookPen,
  Pause,
  Play,
  RotateCcw,
  SkipForward,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
  SunMoon,
  Wallet,
} from 'lucide-react';
import { REPLAY_SPEEDS, type SourceId } from '../../shared/protocol';
import { clock, tzLabel } from '../design/format';
import { MarkGlyph } from '../design/Mark';
import { Dot, Pill, Segmented, SPRING } from '../design/primitives';
import { replayControl, useSources } from './api';
import { botStatus } from './botStatus';
import { sessionAt } from './session';
import { useConsole, viewNow } from './store';
import { useNow } from './hooks';
import { ActivityPanel } from './ActivityPanel';
import { useLive } from './Live';
import s from './shell.module.css';

// Grouped by what a page is about: the whole mission, the stock bot's own detail, research.
const NAV = [
  { to: '/', label: 'Overview', icon: LayoutGrid, group: 'Mission' },
  { to: '/markets', label: 'Markets', icon: CandlestickChart },
  // Your workspace: the setup sheet, the goal and notes for Claude.
  { to: '/desk', label: 'Desk', icon: NotebookPen },
  { to: '/decisions', label: 'Decisions', icon: ListTree, group: 'Stock bot' },
  { to: '/money', label: 'Money', icon: Wallet },
  { to: '/risk', label: 'Risk & health', icon: ShieldCheck },
  { to: '/rules', label: 'Rules', icon: SlidersHorizontal },
  { to: '/learning', label: 'Learning', icon: FlaskConical, group: 'Research' },
];

export function Shell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const alerts = useConsole((st) => st.tables.alerts.length);
  const pauses = useConsole((st) => st.tables.pause_state.filter((p) => p.active).length);
  return (
    <div className={s.shell}>
      <aside className={s.sidebar}>
        <div className={s.brand}>
          <span className={s.brandMark}>
            <MarkGlyph size={20} />
          </span>
          <div>
            <div className={s.brandName}>Trader Console</div>
            <div className={s.brandSub}>Read-only · localhost</div>
          </div>
        </div>
        <nav className={s.nav} aria-label="Sections">
          {NAV.map(({ to, label, icon: Icon, group }) => {
            const active =
              to === '/' ? location.pathname === '/' : location.pathname.startsWith(to) || (to === '/money' && location.pathname.startsWith('/trades/'));
            const badge = to === '/risk' ? pauses + alerts : 0;
            return (
              <div key={to} style={{ display: 'contents' }}>
                {group && <span className={s.navGroup}>{group}</span>}
                <NavLink to={to} className={`${s.navItem} ${active ? s.navActive : ''}`}>
                  {active && <motion.span layoutId="nav-thumb" className={s.navThumb} transition={SPRING} />}
                  <Icon size={18} strokeWidth={1.6} aria-hidden />
                  {label}
                  {badge > 0 && <span className={s.navBadge}>{badge}</span>}
                </NavLink>
              </div>
            );
          })}
        </nav>
        <SidebarFoot />
      </aside>
      <div className={s.main}>
        <StatusStrip />
        <main className={s.page}>{children}</main>
      </div>
      <ActivityPanel />
    </div>
  );
}

function SidebarFoot() {
  const tz = useConsole((st) => st.tz);
  const setTz = useConsole((st) => st.setTz);
  const theme = useConsole((st) => st.theme);
  const setTheme = useConsole((st) => st.setTheme);
  return (
    <div className={s.sideFoot}>
      <div className={s.sideRow}>
        <span>Times</span>
        <Segmented
          label="Time zone"
          value={tz}
          onChange={setTz}
          options={[
            { value: 'market', label: 'ET' },
            { value: 'local', label: tzLabel('local') },
          ]}
        />
      </div>
      <div className={s.sideRow}>
        <span>Theme</span>
        <Segmented
          label="Theme"
          value={theme}
          onChange={setTheme}
          options={[
            { value: 'system', label: <SunMoon size={14} aria-label="System" /> },
            { value: 'light', label: <Sun size={14} aria-label="Light" /> },
            { value: 'dark', label: <Moon size={14} aria-label="Dark" /> },
          ]}
        />
      </div>
      <p className={s.attribution}>
        <span className={s.kbd}>Ctrl K</span> to jump anywhere. Charts by{' '}
        <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">
          TradingView Lightweight Charts
        </a>
        .
      </p>
    </div>
  );
}

function StatusStrip() {
  const source = useConsole((st) => st.source);
  const setSource = useConsole((st) => st.setSource);
  const status = useConsole((st) => st.status);
  const connection = useConsole((st) => st.connection);
  const pauses = useConsole((st) => st.tables.pause_state);
  const tz = useConsole((st) => st.tz);
  const lastMessageAt = useConsole((st) => st.lastMessageAt);
  const wall = useNow(1000);
  const sources = useSources();
  const scenarios = sources.data?.scenarios ?? [];
  const now = viewNow(status, wall);
  const session = useMemo(() => sessionAt(now), [now]);
  const bot = botStatus(status, pauses, now, session);
  const isReplay = source.startsWith('replay:');
  const replay = status?.replay ?? null;
  const scenario = isReplay ? source.slice('replay:'.length) : null;

  const pickKind = (kind: 'live' | 'replay') => {
    if (kind === 'live') setSource('live');
    else setSource(`replay:${scenarios.find((x) => x.id === 'baseline')?.id ?? scenarios[0]?.id ?? 'baseline'}` as SourceId);
  };

  const sessionText =
    session.phase === 'closed'
      ? session.nextOpen
        ? `Opens ${new Intl.DateTimeFormat('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: tz === 'market' ? 'America/New_York' : undefined }).format(session.nextOpen)} ${tzLabel(tz)}`
        : 'Calendar ends; re-export'
      : session.detail;

  return (
    <>
      <div className={s.strip}>
        <div className={s.stripGroup}>
          <Segmented
            label="Data source"
            value={isReplay ? 'replay' : 'live'}
            onChange={(v) => pickKind(v as 'live' | 'replay')}
            options={[
              { value: 'live', label: 'Live paper' },
              { value: 'replay', label: 'Replay' },
            ]}
          />
          {isReplay && (
            <>
              <select
                className={s.select}
                value={scenario ?? ''}
                aria-label="Replay scenario"
                onChange={(e) => setSource(`replay:${e.target.value}` as SourceId)}
              >
                {scenarios.map((sc) => (
                  <option key={sc.id} value={sc.id}>
                    {sc.id}
                  </option>
                ))}
              </select>
              {replay && scenario && (
                <>
                  <div className={s.transport}>
                    <button
                      className={s.transportButton}
                      aria-label={replay.playing ? 'Pause replay' : 'Play replay'}
                      title={replay.playing ? 'Pause (Space)' : 'Play (Space)'}
                      onClick={() => replayControl(scenario, { action: replay.playing ? 'pause' : 'play' })}
                    >
                      {replay.playing ? <Pause size={15} fill="currentColor" /> : <Play size={15} fill="currentColor" />}
                    </button>
                    <button className={s.transportButton} aria-label="Jump to the next event" title="Jump to the next decision, order, fill or pause" onClick={() => replayControl(scenario, { action: 'next' })}>
                      <SkipForward size={15} />
                    </button>
                    <button className={s.transportButton} aria-label="Restart replay" title="Restart" onClick={() => replayControl(scenario, { action: 'restart' })}>
                      <RotateCcw size={14} />
                    </button>
                    <span className={s.transportTime}>
                      {clock(replay.now, tz)} <span className={s.transportOf}>/ {clock(replay.endAt, tz)}</span>
                    </span>
                  </div>
                  <Segmented
                    label="Replay speed"
                    value={replay.speed}
                    onChange={(v) => replayControl(scenario, { action: 'speed', speed: v })}
                    options={REPLAY_SPEEDS.map((v) => ({ value: v, label: `${v}×` }))}
                  />
                  <span className={s.badge} title={replay.watermark ?? undefined}>SYNTHETIC</span>
                </>
              )}
            </>
          )}
        </div>
        <div className={`${s.stripGroup} ${s.stripRight}`}>
          <div className={s.clock}>
            <span className={s.clockMain}>
              {clock(now, tz, true)} {tzLabel(tz)}
              {isReplay && replay ? <span className={s.clockDate}> · {new Date(now).toISOString().slice(0, 10)}</span> : null}
            </span>
            <span className={s.clockSub}>
              {session.label}
              {sessionText ? ` · ${sessionText}` : ''}
            </span>
          </div>
          <Pill tone={bot.tone} live={bot.tone === 'running' || (bot.tone === 'replay' && Boolean(replay?.playing))} title={bot.detail}>
            {bot.label}
          </Pill>
          <ActivityButton />
          <span title={connection === 'open' ? `Last update ${lastMessageAt ? Math.round((wall - lastMessageAt) / 1000) : '—'}s ago` : 'Reconnecting to the console server'}>
            <Dot tone={connection === 'open' ? 'good' : 'paused'} />
          </span>
        </div>
      </div>
      {isReplay && replay && scenario && <Scrubber scenario={scenario} />}
    </>
  );
}

function ActivityButton() {
  const open = useConsole((st) => st.activityOpen);
  const setOpen = useConsole((st) => st.setActivityOpen);
  const { unread } = useLive();
  return (
    <button
      className={`${s.activityButton} ${open ? s.activityButtonOn : ''}`}
      onClick={() => setOpen(!open)}
      aria-pressed={open}
      title="Activity: everything the bot did, as it happens (A)"
    >
      <PulseIcon size={15} strokeWidth={2} />
      <span className={s.activityLabel}>Activity</span>
      {unread > 0 && !open && <span className={s.unread}>{unread > 99 ? '99+' : unread}</span>}
    </button>
  );
}

function Scrubber({ scenario }: { scenario: string }) {
  const replay = useConsole((st) => st.status?.replay);
  const decisions = useConsole((st) => st.tables.risk_decisions);
  const [hover, setHover] = useState(false);
  if (!replay) return null;
  const a = Date.parse(replay.startAt);
  const b = Date.parse(replay.endAt);
  const f = b > a ? (Date.parse(replay.now) - a) / (b - a) : 0;
  return (
    <div
      className={s.scrub}
      role="slider"
      aria-label="Replay position"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(f * 100)}
      tabIndex={0}
      style={hover ? { height: 5 } : undefined}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        const at = a + ((e.clientX - r.left) / r.width) * (b - a);
        void replayControl(scenario, { action: 'seek', at: new Date(at).toISOString() });
      }}
    >
      <motion.div className={s.scrubFill} animate={{ width: `${f * 100}%` }} transition={{ duration: 0.12, ease: 'linear' }} />
      {decisions.map((d) => (
        <span key={d.decision_id} className={s.scrubTick} style={{ left: `${((Date.parse(d.decided_at) - a) / (b - a)) * 100}%` }} />
      ))}
    </div>
  );
}

export function useApplyTheme() {
  const theme = useConsole((st) => st.theme);
  useEffect(() => {
    if (theme === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }, [theme]);
}
