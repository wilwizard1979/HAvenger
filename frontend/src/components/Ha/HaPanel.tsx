import { useNavigate } from 'react-router';
import {
  RefreshCw,
  Plug,
  Workflow,
  DatabaseBackup,
  History,
  Download,
  CalendarClock,
  Terminal,
  Hourglass,
  Power,
  MessageSquareText,
  type LucideIcon,
} from 'lucide-react';
import { api, usePolled, TONE_COLOR, type Tone } from '../../lib/maintenance';

const POLL_MS = 60_000;

function Card({
  icon: Icon,
  label,
  value,
  unit,
  tone,
  section,
}: {
  icon: LucideIcon;
  label: string;
  value: string | number | null;
  unit?: string;
  tone: Tone;
  section: string;
}) {
  const navigate = useNavigate();
  return (
    <button
      onClick={() => navigate(`/maintenance?section=${section}`)}
      className="hud-panel p-3 flex flex-col gap-1 min-w-0 text-left cursor-pointer"
      style={{ borderLeft: `2px solid ${TONE_COLOR[tone]}` }}
      title={`Open ${label} in the Maintenance dashboard`}
    >
      <div className="hud-label flex items-center gap-1.5">
        <Icon size={12} style={{ color: TONE_COLOR[tone] }} />
        <span className="truncate">{label}</span>
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-semibold hud-mono" style={{ color: TONE_COLOR[tone] }}>
          {value ?? '—'}
        </span>
        {unit && (
          <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {unit}
          </span>
        )}
      </div>
    </button>
  );
}

interface HaPanelProps {
  /** Hand a prompt to the chat (opens as a new message). */
  onAsk?: (prompt: string) => void;
}

/** Main-page summary: totals only. Every card opens the Maintenance dashboard. */
export function HaPanel({ onAsk }: HaPanelProps) {
  const navigate = useNavigate();
  const { data, loading, error, reload } = usePolled(api.overview, POLL_MS);
  const c = data?.cards;
  const ago = data?.checked_at ? Math.max(0, Math.round(Date.now() / 1000 - data.checked_at)) : null;

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
            {data?.ha_version ?? '—'}
            {ago !== null ? ` · checked ${ago < 90 ? `${ago}s` : `${Math.round(ago / 60)} min`} ago` : ' · waiting for first check'}
          </div>
        </div>
        <button
          onClick={() => void reload()}
          className="p-1.5 rounded-md cursor-pointer transition-colors"
          style={{ color: 'var(--color-text-secondary)' }}
          title="Refresh"
          aria-label="Refresh maintenance totals"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="p-3 flex flex-col gap-3">
        {error && (
          <div
            className="text-xs p-2 rounded-md"
            style={{ background: 'color-mix(in srgb, var(--color-error) 10%, transparent)', color: 'var(--color-text)' }}
          >
            Janitor not reachable: {error}
          </div>
        )}
        {c && (
          <div className="grid grid-cols-2 gap-2">
            <Card icon={DatabaseBackup} label="Backup failed" value={c.backup_failed.value} tone={c.backup_failed.tone} section="backup_failed" />
            <Card
              icon={History}
              label="Last backup"
              value={c.last_backup.value === null ? null : Math.round(c.last_backup.value)}
              unit={c.last_backup.value === null ? undefined : 'h ago'}
              tone={c.last_backup.tone}
              section="last_backup"
            />
            <Card icon={Plug} label="Integrations failed" value={c.integrations_failed.value} tone={c.integrations_failed.tone} section="integrations_failed" />
            <Card icon={Workflow} label="Automation errors" value={c.automation_errors.value} tone={c.automation_errors.tone} section="automation_errors" />
            <Card icon={Download} label="Updates pending" value={c.updates_pending.value} tone={c.updates_pending.tone} section="updates_pending" />
            <Card
              icon={CalendarClock}
              label="Breaking changes"
              value={c.deprecations.value}
              unit={c.deprecations.next_version ? `from ${c.deprecations.next_version}` : undefined}
              tone={c.deprecations.tone}
              section="deprecations"
            />
            <Card icon={Terminal} label="Critical log errors" value={c.log_critical.value} tone={c.log_critical.tone} section="log_critical" />
            <Card
              icon={Hourglass}
              label={`Unavailable > ${c.stale_unavailable.days} d`}
              value={c.stale_unavailable.value}
              unit="entities"
              tone={c.stale_unavailable.tone}
              section="stale_unavailable"
            />
            <Card
              icon={Power}
              label="Last HA restart"
              value={c.last_restart.value === null ? null : Math.round(c.last_restart.value)}
              unit={c.last_restart.value === null ? undefined : 'h ago'}
              tone={c.last_restart.tone}
              section="last_restart"
            />
          </div>
        )}
        {!data && loading && (
          <div className="text-xs hud-mono" style={{ color: 'var(--color-text-tertiary)' }}>
            reading maintenance data…
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <button
            onClick={() => navigate('/maintenance?tab=plan')}
            className="px-3 py-2 rounded-lg text-xs cursor-pointer text-left transition-colors"
            style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
          >
            Open the maintenance plan
          </button>
          {onAsk && (
            <button
              onClick={() =>
                onAsk(
                  'Run a Home Assistant health check. Read only, change nothing. Summarise the top 5 problems with a proposed fix for each.',
                )
              }
              className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs cursor-pointer text-left transition-colors"
              style={{ background: 'var(--color-accent-subtle)', border: '1px solid var(--color-accent)', color: 'var(--color-text)' }}
            >
              <MessageSquareText size={14} style={{ color: 'var(--color-accent)' }} />
              Ask Jarvis for a health check
            </button>
          )}
        </div>
      </div>
    </aside>
  );
}
