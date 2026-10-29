import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import {
  RefreshCw,
  History,
  Wrench,
  Workflow,
  Coins,
  Activity,
  MessageSquareText,
  Volume2,
  Square,
  ChevronRight,
  type LucideIcon,
} from 'lucide-react';
import { api, usePolled, TONE_COLOR, type Tone } from '../../lib/maintenance';
import { fetchOverview, type ArchitectOverview } from '../../lib/architect';
import { useReadAloud } from '../../lib/janitorSpeech';

const POLL_MS = 60_000;
const muted = { color: 'var(--color-text-secondary)' };
const faint = { color: 'var(--color-text-tertiary)' };

function Stat({
  icon: Icon,
  label,
  value,
  unit,
  tone = 'ok',
  to,
  title,
}: {
  icon: LucideIcon;
  label: string;
  value: string | number | null;
  unit?: string;
  tone?: Tone;
  to?: string;
  title?: string;
}) {
  const navigate = useNavigate();
  return (
    <button
      onClick={() => to && navigate(to)}
      className="hud-panel p-3 flex flex-col gap-1 min-w-0 text-left"
      style={{ borderLeft: `2px solid ${TONE_COLOR[tone]}`, cursor: to ? 'pointer' : 'default' }}
      title={title}
    >
      <div className="hud-label flex items-center gap-1.5">
        <Icon size={12} style={{ color: TONE_COLOR[tone] }} />
        <span className="truncate" title={label}>{label}</span>
      </div>
      <div className="flex items-baseline gap-1 flex-wrap">
        <span className="text-2xl font-semibold hud-mono" style={{ color: TONE_COLOR[tone] }}>
          {value ?? '—'}
        </span>
        {unit && (
          <span className="text-xs" style={faint}>
            {unit}
          </span>
        )}
      </div>
    </button>
  );
}

function Specialist({ icon: Icon, name, line, to, tone }: { icon: LucideIcon; name: string; line: string; to: string; tone: Tone }) {
  const navigate = useNavigate();
  return (
    <button
      onClick={() => navigate(to)}
      className="hud-panel px-3 py-2.5 flex items-center gap-2.5 text-left cursor-pointer min-w-0"
      style={{ borderLeft: `2px solid ${TONE_COLOR[tone]}` }}
    >
      <Icon size={16} style={{ color: TONE_COLOR[tone] }} />
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium" style={{ color: 'var(--color-text)' }}>
          {name}
        </div>
        <div className="text-[0.6875rem] truncate" style={muted}>
          {line}
        </div>
      </div>
      <ChevronRight size={14} style={faint} />
    </button>
  );
}

const usd = (n: number) => `$${n.toFixed(n >= 10 ? 0 : 2)}`;

interface JarvisPanelProps {
  /** Hand a prompt to the chat (opens as a new message). */
  onAsk?: (prompt: string) => void;
}

