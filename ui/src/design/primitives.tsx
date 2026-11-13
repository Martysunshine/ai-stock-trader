import { useEffect, useId, useState, type ReactNode } from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'motion/react';
import { ArrowDownRight, ArrowUpRight, Check, Copy, X } from 'lucide-react';
import type { Dec } from '../../shared/schema';
import { pct, sign, usd } from './format';
import s from './primitives.module.css';

export const SPRING = { type: 'spring', stiffness: 380, damping: 34, mass: 0.9 } as const;
const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(' ');

export function Card(props: {
  title?: ReactNode;
  sub?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
  flush?: boolean;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <section className={cx(s.card, props.flush && s.cardFlush, props.className)} style={props.style}>
      {(props.title || props.action) && (
        <header className={s.cardHead} style={props.flush ? { padding: '20px 24px 0' } : undefined}>
          <div>
            {props.title && <h2 className={s.cardTitle}>{props.title}</h2>}
            {props.sub && <p className={s.cardSub}>{props.sub}</p>}
          </div>
          {props.action}
        </header>
      )}
      {props.children}
    </section>
  );
}

/** Rolling digits for money that changes in front of you. */
export function Odometer({ text, className }: { text: string; className?: string }) {
  const reduce = useReducedMotion();
  return (
    <span className={cx(s.odo, className)} aria-label={text}>
      {text.split('').map((ch, i) => {
        const d = ch.charCodeAt(0) - 48;
        if (d < 0 || d > 9 || reduce) return <span key={`${i}-${ch}`} aria-hidden>{ch}</span>;
        return (
          <span key={i} className={s.odoDigit} aria-hidden>
            <motion.span className={s.odoStrip} initial={false} animate={{ y: `${-d * 1.15}em` }} transition={SPRING}>
              {Array.from({ length: 10 }, (_, n) => (
                <span key={n}>{n}</span>
              ))}
            </motion.span>
          </span>
        );
      })}
    </span>
  );
}

export function Delta({ value, kind = 'usd', pill, precise }: { value: Dec | number | null; kind?: 'usd' | 'pct'; pill?: boolean; precise?: boolean }) {
  const sg = sign(value);
  const Icon = sg > 0 ? ArrowUpRight : sg < 0 ? ArrowDownRight : null;
  const tone = sg > 0 ? s.gain : sg < 0 ? s.loss : s.flat;
  const text = value === null ? '—' : kind === 'usd' ? usd(value, { sign: true, precise }) : pct(value, 2, true);
  return (
    <span className={cx(s.delta, tone, pill && s.deltaPill)}>
      {Icon && <Icon size={14} strokeWidth={2.25} aria-hidden />}
      {text}
    </span>
  );
}

export function Stat(props: { label: ReactNode; value: ReactNode; foot?: ReactNode; delta?: ReactNode }) {
  return (
    <div className={s.stat}>
      <span className={s.label}>{props.label}</span>
      <span className={s.statValue}>{props.value}</span>
      {props.delta}
      {props.foot && <span className={s.statFoot}>{props.foot}</span>}
    </div>
  );
}

/** Severity follows usage: accent below 50%, warning to 80%, critical above. */
export function Meter(props: {
  label: ReactNode;
  used: number;
  valueText: ReactNode;
  foot?: ReactNode;
  invert?: boolean;
}) {
  const f = Math.max(0, Math.min(1, props.used));
  const color = f >= 0.8 ? 'var(--critical)' : f >= 0.5 ? 'var(--warning)' : 'var(--accent)';
  return (
    <div className={s.meter}>
      <div className={s.meterHead}>
        <span className={s.label}>{props.label}</span>
        <span className={s.meterValue}>{props.valueText}</span>
      </div>
      <div className={s.meterTrack} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(f * 100)}>
        <motion.div
          className={s.meterFill}
          style={{ background: color }}
          initial={{ scaleX: 0 }}
          animate={{ scaleX: Math.max(f, 0.004) }}
          transition={SPRING}
        />
      </div>
      {props.foot && <span className={s.meterFoot}>{props.foot}</span>}
    </div>
  );
}

export type Tone = 'running' | 'idle' | 'paused' | 'alert' | 'offline' | 'replay' | 'good' | 'neutral';
const TONE_COLOR: Record<Tone, string> = {
  running: 'var(--good)',
  good: 'var(--good)',
  idle: 'var(--ink-3)',
  neutral: 'var(--ink-3)',
  paused: 'var(--warning)',
  alert: 'var(--critical)',
  offline: 'var(--ink-3)',
  replay: 'var(--accent)',
};

