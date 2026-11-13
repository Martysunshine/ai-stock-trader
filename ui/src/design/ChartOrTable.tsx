import { useState, type ReactNode } from 'react';
import { BarChart3, Table2 } from 'lucide-react';
import { styles as ps } from './primitives';

/**
 * Every chart can be read as a table: the exact numbers behind the marks, for anyone who prefers
 * them, for colour-vision differences, and for copying.
 */
export function ChartOrTable({
  chart,
  columns,
  rows,
  label,
}: {
  chart: ReactNode;
  columns: { key: string; label: string; align?: 'right' }[];
  rows: Record<string, ReactNode>[];
  label: string;
}) {
  const [table, setTable] = useState(false);
  return (
    <div style={{ position: 'relative' }}>
      <button
        className={ps.iconButton}
        style={{ position: 'absolute', top: -46, right: -6, zIndex: 2 }}
        onClick={() => setTable((t) => !t)}
        aria-pressed={table}
        aria-label={table ? `Show ${label} as a chart` : `Show ${label} as a table`}
        title={table ? 'Show as chart' : 'Show as table'}
      >
        {table ? <BarChart3 size={16} /> : <Table2 size={16} />}
      </button>
      {table ? (
        <div style={{ maxHeight: 360, overflow: 'auto' }}>
          <table className={ps.table} aria-label={label}>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.key} className={c.align === 'right' ? ps.r : undefined}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td key={c.key} className={c.align === 'right' ? ps.r : undefined}>
                      {r[c.key]}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        chart
      )}
    </div>
  );
}