/** Main-page summary: general facts and one row per specialist. Details live on their pages. */
export function JarvisPanel({ onAsk }: JarvisPanelProps) {
  const speech = useReadAloud();
  const overview = usePolled(api.overview, POLL_MS);
  const plan = usePolled(api.plan, POLL_MS);
  const costs = usePolled(api.costs, POLL_MS);
  const [arch, setArch] = useState<ArchitectOverview | null>(null);
  useEffect(() => {
    const load = () => fetchOverview().then(setArch).catch(() => {});
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, []);

  const data = overview.data;
  const ago = data?.checked_at ? Math.max(0, Math.round(Date.now() / 1000 - data.checked_at)) : null;
  const tasks = plan.data?.tasks ?? [];
  const serious = tasks.filter((t) => t.severity === 'critical').length;
  const lb = data?.cards.last_backup;
  const k = arch?.kpis;
  const c = costs.data;
  const since = c?.counted_from ? new Date(c.counted_from * 1000).toLocaleDateString() : null;

  return (
    <aside
      className="flex flex-col h-full overflow-y-auto shrink-0"
      style={{ width: 360, minWidth: 360, background: 'var(--color-bg)', borderLeft: '1px solid var(--color-border)' }}
    >
      <div className="flex items-center justify-between px-4 py-3 shrink-0" style={{ borderBottom: '1px solid var(--color-border)' }}>
        <div className="min-w-0">
          <div className="text-xs font-semibold tracking-wide uppercase" style={muted}>
            Jarvis
          </div>
          <div className="text-[0.6875rem] truncate" style={faint}>
            Home Assistant {data?.ha_version ?? '—'}
            {ago !== null ? ` · checked ${ago < 90 ? `${ago}s` : `${Math.round(ago / 60)} min`} ago` : ''}
          </div>
        </div>
        <button
          onClick={() => {
            void overview.reload();
            void plan.reload();
            void costs.reload();
          }}
          className="p-1.5 rounded-md cursor-pointer"
          style={muted}
          title="Refresh"
          aria-label="Refresh"
        >
          <RefreshCw size={14} className={overview.loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="p-3 flex flex-col gap-3">
        {overview.error && (
          <div className="text-xs p-2 rounded-md" style={{ background: 'color-mix(in srgb, var(--color-error) 10%, transparent)', color: 'var(--color-text)' }}>
            Janitor not reachable: {overview.error}
          </div>
        )}

        <div className="flex flex-col gap-2">
          <div className="hud-label">Specialists</div>
          <Specialist
            icon={Wrench}
            name="Janitor · maintenance"
            line={plan.data ? `${tasks.length} open tasks · ${serious} serious` : 'reading…'}
            to="/maintenance"
            tone={serious ? 'bad' : tasks.length ? 'warn' : 'ok'}
          />
          <Specialist
            icon={Workflow}
            name="Architect · automations"
            line={k ? `${k.total} automations · ${k.broken} with broken references` : arch?.ready === false ? 'no map yet, press Refresh' : 'reading…'}
            to="/automations"
            tone={k && k.broken ? 'warn' : 'ok'}
          />
        </div>

        <div className="grid grid-cols-2 gap-2">
          <Stat
            icon={History}
            label="Last backup"
            value={lb && lb.value !== null ? Math.round(lb.value) : null}
            unit={lb && lb.value !== null ? 'h ago' : undefined}
            tone={lb?.tone ?? 'ok'}
            to="/maintenance?section=last_backup"
          />
          <Stat
            icon={Activity}
            label="Ran, 7 days"
            value={k ? k.recent : null}
            unit={k ? `of ${k.total}` : undefined}
            to="/automations"
            title="Automations with a run in the last 7 days (Home Assistant keeps only the last run time)"
          />
          <Stat
            icon={Coins}
            label="Claude cost"
            value={c ? usd(c.total_usd) : null}
            unit={since ? `from ${since}` : undefined}
            title="Real Claude cost from the ledger (dry runs excluded). Reset in Settings."
          />
          <Stat icon={Coins} label="This month" value={c ? usd(c.month_usd) : null} />
        </div>
        {c && (
          <div className="text-[0.6875rem] -mt-1" style={faint}>
            Janitor {usd(c.janitor_usd)} · Architect {usd(c.architect_usd)}
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <button
            onClick={() => (speech.speaking ? speech.stop() : void speech.run())}
            disabled={speech.busy}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs cursor-pointer text-left transition-colors"
            style={{ background: 'var(--color-accent-subtle)', border: '1px solid var(--color-accent)', color: 'var(--color-text)' }}
            title="Reads the plain-English house status aloud and shows it in the chat"
          >
            {speech.speaking ? <Square size={14} style={{ color: 'var(--color-accent)' }} /> : <Volume2 size={14} style={{ color: 'var(--color-accent)' }} />}
            {speech.speaking ? 'Stop reading' : speech.busy ? 'Preparing…' : 'Read the house status aloud'}
          </button>
          {speech.error && (
            <div className="text-[0.6875rem]" style={{ color: 'var(--color-warning)' }}>
              {speech.error}
            </div>
          )}
          {onAsk && (
            <button
              onClick={() => onAsk('Run a Home Assistant health check. Read only, change nothing. Summarise the top 5 problems with a proposed fix for each.')}
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
