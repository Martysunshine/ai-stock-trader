import type { DecisionRow } from '../../../shared/schema';
import { decisionVerdict } from '../../../shared/verdict';
import { Chip, Dot } from '../../design/primitives';

/** Skipped (routine, quiet) or Blocked (the system was stopped: amber), with the reason on hover. */
export function VerdictChip({ decision }: { decision: DecisionRow }) {
  const v = decisionVerdict(decision);
  if (v.verdict === 'blocked') {
    return (
      <Chip strong title={`Blocked by the system: ${v.why}`} style={{ color: 'var(--warning-ink)' }}>
        <Dot tone="paused" /> Blocked
      </Chip>
    );
  }
  return <Chip title={v.why}>{v.label}</Chip>;
}
