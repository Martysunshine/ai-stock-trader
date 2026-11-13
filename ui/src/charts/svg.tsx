/**
 * Small purpose-built SVG charts. Mark specs follow the dataviz rules: bars <= 24px thick with a
 * 4px rounded data end, 2px lines, recessive hairline grid, a hover tooltip on every mark, and a
 * table view for anyone who prefers numbers.
 */
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { motion } from 'motion/react';
import { scaleBand, scaleLinear } from 'd3-scale';
import { SPRING } from '../design/primitives';
import s from './svg.module.css';

function useWidth(initial = 600) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(initial);
  const obs = useRef<ResizeObserver | null>(null);
  const setRef = (node: HTMLDivElement | null) => {
    obs.current?.disconnect();
    (ref as React.MutableRefObject<HTMLDivElement | null>).current = node;
    if (node) {
      obs.current = new ResizeObserver(([e]) => setW(Math.max(120, e.contentRect.width)));
      obs.current.observe(node);
    }
  };
  return [setRef, w] as const;
}

interface Tip {
  x: number;
  y: number;
  content: ReactNode;
}

/** Horizontal bars, sorted by the caller. Value at the tip. */
export function HBars({
  data,
  color = 'var(--series-1)',
  format = (n: number) => String(n),
  rowHeight = 30,
}: {
  data: { label: string; value: number; hint?: string }[];
  color?: string;
  format?: (n: number) => string;
  rowHeight?: number;
}) {
  const [ref, width] = useWidth();
  const [tip, setTip] = useState<Tip | null>(null);
  const labelW = Math.min(180, width * 0.38);
  const valueW = 44;
  const max = Math.max(1, ...data.map((d) => d.value));
  const x = scaleLinear().domain([0, max]).range([0, Math.max(10, width - labelW - valueW)]);
  const h = data.length * rowHeight;
  return (
    <div className={s.wrap} ref={ref}>
      <svg className={s.svg} height={h} role="img" aria-label="Bar chart">
        {data.map((d, i) => {
          const y = i * rowHeight;
          const bw = Math.max(2, x(d.value));
          const bh = Math.min(16, rowHeight - 12);
          return (
            <g
              key={d.label}
              className={s.row}
              onMouseMove={(e) => {
                const r = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                setTip({ x: e.clientX - r.left, y: e.clientY - r.top, content: <><b>{d.label}</b> · {format(d.value)}{d.hint && <div className={s.tipMuted}>{d.hint}</div>}</> });
              }}
              onMouseLeave={() => setTip(null)}
            >
              <rect className={s.hit} x={0} y={y} width={width} height={rowHeight} />
              <text className={s.label} x={0} y={y + rowHeight / 2} dominantBaseline="middle">
                {d.label}
              </text>
              <motion.path
                className={s.mark}
                fill={color}
                initial={false}
                animate={{ d: barPath(labelW, y + (rowHeight - bh) / 2, bw, bh) }}
                transition={SPRING}
              />
              <text className={s.value} x={labelW + bw + 8} y={y + rowHeight / 2} dominantBaseline="middle">
                {format(d.value)}
              </text>
            </g>
          );
        })}
      </svg>
      {tip && <div className={s.tip} style={{ left: tip.x, top: tip.y }}>{tip.content}</div>}
    </div>
  );
}

/** Horizontal bar with square start and a 4px rounded data end. */
function barPath(x: number, y: number, w: number, h: number) {
  const r = Math.min(4, w / 2, h / 2);
  return `M${x},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} H${x} Z`;
}

