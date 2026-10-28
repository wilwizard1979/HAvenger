import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import {
  askArchitect,
  fetchAutomations,
  fetchOverview,
  startRefresh,
  type ArchitectOverview,
  type AskResult,
  type AutomationRow,
} from '../lib/architect';

const muted = { color: 'var(--color-text-secondary)' } as const;
const faint = { color: 'var(--color-text-tertiary)' } as const;
const MODES = ['Ask', 'Change', 'Create'] as const;
const FILTERS = [
  { id: '', label: 'All' },
  { id: 'broken', label: 'Broken references' },
  { id: 'stale', label: 'Not run > 1 year' },
  { id: 'disabled', label: 'Disabled' },
];

function Kpi({ label, value, color }: { label: string; value: number | string; color?: string }) {
  return (
    <div className="hud-panel px-3 py-2">
      <div className="text-[0.6875rem]" style={muted}>
        {label}
      </div>
      <div className="text-xl font-semibold hud-mono" style={{ color: color ?? 'var(--color-text)' }}>
        {value}
      </div>
    </div>
  );
}

function Row({ r }: { r: AutomationRow }) {
  return (
    <div className="flex items-start gap-2 py-1.5" style={{ borderTop: '1px solid var(--color-border)', opacity: r.disabled ? 0.55 : 1 }}>
      <div className="min-w-0 flex-1">
        <div className="text-xs truncate" style={{ color: 'var(--color-text)' }}>
          {r.alias}
        </div>
        <div className="text-[0.6875rem] truncate" style={faint}>
          {r.entity_id}
          {r.via && r.via.length > 0 ? ` · uses ${r.via.join(', ')}` : ''}
        </div>
        {r.broken && (
          <div className="text-[0.6875rem]" style={{ color: 'var(--color-error)' }}>
            {[...r.missing.map((e) => `missing ${e}`), ...r.offline.map((e) => `offline ${e}`)].slice(0, 3).join(' · ')}
          </div>
        )}
      </div>
      <div className="text-[0.6875rem] text-right shrink-0" style={muted}>
        {r.disabled ? 'off' : r.state}
        <div style={faint}>{r.age_days == null ? 'never run' : `${Math.round(r.age_days)} d ago`}</div>
      </div>
    </div>
  );
}

