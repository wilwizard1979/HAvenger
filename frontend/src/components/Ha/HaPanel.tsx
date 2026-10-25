import type { ReactNode } from 'react';
import {
  AlertTriangle,
  RefreshCw,
  Download,
  Plug,
  Boxes,
  Radar,
  Workflow,
  DatabaseBackup,
  Terminal,
  CalendarClock,
  MessageSquareText,
} from 'lucide-react';
import { useHaDashboard, type HaDashboard } from '../../lib/ha';

type Tone = 'ok' | 'warn' | 'bad' | 'idle';

const TONE_COLOR: Record<Tone, string> = {
  ok: 'var(--color-success)',
  warn: 'var(--color-warning)',
  bad: 'var(--color-error)',
  idle: 'var(--color-text-tertiary)',
};

function Card({
  icon: Icon,
  label,
  value,
  unit,
  sub,
  tone,
  children,
  wide = false,
}: {
  icon: typeof AlertTriangle;
  label: string;
  value?: string | number;
  unit?: string;
  sub?: string;
  tone: Tone;
  children?: ReactNode;
  wide?: boolean;
}) {
  return (
    <div
      className={`hud-panel p-3 flex flex-col gap-1 min-w-0 ${wide ? 'col-span-2' : ''}`}
      style={{ borderLeft: `2px solid ${TONE_COLOR[tone]}` }}
    >
      <div className="hud-label flex items-center gap-1.5">
        <Icon size={12} style={{ color: TONE_COLOR[tone] }} />
        <span className="truncate">{label}</span>
      </div>
      {value !== undefined && (
        <div className="flex items-baseline gap-1">
          <span className="text-2xl font-semibold hud-mono" style={{ color: TONE_COLOR[tone] }}>
            {value}
          </span>
          {unit && (
            <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {unit}
            </span>
          )}
        </div>
      )}
      {sub && (
        <div className="text-[11px] leading-snug" style={{ color: 'var(--color-text-secondary)' }}>
          {sub}
        </div>
      )}
      {children}
    </div>
  );
}

const toneFor = (n: number, warnAt = 1, badAt = 10): Tone => (n >= badAt ? 'bad' : n >= warnAt ? 'warn' : 'ok');
const pct = (a: number, b: number) => (b > 0 ? Math.round((a / b) * 100) : 0);

function Cards({ d }: { d: HaDashboard }) {
  const r = d.repairs;
  const e = d.entities;
  const i = d.integrations;
  const m = d.motion;
  const l = d.logs;

  return (
    <div className="grid grid-cols-2 gap-2">
      {/* 1. What is broken */}
      {r && (
        <Card
          icon={AlertTriangle}
          label="Repairs"
          value={r.total}
          sub={`${r.errors} errors · ${r.warnings} warnings`}
          tone={r.errors > 0 ? 'bad' : r.warnings > 0 ? 'warn' : 'ok'}
        />
      )}
      {r && (
        <Card
          icon={Workflow}
          label="Automation errors"
          value={r.automation_errors}
          sub={`of ${e?.automations_total ?? '?'} · ${e?.automations_unavailable ?? 0} unavailable`}
          tone={toneFor(r.automation_errors, 1, 5)}
        />
      )}
      {i && (
        <Card
          icon={Plug}
          label="Integrations failed"
          value={i.failed}
          sub={`of ${i.total} · ${Object.entries(i.states)
            .filter(([k]) => k !== 'loaded')
            .map(([k, v]) => `${v} ${k.replace('_', ' ')}`)
            .join(', ') || 'all loaded'}`}
          tone={toneFor(i.failed, 1, 5)}
        />
      )}
      {e && (
        <Card
          icon={Download}
          label="Updates open"
          value={e.updates_available}
          sub={e.updates_available ? 'install after a backup' : 'everything up to date'}
          tone={toneFor(e.updates_available, 1, 30)}
        />
      )}

      {/* 2. Devices */}
      {e && (
        <Card
          icon={Boxes}
          label="Entities"
          value={e.unavailable}
          unit={`unavailable / ${e.total.toLocaleString()}`}
          tone={toneFor(pct(e.unavailable, e.total), 1, 20)}
          wide
        >
          <div className="h-1.5 rounded-full overflow-hidden mt-1" style={{ background: 'var(--color-bg-tertiary)' }}>
            <div
              className="h-full"
              style={{
                width: `${pct(e.unavailable, e.total)}%`,
                background: TONE_COLOR[toneFor(pct(e.unavailable, e.total), 1, 20)],
              }}
            />
          </div>
          <div className="text-[11px] mt-1" style={{ color: 'var(--color-text-secondary)' }}>
            {pct(e.unavailable, e.total)}% unavailable · worst:{' '}
            {e.worst_domains
              .slice(0, 4)
              .map((w) => `${w.domain} ${w.unavailable}`)
              .join(' · ')}
          </div>
        </Card>
      )}
      {m && (
        <Card
          icon={Radar}
          label="Motion offline"
          value={m.offline}
          unit={`/ ${m.total}`}
          tone={toneFor(m.offline, 1, 5)}
          wide
        >
          {m.offline_entities.length > 0 && (
            <ul className="text-[11px] mt-1 space-y-0.5" style={{ color: 'var(--color-text-secondary)' }}>
              {m.offline_entities.slice(0, 5).map((s) => (
                <li key={s.entity_id} className="truncate" title={s.entity_id}>
                  {s.name || s.entity_id}
                </li>
              ))}
              {m.offline_entities.length > 5 && <li>+{m.offline_entities.length - 5} more</li>}
            </ul>
          )}
        </Card>
      )}

      {/* 3. Logs and risks */}
      {l && (
        <Card
          icon={Terminal}
          label="Log errors"
          value={l.critical + l.errors}
          unit={`${l.critical} critical · last ${l.lines_scanned} lines`}
          tone={l.critical > 0 ? 'bad' : l.errors > 0 ? 'warn' : 'ok'}
          wide
        >
          {l.by_component.length > 0 && (
            <div className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
              {l.by_component.map((c) => `${c.component} ×${c.count}`).join(' · ')}
            </div>
          )}
          {l.recent.slice(0, 3).map((x, k) => (
            <div
              key={k}
              className="text-[10px] hud-mono truncate"
              style={{ color: 'var(--color-text-tertiary)' }}
              title={`${x.ts} ${x.message}`}
            >
              {x.ts.slice(11)} {x.message}
            </div>
          ))}
        </Card>
      )}
      {r && (
        <Card
          icon={DatabaseBackup}
          label="Backup issues"
          value={r.backup_issues}
          sub={r.backup_issues ? 'a backup target is failing' : 'no backup problems'}
          tone={r.backup_issues > 0 ? 'bad' : 'ok'}
        />
      )}
      {r && (
        <Card
          icon={CalendarClock}
          label="Breaking soon"
          value={r.upcoming_breaking.length}
          sub={
            r.upcoming_breaking.length
              ? [...new Set(r.upcoming_breaking.map((u) => `${u.domain} → ${u.breaks_in}`))].slice(0, 3).join(', ')
              : 'no deprecations due'
          }
          tone={r.upcoming_breaking.length ? 'warn' : 'ok'}
        />
      )}
    </div>
  );
}