/** Vertical columns around a zero baseline (gains up, losses down). */
export function Columns({
  data,
  format,
  height = 180,
}: {
  data: { label: string; value: number; hint?: string }[];
  format: (n: number) => string;
  height?: number;
}) {
  const [ref, width] = useWidth();
  const [tip, setTip] = useState<Tip | null>(null);
  const padL = 44;
  const padB = 22;
  const ext = Math.max(0.01, ...data.map((d) => Math.abs(d.value)));
  const y = scaleLinear().domain([-ext, ext]).nice().range([height - padB, 6]);
  const x = scaleBand<string>().domain(data.map((d) => d.label)).range([padL, width]).paddingInner(0.3).paddingOuter(0.1);
  const bw = Math.min(24, x.bandwidth());
  const ticks = y.ticks(4);
  // Thin the category labels so they never overprint (about 52px per label).
  const every = Math.max(1, Math.ceil((data.length * 52) / Math.max(1, width - padL)));
  return (
    <div className={s.wrap} ref={ref}>
      <svg className={s.svg} height={height} role="img" aria-label="Column chart">
        {ticks.map((t) => (
          <g key={t}>
            <line className={t === 0 ? s.base : s.grid} x1={padL} x2={width} y1={y(t)} y2={y(t)} />
            <text className={s.axis} x={padL - 8} y={y(t)} textAnchor="end" dominantBaseline="middle">
              {format(t)}
            </text>
          </g>
        ))}
        {data.map((d) => {
          const cx = (x(d.label) ?? 0) + x.bandwidth() / 2;
          const y0 = y(0);
          const y1 = y(d.value);
          const top = Math.min(y0, y1);
          const hgt = Math.max(1.5, Math.abs(y1 - y0));
          const up = d.value >= 0;
          const r = Math.min(4, hgt / 2, bw / 2);
          const path = up
            ? `M${cx - bw / 2},${y0} V${top + r} Q${cx - bw / 2},${top} ${cx - bw / 2 + r},${top} H${cx + bw / 2 - r} Q${cx + bw / 2},${top} ${cx + bw / 2},${top + r} V${y0} Z`
            : `M${cx - bw / 2},${y0} V${top + hgt - r} Q${cx - bw / 2},${top + hgt} ${cx - bw / 2 + r},${top + hgt} H${cx + bw / 2 - r} Q${cx + bw / 2},${top + hgt} ${cx + bw / 2},${top + hgt - r} V${y0} Z`;
          return (
            <g
              key={d.label}
              onMouseMove={(e) => {
                const rr = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                setTip({ x: e.clientX - rr.left, y: e.clientY - rr.top, content: <><b>{d.label}</b> · {format(d.value)}{d.hint && <div className={s.tipMuted}>{d.hint}</div>}</> });
              }}
              onMouseLeave={() => setTip(null)}
            >
              <rect className={s.hit} x={x(d.label)} y={0} width={x.bandwidth()} height={height - padB} />
              <motion.path initial={false} animate={{ d: path }} transition={SPRING} fill={up ? 'var(--gain-mark)' : 'var(--loss-mark)'} />
              {data.indexOf(d) % every === 0 && (
                <text className={s.axis} x={cx} y={height - 6} textAnchor="middle">
                  {d.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {tip && <div className={s.tip} style={{ left: tip.x, top: tip.y }}>{tip.content}</div>}
    </div>
  );
}

/**
 * Confidence intervals against zero: "is the lower bound above zero?" is the whole question G2
 * asks, so zero is drawn as the baseline and each interval as a bar with its estimate as a dot.
 */
export function IntervalPlot({
  rows,
  format,
}: {
  rows: { label: string; lower: number; estimate: number; upper: number; hint?: string }[];
  format: (n: number) => string;
}) {
  const [ref, width] = useWidth();
  const [tip, setTip] = useState<Tip | null>(null);
  const rowH = 34;
  const labelW = Math.min(150, width * 0.3);
  const lo = Math.min(0, ...rows.map((r) => r.lower));
  const hi = Math.max(0, ...rows.map((r) => r.upper));
  const pad = (hi - lo) * 0.08 || 0.01;
  const x = scaleLinear().domain([lo - pad, hi + pad]).nice().range([labelW, width - 8]);
  const h = rows.length * rowH + 22;
  return (
    <div className={s.wrap} ref={ref}>
      <svg className={s.svg} height={h} role="img" aria-label="Confidence intervals">
        {x.ticks(5).map((t) => (
          <g key={t}>
            <line className={t === 0 ? s.base : s.grid} x1={x(t)} x2={x(t)} y1={0} y2={rows.length * rowH} />
            <text className={s.axis} x={x(t)} y={h - 4} textAnchor="middle">
              {format(t)}
            </text>
          </g>
        ))}
        {rows.map((r, i) => {
          const y = i * rowH + rowH / 2;
          const above = r.lower > 0;
          const color = above ? 'var(--gain-mark)' : r.upper < 0 ? 'var(--loss-mark)' : 'var(--ink-3)';
          return (
            <g
              key={r.label}
              onMouseMove={(e) => {
                const rr = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                setTip({
                  x: e.clientX - rr.left,
                  y: e.clientY - rr.top,
                  content: (
                    <>
                      <b>{r.label}</b> · estimate {format(r.estimate)}
                      <div className={s.tipMuted}>
                        interval {format(r.lower)} to {format(r.upper)}
                        {r.hint ? ` · ${r.hint}` : ''}
                      </div>
                    </>
                  ),
                });
              }}
              onMouseLeave={() => setTip(null)}
            >
              <rect className={s.hit} x={0} y={y - rowH / 2} width={width} height={rowH} />
              <text className={s.label} x={0} y={y} dominantBaseline="middle">
                {r.label}
              </text>
              <motion.rect
                initial={false}
                animate={{ x: x(r.lower), width: Math.max(2, x(r.upper) - x(r.lower)) }}
                transition={SPRING}
                y={y - 3}
                height={6}
                rx={3}
                fill={color}
                opacity={0.35}
              />
              <motion.circle initial={false} animate={{ cx: x(r.estimate) }} transition={SPRING} cy={y} r={5} fill={color} stroke="var(--surface)" strokeWidth={2} />
            </g>
          );
        })}
      </svg>
      {tip && <div className={s.tip} style={{ left: tip.x, top: tip.y }}>{tip.content}</div>}
    </div>
  );
}

/** 2px sparkline; the last point is marked. Used inside stat tiles. */
export function Sparkline({ values, width = 120, height = 32, color = 'var(--accent)' }: { values: number[]; width?: number; height?: number; color?: string }) {
  const path = useMemo(() => {
    if (values.length < 2) return null;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const pts = values.map((v, i) => [(i / (values.length - 1)) * (width - 6) + 3, height - 3 - ((v - min) / span) * (height - 6)]);
    return { d: pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' '), last: pts[pts.length - 1] };
  }, [values, width, height]);
  if (!path) return <svg width={width} height={height} aria-hidden />;
  return (
    <svg width={width} height={height} aria-hidden style={{ overflow: 'visible' }}>
      <path d={path.d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={path.last[0]} cy={path.last[1]} r={3.5} fill={color} stroke="var(--surface)" strokeWidth={2} />
    </svg>
  );
}

export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className={s.legend}>
      {items.map((i) => (
        <span key={i.label} className={s.key}>
          <span className={s.swatch} style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  );
}