export function AutomationsPage() {
  const [ov, setOv] = useState<ArchitectOverview | null>(null);
  const [group, setGroup] = useState('');
  const [filter, setFilter] = useState('');
  const [rows, setRows] = useState<AutomationRow[]>([]);
  const [mode, setMode] = useState<(typeof MODES)[number]>('Ask');
  const [text, setText] = useState('');
  const [answer, setAnswer] = useState<AskResult | null>(null);
  const [err, setErr] = useState('');

  const load = useCallback(() => {
    fetchOverview().then(setOv).catch((e) => setErr(String(e)));
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  // poll while a read is running
  useEffect(() => {
    if (!ov?.refreshing) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [ov?.refreshing, load]);
  useEffect(() => {
    if (!ov?.ready) return;
    fetchAutomations(group, filter, '').then((d) => setRows(d.rows)).catch(() => {});
  }, [ov?.ready, ov?.fetched_at, group, filter]);

  const refresh = async () => {
    await startRefresh();
    load();
  };
  const ask = () => {
    if (!text.trim()) return;
    askArchitect(text).then(setAnswer).catch((e) => setErr(String(e)));
  };

  const k = ov?.kpis;
  const read = ov?.fetched_at ? new Date(ov.fetched_at * 1000).toLocaleString() : 'never';
  return (
    <div className="flex-1 overflow-y-auto p-6 flex flex-col gap-4 max-w-6xl">
      <div className="flex items-start gap-3">
        <div>
          <h1 className="text-lg font-semibold" style={{ color: 'var(--color-text)' }}>
            Automations
          </h1>
          <div className="text-xs" style={muted}>
            Jarvis the Architect: understand, fix, create. Read-only for now · last read {read}
          </div>
        </div>
        <button onClick={refresh} disabled={ov?.refreshing} className="ml-auto flex items-center gap-1.5 text-xs px-3 py-1.5 rounded cursor-pointer" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)', background: 'var(--color-bg-secondary)' }}>
          <RefreshCw size={13} className={ov?.refreshing ? 'animate-spin' : ''} />
          {ov?.refreshing ? 'Reading Home Assistant…' : 'Refresh'}
        </button>
      </div>
      {(err || ov?.error) && (
        <div className="text-xs" style={{ color: 'var(--color-error)' }}>
          {err || ov?.error}
        </div>
      )}
      {ov && !ov.ready && (
        <div className="hud-panel p-4 text-xs" style={muted}>
          No inventory yet. Press Refresh to read all automations (read-only, takes a few minutes the first time).
        </div>
      )}
      {k && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2">
          <Kpi label="Automations" value={k.total} />
          <Kpi label="On / off" value={`${k.on} / ${k.off}`} />
          <Kpi label="Unavailable" value={k.unavailable} color={k.unavailable ? 'var(--color-warning)' : undefined} />
          <Kpi label="Broken references" value={k.broken} color={k.broken ? 'var(--color-error)' : undefined} />
          <Kpi label="Not run > 1 year" value={k.stale} color={k.stale ? 'var(--color-warning)' : undefined} />
          <Kpi label="Ran last 7 days" value={k.recent} color="var(--color-success)" />
          <Kpi label="Architect spend" value="$0.00" />
        </div>
      )}

      <section className="hud-panel p-3 flex flex-col gap-2">
        <div className="flex gap-1.5">
          {MODES.map((m) => (
            <button key={m} onClick={() => setMode(m)} className="text-xs px-2.5 py-0.5 rounded-full cursor-pointer" style={{ border: '1px solid var(--color-border)', background: mode === m ? 'var(--color-accent)' : 'transparent', color: mode === m ? 'var(--color-bg)' : 'var(--color-text-secondary)' }}>
              {m}
            </button>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && mode === 'Ask' && ask()}
            placeholder={mode === 'Ask' ? 'e.g. Which automations use light.lamp_entrada?' : 'Coming next: Sonnet / Opus recommendation'}
            disabled={mode !== 'Ask'}
            className="flex-1 text-sm px-3 py-2 rounded"
            style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
          />
          <button onClick={ask} disabled={mode !== 'Ask' || !ov?.ready} className="text-xs px-3 rounded cursor-pointer" style={{ background: 'var(--color-accent)', color: 'var(--color-bg)' }}>
            Ask
          </button>
          <button disabled title="Next step" className="text-xs px-3 rounded" style={{ border: '1px solid var(--color-border)', ...faint }}>
            Recommend (Sonnet)
          </button>
          <button disabled title="Next step" className="text-xs px-3 rounded" style={{ border: '1px solid var(--color-border)', ...faint }}>
            Recommend (Opus)
          </button>
        </div>
        <div className="text-[0.6875rem]" style={faint}>
          Ask is answered by Python from the local map: free and instant. Recommend (Sonnet/Opus) comes in the next step, with the cost shown before the click.
        </div>
        {answer?.error && <div className="text-xs" style={{ color: 'var(--color-error)' }}>{answer.error}</div>}
        {answer && !answer.error && (
          <div className="flex flex-col">
            <div className="text-xs mb-1" style={muted}>
              {answer.count} found ({answer.active} active, {answer.disabled} disabled, shown last) · matched on {answer.matched_on?.join(', ') || 'nothing'}
            </div>
            {answer.rows?.map((r) => <Row key={r.entity_id} r={r} />)}
          </div>
        )}
      </section>

      {ov?.groups && (
        <section className="grid gap-4 lg:grid-cols-[1fr_2fr]">
          <div className="hud-panel p-3 flex flex-col">
            <div className="hud-label mb-1">Groups (by name prefix)</div>
            <button onClick={() => setGroup('')} className="text-left text-xs py-1.5 cursor-pointer" style={{ background: 'none', border: 'none', color: group === '' ? 'var(--color-accent)' : 'var(--color-text)' }}>
              All · {k?.total}
            </button>
            {ov.groups.map((g) => (
              <button key={g.name} onClick={() => setGroup(g.name)} className="text-left text-xs py-1.5 cursor-pointer flex gap-2" style={{ background: 'none', border: 'none', borderTop: '1px solid var(--color-border)', color: group === g.name ? 'var(--color-accent)' : 'var(--color-text)' }}>
                <span>
                  {g.name} · {g.count}
                </span>
                <span className="ml-auto text-[0.6875rem]" style={muted}>
                  {g.broken ? `${g.broken} broken · ` : ''}
                  {g.disabled} off
                </span>
              </button>
            ))}
          </div>
          <div className="hud-panel p-3 flex flex-col">
            <div className="flex gap-1.5 mb-1 flex-wrap">
              {FILTERS.map((f) => (
                <button key={f.id} onClick={() => setFilter(f.id)} className="text-[0.6875rem] px-2 py-0.5 rounded-full cursor-pointer" style={{ border: '1px solid var(--color-border)', background: filter === f.id ? 'var(--color-bg-tertiary)' : 'transparent', color: 'var(--color-text-secondary)' }}>
                  {f.label}
                </button>
              ))}
              <span className="ml-auto text-[0.6875rem]" style={faint}>
                {rows.length} shown, active first
              </span>
            </div>
            <div className="max-h-[28rem] overflow-y-auto">
              {rows.map((r) => <Row key={r.entity_id} r={r} />)}
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