export function Dot({ tone, live }: { tone: Tone; live?: boolean }) {
  return <span className={cx(s.dot, live && s.dotLive)} style={{ background: TONE_COLOR[tone], color: TONE_COLOR[tone] }} />;
}

export function Pill({ tone, children, live, title }: { tone: Tone; children: ReactNode; live?: boolean; title?: string }) {
  return (
    <span className={s.pill} title={title}>
      <Dot tone={tone} live={live} />
      {children}
    </span>
  );
}

export function Chip({ children, strong, title, style }: { children: ReactNode; strong?: boolean; title?: string; style?: React.CSSProperties }) {
  return (
    <span className={cx(s.chip, strong && s.chipStrong)} title={title} style={style}>
      {children}
    </span>
  );
}

export function Segmented<T extends string | number>(props: {
  options: { value: T; label: ReactNode }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  const id = useId();
  return (
    <div className={s.segmented} role="radiogroup" aria-label={props.label}>
      {props.options.map((o) => {
        const active = o.value === props.value;
        return (
          <button
            key={String(o.value)}
            role="radio"
            aria-checked={active}
            className={cx(s.segment, active && s.segmentActive)}
            onClick={() => props.onChange(o.value)}
          >
            {active && <motion.span layoutId={`thumb-${id}`} className={s.segmentThumb} transition={SPRING} />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1400);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <div className={s.copy}>
      <code>{command}</code>
      <button
        className={s.iconButton}
        aria-label={copied ? 'Copied' : 'Copy command'}
        title={copied ? 'Copied' : 'Copy'}
        onClick={() => {
          void navigator.clipboard?.writeText(command).then(() => setCopied(true));
        }}
      >
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={copied ? 'ok' : 'copy'}
            initial={{ scale: 0.6, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.6, opacity: 0 }}
            transition={{ duration: 0.15 }}
            style={{ display: 'grid' }}
          >
            {copied ? <Check size={15} color="var(--good)" /> : <Copy size={15} />}
          </motion.span>
        </AnimatePresence>
      </button>
    </div>
  );
}

/** A short value (a YAML key path, an ID) with a copy button. */
export function CopyChip({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1200);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <button
      className={s.copyChip}
      title={copied ? 'Copied' : `Copy ${label ?? text}`}
      onClick={() => void navigator.clipboard?.writeText(text).then(() => setCopied(true))}
    >
      <code>{text}</code>
      {copied ? <Check size={13} color="var(--good)" /> : <Copy size={13} />}
    </button>
  );
}

export function Empty({ title, body, art }: { title: ReactNode; body?: ReactNode; art?: string }) {
  return (
    <div className={s.empty}>
      {art && <img className={s.emptyArt} src={art} alt="" />}
      <span className={s.emptyTitle}>{title}</span>
      {body && <span className={s.emptyBody}>{body}</span>}
    </div>
  );
}

export function Monogram({ symbol }: { symbol: string }) {
  return <span className={s.monogram}>{symbol.slice(0, 4)}</span>;
}

/** Where the price sits between the stop and the target. */
export function RangeBar({ stop, price, target }: { stop: number; price: number; target: number }) {
  const span = target - stop;
  const at = span > 0 ? Math.max(0, Math.min(1, (price - stop) / span)) : 0.5;
  return (
    <div>
      <div className={s.range}>
        <motion.span className={s.rangeMark} initial={false} animate={{ left: `${at * 100}%` }} transition={SPRING} />
      </div>
      <div className={s.rangeLabels}>
        <span>Stop {stop.toFixed(2)}</span>
        <span>Target {target.toFixed(2)}</span>
      </div>
    </div>
  );
}

export function Sheet({ open, onClose, title, sub, children }: { open: boolean; onClose: () => void; title: ReactNode; sub?: ReactNode; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);
  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div className={s.sheetScrim} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} />
          <motion.aside
            className={s.sheet}
            role="dialog"
            aria-modal="true"
            initial={{ x: 40, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: 40, opacity: 0 }}
            transition={SPRING}
          >
            <header className={s.sheetHead}>
              <div>
                <h2 className={s.cardTitle} style={{ fontSize: 20 }}>{title}</h2>
                {sub && <p className={s.cardSub}>{sub}</p>}
              </div>
              <button className={s.iconButton} onClick={onClose} aria-label="Close">
                <X size={16} />
              </button>
            </header>
            <div className={s.sheetBody}>{children}</div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className={s.kv}>
      {items.map(([k, v], i) => (
        <div key={i} style={{ display: 'contents' }}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export const styles = s;
export { cx };
