import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { motion, useReducedMotion } from 'motion/react';
import { ArrowUpRight, Check, Copy, CornerDownLeft, FileText, Send } from 'lucide-react';
import {
  buildLaunch,
  goalProgress,
  newNoteId,
  type DeskAction,
  type DeskNote,
  type DeskPayload,
  type DeskSection,
  type DeskSession,
} from '../../shared/desk';
import { Card, Chip, CopyCommand, Empty, Odometer, Pill, type Tone } from '../design/primitives';
import { getDesk, getSession, postNote, postOpened, randomHex4, subscribe } from './api';
import { renderMarkdown } from './markdown';
import { SLEEVES } from '../../shared/mission';
import p from '../features/pages.module.css';
import s from './desk.module.css';

/** Opens the vscode:// link. The e2e script (e2e/desk.mjs) records it instead, via window.__deskOpen. */
function openInVsCode(uri: string): void {
  const hook = (window as unknown as { __deskOpen?: (u: string) => void }).__deskOpen;
  if (hook) hook(uri);
  else window.location.assign(uri);
}

const money = (n: number) => n.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const shortMoney = (n: number) => (n >= 1000 ? `$${(n / 1000).toLocaleString('en-US', { maximumFractionDigits: 1 })}k` : `$${n}`);

function when(iso: string | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** Short cell text: `code` in backticks and **bold**; nothing else is interpreted. */
function Inline({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith('`') && p.endsWith('`') ? (
          <code key={i}>{p.slice(1, -1)}</code>
        ) : p.startsWith('**') && p.endsWith('**') ? (
          <strong key={i}>{p.slice(2, -2)}</strong>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

// --------------------------------------------------------------------------- data
function useDesk() {
  const [session, setSession] = useState<DeskSession | null>(null);
  const [data, setData] = useState<DeskPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setData(await getDesk());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const refreshSession = useCallback(async () => {
    const next = await getSession();
    setSession(next);
    return next;
  }, []);

  useEffect(() => {
    refreshSession().catch((e: Error) => setError(e.message));
    void reload();
    const stop = subscribe(() => void reload());
    const timer = setInterval(() => void reload(), 30_000); // the goal number changes without a file event
    return () => {
      stop();
      clearInterval(timer);
    };
  }, [reload, refreshSession]);

  return { session, data, error, reload, refreshSession };
}

// --------------------------------------------------------------------------- page
export function DeskApp() {
  const { session, data, error, reload, refreshSession } = useDesk();
  const [draft, setDraft] = useState('');
  const composer = useRef<HTMLTextAreaElement>(null);

  const answer = (label: string) => {
    setDraft(`Decision: ${label}\nMy answer: `);
    composer.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => composer.current?.focus(), 250);
  };

  const sheet = data?.sheet ?? null;
  return (
    <div className={s.page}>
      <header className={p.head}>
        <div>
          <h1 className={p.title}>{sheet?.title ?? 'Desk'}</h1>
          <p className={p.subtitle}>
            Your setup sheet and notes for Claude. A note opens a new Claude Code chat in VS Code with your words typed in; nothing runs
            until you press Enter there.
          </p>
        </div>
        <nav className={s.links} aria-label="Links">
          {/* Links to the local console are the page you are on now; only outside links remain. */}
          {(sheet?.links ?? []).filter((l) => !/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?(\/|$)/i.test(l.href)).map((l) => (
            <a key={l.href} href={l.href} target="_blank" rel="noreferrer" className={s.link}>
              {l.label}
              <ArrowUpRight size={14} aria-hidden />
            </a>
          ))}
        </nav>
      </header>

      {error && (
        <div className={s.banner} role="alert">
          The Desk service is not answering ({error}). It runs inside the console: restart it with <code>npm run dev</code>.
        </div>
      )}

      <GoalCard data={data} />

      <div className={s.grid}>
        <div className={s.colMain}>
          <Card title="Notes for Claude" sub="Each note opens a new Claude Code tab in VS Code with your words typed in. Press Enter there to start.">
            <Composer
              session={session}
              draft={draft}
              setDraft={setDraft}
              inputRef={composer}
              onSent={() => void reload()}
              refreshSession={refreshSession}
            />
            <NoteList notes={data?.notes ?? []} session={session} />
          </Card>
        </div>
        <div className={s.colSide}>
          <Card title="Next actions" sub={sheet ? `What needs you, as of ${sheet.updated}` : undefined}>
            <Actions actions={sheet?.nextActions ?? []} />
          </Card>
        </div>
      </div>

      {data?.sheetError && <div className={s.banner}>{data.sheetError}</div>}
      {(sheet?.sections ?? []).map((section) => (
        <SectionTable key={section.id} section={section} onAnswer={answer} />
      ))}

      <footer className={s.foot}>
        Notes live in <code>.desk/notes/</code> (never committed). The Desk writes a note once; Claude adds the reply. Nothing runs until you press Enter in VS Code.
      </footer>
    </div>
  );
}

// --------------------------------------------------------------------------- goal
function GoalCard({ data }: { data: DeskPayload | null }) {
  const reduce = useReducedMotion();
  const sleeves = data?.goal.sleeves ?? [];
  const start = data?.sheet?.goal.start ?? (sleeves.length ? sleeves.reduce((a, x) => a + x.start, 0) : 300);
  const target = data?.sheet?.goal.target ?? 10_000;
  const equity = sleeves.length ? sleeves.reduce((a, x) => a + x.equity, 0) : data?.goal.equity != null ? Number(data.goal.equity) : null;
  const g = goalProgress(Number.isFinite(equity) ? equity : null, start, target);
  const source = data?.goal.source;
  const note =
    source === 'database'
      ? sleeves.find((x) => x.id === 'stocks')?.detail === 'bot equity snapshot'
        ? `The sum of the three sleeves; stocks from the bot's latest equity snapshot, ${when(data?.goal.at ?? undefined)}.`
        : `The sum of the three sleeves. Stocks shows its start because ${sleeves.find((x) => x.id === 'stocks')?.detail ?? 'the bot has not run yet'}.`
      : source === 'no-snapshots'
        ? `The paper bot has not run yet, so this shows the USD ${start.toLocaleString('en-US')} start.`
        : source === 'database-unavailable'
          ? `The bot database is not reachable right now; showing the USD ${start.toLocaleString('en-US')} start.`
          : `No database is configured for the console (ui/.env.local); showing the USD ${start.toLocaleString('en-US')} start.`;
  return (
    <Card className={s.goal}>
      <div className={s.goalHead}>
        <div>
          <div className={s.goalLabel}>
            Goal: {money(start)} → {money(target)} <Chip title="Paper trading uses simulated money">paper · simulated</Chip>
          </div>
          <div className={s.goalValue}>
            <Odometer text={money(g.equity)} />
          </div>
          <div className={s.goalMeta}>
            {g.belowStartBy > 0
              ? `−${money(g.belowStartBy)} below the start`
              : `${g.doublingsDone.toFixed(1)} of ${g.doublingsNeeded.toFixed(1)} doublings · ${(g.linearFraction * 100).toFixed(1)}% of the way in dollars`}
          </div>
        </div>
        <div className={s.goalPct}>
          <span className="num">{(g.fraction * 100).toFixed(1)}%</span>
          <span className={s.goalPctLabel}>of the doublings</span>
        </div>
      </div>
      <div className={s.track} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(g.fraction * 100)} aria-label="Progress to the goal, log scale">
        <motion.div
          className={s.fill}
          initial={reduce ? false : { width: '0%' }}
          animate={{ width: `${Math.max(g.fraction * 100, 0.6)}%` }}
          transition={reduce ? { duration: 0 } : { duration: 1.6, ease: [0.22, 1, 0.36, 1] }}
        >
          <span className={s.shimmer} />
        </motion.div>
        {g.ticks.map((t) => (
          <span key={t.value} className={s.tick} style={{ left: `${t.at * 100}%` }} />
        ))}
      </div>
      <div className={s.tickLabels} aria-hidden>
        <span style={{ left: '0%' }}>{shortMoney(start)}</span>
        {g.ticks.map((t) => (
          <span key={t.value} style={{ left: `${t.at * 100}%` }}>
            {shortMoney(t.value)}
          </span>
        ))}
      </div>
      {sleeves.length > 0 && (
        <div className={s.goalMeta} style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 22px', marginTop: 12 }}>
          {sleeves.map((x) => (
            <span key={x.id} title={x.detail} style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <img src={SLEEVES[x.id].emblem} alt="" width={24} height={24} style={{ objectFit: 'contain' }} />
              <span>
                {x.label}: <span className="num" style={{ color: 'var(--ink)', fontWeight: 600 }}>{money(x.equity)}</span> of {money(x.start)}
              </span>
            </span>
          ))}
        </div>
      )}
      <p className={s.goalNote}>
        {note} Each step on the bar is one doubling. {data?.sheet?.goal.label}
      </p>
    </Card>
  );
}

// --------------------------------------------------------------------------- composer
function Composer(props: {
  session: DeskSession | null;
  draft: string;
  setDraft: (v: string) => void;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  onSent: () => void;
  refreshSession: () => Promise<DeskSession>;
}) {
  const { session, draft, setDraft, inputRef, onSent, refreshSession } = props;
  const [status, setStatus] = useState<{ tone: 'ok' | 'error'; text: ReactNode } | null>(null);
  const [busy, setBusy] = useState(false);

  const save = async (id: string, text: string): Promise<boolean> => {
    let current = session!;
    let r = await postNote(current, id, text);
    if (r.status === 403) {
      current = await refreshSession(); // the Desk server restarted: new token
      r = await postNote(current, id, text);
    }
    if (!r.ok) {
      const body = (await r.json().catch(() => ({}))) as { error?: string };
      throw new Error(body.error ?? `Desk server answered ${r.status}`);
    }
    return true;
  };

  const send = () => {
    const text = draft.trim();
    if (!text || !session || busy) return;
    const id = newNoteId(new Date(), randomHex4());
    const notePath = `${session.notesDir}${session.sep}${id}.md`;
    const launch = buildLaunch(id, text, notePath);
    const opened = (
      <>
        Opened in VS Code: press <kbd>Enter</kbd> in the new Claude tab.{' '}
        <a href={launch.uri}>Open again</a>
      </>
    );
    setBusy(true);
    const saving = save(id, text);
    if (launch.mode === 'inline') {
      // Open inside the click so the browser keeps the user's gesture; the save runs alongside.
      openInVsCode(launch.uri);
      setDraft('');
      setStatus({ tone: 'ok', text: opened });
    }
    saving
      .then(() => {
        if (launch.mode === 'pointer') {
          openInVsCode(launch.uri);
          setDraft('');
          setStatus({ tone: 'ok', text: <>Long note saved; Claude reads it from the file. {opened}</> });
        }
        onSent();
      })
      .catch((e: Error) => {
        setDraft(text);
        setStatus({ tone: 'error', text: `Not saved: ${e.message}. Your text is back in the box.` });
      })
      .finally(() => setBusy(false));
  };

  return (
    <div className={s.composer}>
      <textarea
        ref={inputRef}
        className={s.input}
        placeholder="Ask Claude anything: a question, a change of plan, a decision…"
        value={draft}
        rows={4}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            send();
          }
        }}
        aria-label="Note for Claude"
      />
      <div className={s.composerBar}>
        <span className={s.hint}>
          <kbd>Ctrl</kbd> <kbd>Enter</kbd> to send · the first time, allow the browser to open VS Code
        </span>
        <button className={s.send} onClick={send} disabled={!draft.trim() || !session || busy}>
          <Send size={15} aria-hidden /> Send to Claude
        </button>
      </div>
      {status && (
        <p className={status.tone === 'error' ? s.statusError : s.statusOk} role="status">
          {status.text}
        </p>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- notes
const STATUS: Record<string, { tone: Tone; label: string }> = {
  new: { tone: 'idle', label: 'New' },
  opened: { tone: 'replay', label: 'Opened' },
  in_progress: { tone: 'paused', label: 'In progress' },
  done: { tone: 'good', label: 'Done' },
};

function NoteList({ notes, session }: { notes: DeskNote[]; session: DeskSession | null }) {
  if (!notes.length) return <Empty title="No notes yet" body="Your first note will appear here, with Claude's reply once it is done." />;
  return (
    <ol className={s.notes}>
      {notes.map((n) => (
        <NoteItem key={n.id} note={n} session={session} />
      ))}
    </ol>
  );
}

function NoteItem({ note, session }: { note: DeskNote; session: DeskSession | null }) {
  const [open, setOpen] = useState(false);
  const status = note.status === 'new' && note.opened ? 'opened' : note.status;
  const st = STATUS[status] ?? { tone: 'neutral' as Tone, label: status };
  const launch = useMemo(() => buildLaunch(note.id, note.text, note.path), [note.id, note.text, note.path]);
  const long = note.text.length > 360 || note.text.split('\n').length > 6;
  return (
    <li className={s.note}>
      <div className={s.noteHead}>
        <Pill tone={st.tone}>{st.label}</Pill>
        <span className={s.noteWhen}>{when(note.created)}</span>
        <span className={s.noteActions}>
          <a href={launch.uri} className={s.reopen} onClick={() => session && postOpened(session, note.id)} title="Open a new Claude Code tab with this note">
            <CornerDownLeft size={13} aria-hidden /> Reopen in Claude
          </a>
          <CopyPath path={note.path} />
        </span>
      </div>
      <p className={`${s.noteText} ${long && !open ? s.clamp : ''}`}>{note.text || '(empty)'}</p>
      {long && (
        <button className={s.more} onClick={() => setOpen((v) => !v)}>
          {open ? 'Show less' : 'Show all'}
        </button>
      )}
      {note.reply ? (
        <div className={s.reply}>
          <div className={s.replyHead}>
            <FileText size={13} aria-hidden /> Claude
          </div>
          <div className={s.markdown} dangerouslySetInnerHTML={{ __html: renderMarkdown(note.reply) }} />
        </div>
      ) : null}
    </li>
  );
}

/** A short button: the full Windows path is too wide to show on a phone. */
function CopyPath({ path }: { path: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1200);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <button className={s.copyPath} title={path} onClick={() => void navigator.clipboard?.writeText(path).then(() => setCopied(true))}>
      {copied ? <Check size={13} color="var(--good)" aria-hidden /> : <Copy size={13} aria-hidden />}
      {copied ? 'Copied' : 'Copy path'}
    </button>
  );
}

// --------------------------------------------------------------------------- actions and tables
function Actions({ actions }: { actions: DeskAction[] }) {
  if (!actions.length) return <Empty title="Nothing waiting on you" />;
  return (
    <ol className={s.actions}>
      {actions.map((a, i) => (
        <li key={i} className={`${s.action} ${a.status === 'done' ? s.actionDone : ''}`}>
          <span className={s.actionNum}>{i + 1}</span>
          <div>
            <div className={s.actionTitle}>
              <Inline text={a.title} />
              {a.status === 'doing' && <Chip>in progress</Chip>}
              {a.status === 'done' && <Chip>done</Chip>}
            </div>
            <p className={s.actionWhy}>
              <Inline text={a.why} />
            </p>
            {a.command && <CopyCommand command={a.command} />}
          </div>
        </li>
      ))}
    </ol>
  );
}

function SectionTable({ section, onAnswer }: { section: DeskSection; onAnswer: (label: string) => void }) {
  return (
    <Card title={section.title} sub={section.lead}>
      <div className={s.tableWrap}>
        <table className={s.table}>
          <thead>
            <tr>
              {section.columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
              {section.answerable && <th aria-label="Answer" />}
            </tr>
          </thead>
          <tbody>
            {section.rows.map((row, i) => (
              <tr key={i}>
                {section.columns.map((_, j) => (
                  <td key={j}>
                    <Inline text={row[j] ?? ''} />
                  </td>
                ))}
                {section.answerable && (
                  <td className={s.answerCell}>
                    {!(row[section.columns.length - 1] ?? '').trim() && (
                      <button className={s.answer} onClick={() => onAnswer(row[0] ?? '')}>
                        Answer
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** The Desk as a console page (route /desk). */
export default DeskApp;
