import { useState, type ReactNode } from 'react';
import type { Job } from '../lib/types';
import { fmtPct, fmtTime, humanize } from '../lib/format';

export type Tone = 'ok' | 'info' | 'warn' | 'bad' | 'purple' | 'teal' | 'muted' | 'neutral';

const TONES: Record<string, Tone> = {
  // generic
  ok: 'ok',
  done: 'ok',
  verified: 'ok',
  completed: 'ok',
  approved: 'ok',
  accepted: 'ok',
  allowed: 'ok',
  operator_verified: 'ok',
  geometry_consistent: 'ok',
  // in progress
  running: 'info',
  queued: 'info',
  captured: 'info',
  idle: 'info',
  starting: 'info',
  draft: 'info',
  direct: 'info',
  // attention
  needs_review: 'warn',
  paused: 'warn',
  manual_required: 'warn',
  awaiting_operator: 'warn',
  warning: 'warn',
  uncertain: 'warn',
  stalled: 'warn',
  pending: 'warn',
  unresolved: 'warn',
  manual_intervention: 'warn',
  limit: 'warn',
  // problems
  failed: 'bad',
  rejected: 'bad',
  error: 'bad',
  blocked: 'bad',
  missing: 'bad',
  // special
  no_imagery: 'purple',
  reused: 'teal',
  // inert
  planned: 'muted',
  cancelled: 'muted',
  stopped: 'muted',
  superseded: 'muted',
  excluded: 'muted',
  none: 'muted',
  unconfigured: 'muted',
  skipped: 'muted',
  info: 'neutral',
};

export function toneOf(status: string | null | undefined): Tone {
  if (!status) return 'muted';
  return TONES[status] ?? (status.startsWith('reused') ? 'teal' : 'neutral');
}

export function Chip({ value, tone, children, title, testid }: { value?: string | null; tone?: Tone; children?: ReactNode; title?: string; testid?: string }) {
  const t = tone ?? toneOf(value);
  return (
    <span className={`chip chip-${t}`} title={title} data-testid={testid}>
      {children ?? humanize(value)}
    </span>
  );
}

export function ErrorList({ errors, title }: { errors: string[] | null | undefined; title?: string }) {
  if (!errors || errors.length === 0) return null;
  return (
    <div className="notice notice-bad" role="alert">
      {title && <strong>{title}</strong>}
      {errors.length === 1 && !title ? (
        <span>{errors[0]}</span>
      ) : (
        <ul>
          {errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Notice({ tone = 'info', children, testid }: { tone?: 'info' | 'warn' | 'bad' | 'ok'; children: ReactNode; testid?: string }) {
  return (
    <div className={`notice notice-${tone}`} data-testid={testid} role={tone === 'bad' ? 'alert' : undefined}>
      {children}
    </div>
  );
}

export function Section({ title, actions, children, className, testid }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; testid?: string }) {
  return (
    <section className={`panel ${className ?? ''}`} data-testid={testid}>
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2>{title}</h2>}
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Field({ label, hint, children, htmlFor, error, inline }: { label: ReactNode; hint?: ReactNode; children: ReactNode; htmlFor?: string; error?: string | null; inline?: boolean }) {
  return (
    <div className={`field ${inline ? 'field-inline' : ''} ${error ? 'field-error' : ''}`}>
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
      {error && <div className="field-msg">{error}</div>}
    </div>
  );
}

export function Progress({ value, label, sub, testid, tone = 'info' }: { value: number | null | undefined; label: ReactNode; sub?: ReactNode; testid?: string; tone?: 'info' | 'ok' | 'teal' | 'warn' }) {
  const v = typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
  return (
    <div className="progress" data-testid={testid} data-value={typeof value === 'number' ? value : ''}>
      <div className="progress-label">
        <span>{label}</span>
        <strong>{fmtPct(value)}</strong>
      </div>
      <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v * 100)}>
        <div className={`progress-fill progress-${tone}`} style={{ width: `${v * 100}%` }} />
      </div>
      {sub && <div className="hint">{sub}</div>}
    </div>
  );
}

export function KV({ rows, className }: { rows: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={`kv ${className ?? ''}`}>
      {rows.map(([k, v], i) => (
        <div className="kv-row" key={i}>
          <dt>{k}</dt>
          <dd>{v === null || v === undefined || v === '' ? <span className="muted">—</span> : v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function JsonBlock({ value, summary = 'JSON', open = false }: { value: unknown; summary?: ReactNode; open?: boolean }) {
  return (
    <details className="json" open={open}>
      <summary>{summary}</summary>
      <pre>{value === undefined ? 'undefined' : JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading({ what }: { what?: string }) {
  return <div className="loading">Loading{what ? ` ${what}` : ''}…</div>;
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <code className="mono" title={title}>
      {children}
    </code>
  );
}

export function Copyable({ text }: { text: string | null | undefined }) {
  const [copied, setCopied] = useState(false);
  if (!text) return <span className="muted">—</span>;
  return (
    <span className="copyable">
      <code className="mono">{text}</code>
      <button
        type="button"
        className="btn-link"
        onClick={() => {
          void navigator.clipboard?.writeText(text).then(
            () => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1200);
            },
            () => undefined,
          );
        }}
      >
        {copied ? 'copied' : 'copy'}
      </button>
    </span>
  );
}

export function JobSelect({ jobs, value, onChange, testid, allowNone, noneLabel }: { jobs: Job[] | undefined; value: string | null; onChange: (id: string | null) => void; testid?: string; allowNone?: boolean; noneLabel?: string }) {
  return (
    <select data-testid={testid} value={value ?? ''} onChange={(e) => onChange(e.target.value || null)} aria-label="Job">
      {(allowNone || !value) && <option value="">{noneLabel ?? (jobs && jobs.length ? 'Select a job…' : 'No jobs yet')}</option>}
      {(jobs ?? []).map((j) => (
        <option key={j.id} value={j.id}>
          {j.name || j.id.slice(0, 8)} — {j.status} ({fmtTime(j.created_at)})
        </option>
      ))}
    </select>
  );
}

export function CountChips({ counts, order, testid }: { counts: Record<string, number> | null | undefined; order: string[]; testid?: string }) {
  const c = counts ?? {};
  const keys = [...order, ...Object.keys(c).filter((k) => !order.includes(k))];
  return (
    <div className="chips" data-testid={testid}>
      {keys.map((k) => (
        <span key={k} className={`count-chip ${c[k] ? `chip-${toneOf(k)}` : 'count-zero'}`} title={humanize(k)}>
          <span className="count-label">{humanize(k)}</span>
          <span className="count-value">{c[k] ?? 0}</span>
        </span>
      ))}
    </div>
  );
}

export function confirmAction(message: string): boolean {
  return window.confirm(message);
}
