import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import {
  fetchAutomations,
  fetchCases,
  fetchOverview,
  queryArchitect,
  recommend,
  startRefresh,
  type ArchitectOverview,
  type AutomationRow,
  type Case,
  type QueryResult,
} from '../lib/architect';

const muted = { color: 'var(--color-text-secondary)' } as const;
const faint = { color: 'var(--color-text-tertiary)' } as const;
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
          {r.area ? `${r.area} · ` : ''}{r.entity_id}
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
  const [area, setArea] = useState('');
  const [rows, setRows] = useState<AutomationRow[]>([]);
  const [text, setText] = useState('');
  const [pick, setPick] = useState('');
  const [all, setAll] = useState<AutomationRow[]>([]);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [asked, setAsked] = useState('');
  const [rec, setRec] = useState<Case | null>(null);
  const [recErr, setRecErr] = useState('');
  const [cases, setCases] = useState<Case[]>([]);
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
    fetchAutomations(group, filter, '', area).then((d) => setRows(d.rows)).catch(() => {});
  }, [ov?.ready, ov?.fetched_at, group, filter, area]);

  const refresh = async () => {
    await startRefresh();
    load();
  };
  useEffect(() => {
    if (!ov?.ready) return;
    fetchAutomations('', '', '').then((d) => setAll(d.rows.filter((r) => !r.disabled))).catch(() => {});
    fetchCases().then((d) => setCases(d.cases)).catch(() => {});
  }, [ov?.ready, ov?.fetched_at]);

  const picked = all.find((r) => r.alias === pick)?.entity_id;
  const ask = (force = '') => {
    if (!text.trim()) return;
    setRec(null);
    setRecErr('');
    setResult(null);
    setAsked(text);
    queryArchitect(text, picked ? [picked] : [], force).then(setResult).catch((e) => setErr(String(e)));
  };
  const send = async (model: 'sonnet' | 'opus') => {
    const cost = result?.preview?.estimate_usd[model] ?? 0;
    if (!window.confirm(`Send to ${model === 'sonnet' ? 'Sonnet' : 'Opus'}? Worst case $${cost.toFixed(3)}.`)) return;
    setBusy(true);
    setRecErr('');
    const r = await recommend(asked, model, picked ? [picked] : []);
    setBusy(false);
    if (r.error) setRecErr(r.error);
    else if (r.case) {
      setRec(r.case);
      fetchCases().then((d) => setCases(d.cases)).catch(() => {});
      load();
    }
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
          <Kpi label="Architect spend" value={`$${(ov?.spent_usd ?? 0).toFixed(2)}`} />
        </div>
      )}

      <section className="hud-panel p-3 flex flex-col gap-2">
        <div className="flex gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && ask()}
            placeholder='e.g. "Which automations use light.lamp_entrada?" or "Extend the front light time to 15 min"'
            className="flex-1 text-sm px-3 py-2 rounded"
            style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
          />
          <button onClick={() => ask()} disabled={!ov?.ready} className="text-xs px-4 rounded cursor-pointer" style={{ background: 'var(--color-accent)', color: 'var(--color-bg)' }}>
            Ask
          </button>
        </div>
        <div className="flex items-center gap-2 text-[0.6875rem]" style={faint}>
          <span>Optional: automation</span>
          <input
            list="arch-automations"
            value={pick}
            onChange={(e) => setPick(e.target.value)}
            placeholder="type to search"
            className="text-xs px-2 py-1 rounded w-72"
            style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
          />
          <datalist id="arch-automations">
            {all.map((r) => (
              <option key={r.entity_id} value={r.alias} />
            ))}
          </datalist>
          <span className="ml-auto">
            Architect spend ${(ov?.spent_usd ?? 0).toFixed(2)} of ${(ov?.limit_usd ?? 5).toFixed(2)} this month
          </span>
        </div>
        <div className="text-[0.6875rem]" style={faint}>
          Lookups are answered by Python from the local map, free. A change or create request shows what would be sent and the cost first; nothing goes to Sonnet or Opus until you press its button.
        </div>
        {result?.error && <div className="text-xs" style={{ color: 'var(--color-error)' }}>{result.error}</div>}
        {result?.lookup && (
          <div className="flex flex-col">
            <div className="text-xs mb-1" style={muted}>
              {result.lookup.count} found ({result.lookup.active} active, {result.lookup.disabled} disabled, shown last) · matched on {result.lookup.matched_on?.join(', ') || 'nothing'}
              <button onClick={() => ask('change')} className="ml-3 px-2 py-0.5 rounded cursor-pointer" style={{ border: '1px solid var(--color-accent)', color: 'var(--color-accent)', background: 'none' }}>
                Ask Sonnet / Opus about this
              </button>
            </div>
            {result.lookup.rows?.map((r) => <Row key={r.entity_id} r={r} />)}
          </div>
        )}
        {result?.preview && (
          <div className="flex flex-col gap-1.5 text-xs" style={{ color: 'var(--color-text)' }}>
            <div style={muted}>
              Understood as a <b>{result.preview.intent}</b> request
              <button onClick={() => ask('lookup')} className="ml-3 underline cursor-pointer" style={{ background: 'none', border: 'none', color: 'var(--color-accent)' }}>
                treat as a question
              </button>
            </div>
            <div>
              Target: {result.preview.targets.length ? result.preview.targets.map((t) => t.alias).join(', ') : 'none found (pick one above)'}
            </div>
            <div style={muted}>
              Related enabled automations sent: {result.preview.related.length}
              {result.preview.related_hidden ? ` (+${result.preview.related_hidden} not sent)` : ''} · disabled left out: {result.preview.disabled_left_out}
            </div>
            {result.preview.picked_disabled.length > 0 && (
              <div style={{ color: 'var(--color-warning)' }}>The picked automation is disabled, so it is not sent.</div>
            )}
            <div style={result.preview.over_cap ? { color: 'var(--color-error)' } : muted}>
              Size about {result.preview.tokens.toLocaleString()} tokens (cap {result.preview.token_cap.toLocaleString()}).
              {result.preview.over_cap ? ' Too big: ask a narrower question or pick the automation.' : ''}
            </div>
            <div className="flex gap-2 items-center">
              <button onClick={() => send('sonnet')} disabled={!result.preview.can_send || busy} className="px-3 py-1.5 rounded cursor-pointer" style={{ background: 'var(--color-accent)', color: 'var(--color-bg)', opacity: result.preview.can_send ? 1 : 0.4 }}>
                Recommend (Sonnet) · up to ${result.preview.estimate_usd.sonnet.toFixed(3)}
              </button>
              <button onClick={() => send('opus')} disabled={!result.preview.can_send || busy} className="px-3 py-1.5 rounded cursor-pointer" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)', opacity: result.preview.can_send ? 1 : 0.4 }}>
                Recommend (Opus) · up to ${result.preview.estimate_usd.opus.toFixed(3)}
              </button>
              {result.preview.dry_run && <span style={faint}>Dry run is on: nothing will actually be sent.</span>}
              {busy && <span style={faint}>Waiting for the model…</span>}
            </div>
            <details style={faint}>
              <summary className="cursor-pointer">Show exactly what would be sent</summary>
              <pre className="text-[0.6875rem] whitespace-pre-wrap mt-1 max-h-64 overflow-y-auto">{result.preview.text}</pre>
            </details>
          </div>
        )}
        {recErr && <div className="text-xs" style={{ color: 'var(--color-error)' }}>{recErr}</div>}
        {rec && (
          <div className="hud-panel p-3 text-xs flex flex-col gap-1.5" style={{ color: 'var(--color-text)' }}>
            <div style={muted}>
              Recommendation {rec.id} · {rec.model} · {rec.tokens.toLocaleString()} tokens sent · ${rec.cost_usd.toFixed(4)}
            </div>
            {rec.dry_run ? (
              <div style={faint}>Dry run: nothing was sent to the model. Turn dry run off in Maintenance settings to get a real answer.</div>
            ) : (
              <pre className="whitespace-pre-wrap">{rec.answer}</pre>
            )}
            {rec.unknown_entities.length > 0 && (
              <div style={{ color: 'var(--color-error)' }}>Not in Home Assistant: {rec.unknown_entities.join(', ')}. Do not apply.</div>
            )}
            <div style={faint}>Approval on Telegram comes in the next step; nothing is applied from here.</div>
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
              {ov.areas && ov.areas.length > 0 && (
                <select
                  value={area}
                  onChange={(e) => setArea(e.target.value)}
                  className="text-[0.6875rem] px-1.5 py-0.5 rounded"
                  style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)', color: 'var(--color-text-secondary)' }}
                >
                  <option value="">All areas</option>
                  {ov.areas.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} · {a.count}
                    </option>
                  ))}
                </select>
              )}
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
      {cases.length > 0 && (
        <section className="hud-panel p-3 flex flex-col">
          <div className="hud-label mb-1">History</div>
          {cases.slice(0, 8).map((c) => (
            <div key={c.id} className="flex gap-2 py-1 text-xs" style={{ borderTop: '1px solid var(--color-border)' }}>
              <span className="hud-mono" style={muted}>{c.id}</span>
              <span className="truncate" style={{ color: 'var(--color-text)' }}>{c.question}</span>
              <span className="ml-auto shrink-0" style={faint}>{c.model} · ${c.cost_usd.toFixed(3)}</span>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
