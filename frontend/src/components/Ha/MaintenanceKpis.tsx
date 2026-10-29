import { useNavigate } from 'react-router';
import {
  Plug,
  Workflow,
  DatabaseBackup,
  History,
  Download,
  CalendarClock,
  Terminal,
  Hourglass,
  BatteryLow,
  Power,
  type LucideIcon,
} from 'lucide-react';
import { TONE_COLOR, type Overview, type Tone } from '../../lib/maintenance';

export function Card({
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

/** The ten Maintenance cards. Every card opens its section on the Maintenance page. */
export function MaintenanceKpis({ c }: { c: Overview['cards'] }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
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
              icon={BatteryLow}
              label="Motion batteries"
              value={c.motion_batteries.value}
              unit={
                c.motion_batteries.kind === 'red'
                  ? 'unknown or offline'
                  : c.motion_batteries.kind === 'orange'
                    ? `below ${c.motion_batteries.warn_pct}%`
                    : c.motion_batteries.kind === 'ok'
                      ? 'all fine'
                      : undefined
              }
              tone={c.motion_batteries.tone}
              section="motion_batteries"
            />
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
  );
}
