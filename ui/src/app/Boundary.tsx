import { Component, type ReactNode } from 'react';

/** Keeps one failing view (or a chunk that failed to load) from blanking the whole console. */
export class Boundary extends Component<{ children: ReactNode; fallback?: ReactNode; resetKey?: string }, { error: Error | null; key?: string }> {
  state: { error: Error | null; key?: string } = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  static getDerivedStateFromProps(props: { resetKey?: string }, state: { error: Error | null; key?: string }) {
    if (props.resetKey !== state.key) return { error: null, key: props.resetKey };
    return null;
  }

  render() {
    if (!this.state.error) return this.props.children;
    if (this.props.fallback !== undefined) return this.props.fallback;
    return (
      <div style={{ padding: 32, borderRadius: 22, background: 'var(--surface)', boxShadow: 'inset 0 0 0 0.5px var(--hairline)' }}>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>This view failed to load</div>
        <div style={{ fontSize: 13, color: 'var(--ink-2)', marginBottom: 12 }}>{this.state.error.message}</div>
        <button
          onClick={() => location.reload()}
          style={{ border: 0, background: 'var(--accent)', color: 'var(--on-accent)', borderRadius: 999, padding: '7px 14px', fontWeight: 600, cursor: 'pointer' }}
        >
          Reload
        </button>
      </div>
    );
  }
}
