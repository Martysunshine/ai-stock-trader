import { Database, FlaskConical, PlugZap } from 'lucide-react';
import { useConsole } from '../app/store';
import { CopyCommand } from '../design/primitives';
import p from './pages.module.css';

/** Explains, in one place, why the live view is empty and what to do about it. */
export function SourceNotice() {
  const status = useConsole((s) => s.status);
  const setSource = useConsole((s) => s.setSource);
  if (!status) return null;

  if (status.kind === 'replay') {
    const r = status.replay;
    return (
      <div className={p.banner} style={{ background: 'var(--accent-wash)', boxShadow: 'none', padding: '10px 16px', alignItems: 'center', borderRadius: 14, marginBottom: 0 }}>
        <FlaskConical className={p.bannerIcon} size={16} color="var(--accent-ink)" style={{ marginTop: 0 }} />
        <div className={p.small} style={{ flex: 1, minWidth: 0 }}>
          <b>Replay · {r?.scenario}</b>
          {r?.notes?.[0] ? ` — ${r.notes[0]}. ` : '. '}
          <span className={p.muted}>Synthetic recorded session; not evidence of profitability.</span>
        </div>
      </div>
    );
  }

  if (status.state === 'ok') return null;

  const replayButton = (
    <button
      onClick={() => setSource('replay:baseline')}
      style={{ alignSelf: 'flex-start', border: 0, background: 'var(--accent)', color: 'var(--on-accent)', borderRadius: 999, padding: '7px 14px', fontWeight: 600, fontSize: 13, cursor: 'pointer' }}
    >
      Watch a recorded session instead
    </button>
  );

  if (status.state === 'not_configured') {
    return (
      <div className={p.banner}>
        <PlugZap className={p.bannerIcon} size={20} color="var(--warning-ink)" />
        <div className={p.bannerBody}>
          <div className={p.bannerTitle}>Live data is not connected yet</div>
          <div className={p.small}>
            The console reads the paper bot's database through a SELECT-only role. Create it once (PowerShell, from the repository root), then restart the console:
          </div>
          <CopyCommand command="powershell -ExecutionPolicy Bypass -File ui\scripts\create-readonly-role.ps1" />
          {replayButton}
        </div>
      </div>
    );
  }

  if (status.state === 'uninitialized') {
    return (
      <div className={p.banner} style={{ background: 'var(--surface)', boxShadow: 'inset 0 0 0 0.5px var(--hairline)' }}>
        <Database className={p.bannerIcon} size={20} color="var(--ink-2)" />
        <div className={p.bannerBody}>
          <div className={p.bannerTitle}>Connected. The paper bot has not run yet.</div>
          <div className={p.small}>It creates its tables on first start; this view fills in from then on.</div>
          <CopyCommand command="uv run trader paper run --config configs/paper.yaml" />
          {replayButton}
        </div>
      </div>
    );
  }

  return (
    <div className={`${p.banner} ${p.bannerCritical}`}>
      <Database className={p.bannerIcon} size={20} color="var(--critical)" />
      <div className={p.bannerBody}>
        <div className={p.bannerTitle}>Database unreachable</div>
        <div className={p.small}>{status.message}</div>
        <CopyCommand command="docker compose up -d" />
        {replayButton}
      </div>
    </div>
  );
}