interface HaPanelProps {
  /** Hand a prompt to the chat (opens as a new message). */
  onAsk?: (prompt: string) => void;
}

export function HaPanel({ onAsk }: HaPanelProps) {
  const { data, loading, error, refresh } = useHaDashboard();
  const ago = data?.collected_at ? Math.max(0, Math.round(Date.now() / 1000 - data.collected_at)) : null;

  return (
    <aside
      className="flex flex-col h-full overflow-y-auto shrink-0"
      style={{ width: 360, minWidth: 360, background: 'var(--color-bg)', borderLeft: '1px solid var(--color-border)' }}
    >
      <div
        className="flex items-center justify-between px-4 py-3 shrink-0"
        style={{ borderBottom: '1px solid var(--color-border)' }}
      >
        <div className="min-w-0">
          <div className="text-xs font-semibold tracking-wide uppercase" style={{ color: 'var(--color-text-secondary)' }}>
            Home Assistant
          </div>
          <div className="text-[11px] truncate" style={{ color: 'var(--color-text-tertiary)' }}>
            {data?.system?.name ?? '—'} · {data?.system?.version ?? '—'} · {data?.system?.state ?? 'no data'}
            {ago !== null ? ` · ${ago}s ago` : ''}
            {data?.stale ? ' · stale' : ''}
          </div>
        </div>
        <button
          onClick={() => void refresh()}
          className="p-1.5 rounded-md cursor-pointer transition-colors"
          style={{ color: 'var(--color-text-secondary)' }}
          title="Refresh now"
          aria-label="Refresh Home Assistant data"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="p-3 flex flex-col gap-3">
        {error && (
          <div
            className="text-xs p-2 rounded-md"
            style={{
              background: 'color-mix(in srgb, var(--color-error) 10%, transparent)',
              color: 'var(--color-text)',
            }}
          >
            Could not read Home Assistant: {error}
          </div>
        )}
        {!data && loading && (
          <div className="text-xs hud-mono" style={{ color: 'var(--color-text-tertiary)' }}>
            reading Home Assistant…
          </div>
        )}
        {data?.ok && <Cards d={data} />}

        {onAsk && data?.ok && (
          <div className="flex flex-col gap-1.5">
            <button
              onClick={() =>
                onAsk(
                  'Run a Home Assistant health check. Read only, change nothing. Summarise the top 5 problems with a proposed fix for each.',
                )
              }
              className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs cursor-pointer text-left transition-colors"
              style={{
                background: 'var(--color-accent-subtle)',
                border: '1px solid var(--color-accent)',
                color: 'var(--color-text)',
              }}
            >
              <MessageSquareText size={14} style={{ color: 'var(--color-accent)' }} />
              Ask Jarvis for a health check
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
