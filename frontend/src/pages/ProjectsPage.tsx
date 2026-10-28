import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { fetchProjects, type Project, type ProjectStatus, type ProjectsOverview } from '../lib/projects';

const muted = { color: 'var(--color-text-secondary)' } as const;
const faint = { color: 'var(--color-text-tertiary)' } as const;

const COLUMNS: { id: string; title: string; statuses: ProjectStatus[]; color: string }[] = [
  { id: 'idea', title: 'Ideas', statuses: ['idea'], color: 'var(--color-text-tertiary)' },
  { id: 'planned', title: 'Planned', statuses: ['planned'], color: 'var(--color-accent-purple)' },
  { id: 'in_progress', title: 'In progress', statuses: ['in_progress'], color: 'var(--color-accent)' },
  { id: 'hold', title: 'On hold / blocked', statuses: ['on_hold', 'blocked'], color: 'var(--color-warning)' },
  { id: 'done', title: 'Done', statuses: ['done'], color: 'var(--color-success)' },
];

function Card({ p }: { p: Project }) {
  const [open, setOpen] = useState(false);
  const detail = p.plan.length > 0 || p.next || p.notes || p.user_tasks.length > 0;
  return (
    <div className="hud-panel p-3 flex flex-col gap-1.5">
      <div className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>
        {p.title}
      </div>
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-[0.6875rem] px-1.5 py-0.5 rounded" style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-secondary)' }}>
          {p.area}
        </span>
        {p.status === 'blocked' && (
          <span className="text-[0.6875rem] px-1.5 py-0.5 rounded" style={{ border: '1px solid var(--color-error)', color: 'var(--color-error)' }}>
            blocked
          </span>
        )}
        {p.added && (
          <span className="text-[0.6875rem]" style={faint}>
            since {p.added}
          </span>
        )}
      </div>
      <div className="text-[0.6875rem]" style={muted}>
        {p.status_text}
      </div>
      {p.summary && (
        <div className="text-xs" style={{ color: 'var(--color-text)' }}>
          {p.summary}
        </div>
      )}
      {detail && (
        <button
          onClick={() => setOpen(!open)}
          className="text-[0.6875rem] self-start cursor-pointer"
          style={{ ...muted, background: 'none', border: 'none' }}
        >
          {open ? '▾ Hide details' : '▸ Details'}
        </button>
      )}
      {open && (
        <div className="text-[0.6875rem] flex flex-col gap-1" style={muted}>
          {p.next && <div>Next: {p.next}</div>}
          {p.plan.length > 0 && (
            <ol className="list-decimal pl-4">
              {p.plan.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          )}
          {p.user_tasks.length > 0 && <div>For you: {p.user_tasks.join(' · ')}</div>}
          {p.notes && <div>Notes: {p.notes}</div>}
          <div style={faint}>from {p.source}</div>
        </div>
      )}
    </div>
  );
}

export function ProjectsPage() {
  const [data, setData] = useState<ProjectsOverview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [area, setArea] = useState<string>('all');
  const [q, setQ] = useState('');
  const [backlogOpen, setBacklogOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);

  const load = useCallback(() => {
    fetchProjects()
      .then((d) => {
        setData(d);
        setErr(null);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : 'Failed'));
  }, []);
  useEffect(load, [load]);

  const shown = useMemo(() => {
    if (!data) return [];
    const needle = q.trim().toLowerCase();
    return data.projects.filter(
      (p) => (area === 'all' || p.area === area) && (!needle || JSON.stringify(p).toLowerCase().includes(needle)),
    );
  }, [data, area, q]);

  return (
    <div className="flex-1 overflow-y-auto px-6 py-8">
      <div className="max-w-6xl mx-auto w-full flex flex-col gap-4">
        <header className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h1 className="text-lg font-semibold" style={{ color: 'var(--color-text)' }}>
              Projects
            </h1>
            <p className="text-xs" style={muted}>
              Read-only view of your backlog files{data ? ` in ${data.dir}` : ''}. Edit the files, then refresh.
            </p>
          </div>
          <button
            onClick={load}
            className="text-xs px-2.5 py-1.5 rounded cursor-pointer flex items-center gap-1.5"
            style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
          >
            <RefreshCw size={12} /> Refresh
          </button>
        </header>

        {!data && (
          <div className="text-sm" style={faint}>
            {err ? `Could not load the projects: ${err}` : 'Loading…'}
          </div>
        )}

        {data && (
          <>
            {data.needs_you.length > 0 && (
              <section className="hud-panel p-3 flex flex-col gap-1.5" style={{ borderLeft: '2px solid var(--color-warning)' }}>
                <div className="hud-label">Needs you · {data.needs_you.length}</div>
                <ul className="flex flex-col gap-1">
                  {data.needs_you.map((n, i) => (
                    <li key={i} className="text-xs flex gap-2" style={{ color: 'var(--color-text)' }}>
                      <span style={faint}>☐</span>
                      <span>
                        {n.text} <span style={faint}>· {n.project}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <div className="flex items-center gap-2 flex-wrap">
              {['all', ...data.areas].map((a) => (
                <button
                  key={a}
                  onClick={() => setArea(a)}
                  className="text-[0.6875rem] px-2 py-1 rounded cursor-pointer"
                  style={{
                    background: area === a ? 'var(--color-accent-subtle)' : 'var(--color-bg-secondary)',
                    border: `1px solid ${area === a ? 'var(--color-accent)' : 'var(--color-border)'}`,
                    color: 'var(--color-text)',
                  }}
                >
                  {a === 'all' ? `All · ${data.projects.length}` : a}
                </button>
              ))}
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="search…"
                aria-label="Search projects"
                className="w-56 rounded px-2 py-1 text-xs ml-auto"
                style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)', color: 'var(--color-text)' }}
              />
            </div>

            <div className="grid gap-3 items-start" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))' }}>
              {COLUMNS.map((col) => {
                const items = shown.filter((p) => col.statuses.includes(p.status));
                return (
                  <section key={col.id} className="flex flex-col gap-2">
                    <div className="hud-label" style={{ borderBottom: `2px solid ${col.color}`, paddingBottom: 4 }}>
                      {col.title} · {items.length}
                    </div>
                    {items.length === 0 && (
                      <div className="text-[0.6875rem]" style={faint}>
                        nothing here
                      </div>
                    )}
                    {items.map((p) => (
                      <Card key={p.id} p={p} />
                    ))}
                  </section>
                );
              })}
            </div>

            {data.janitor_backlog.length > 0 && (
              <section className="flex flex-col gap-2">
                <button
                  onClick={() => setBacklogOpen(!backlogOpen)}
                  className="hud-label self-start cursor-pointer"
                  style={{ background: 'none', border: 'none' }}
                >
                  {backlogOpen ? '▾' : '▸'} Jarvis the Janitor backlog ·{' '}
                  {data.janitor_backlog.reduce((n, g) => n + g.items.length, 0)}
                </button>
                {backlogOpen &&
                  data.janitor_backlog.map((g) => (
                    <div key={g.group} className="hud-panel p-3 flex flex-col gap-1">
                      <div className="text-xs font-medium" style={{ color: 'var(--color-text)' }}>
                        {g.group}
                      </div>
                      <ul className="flex flex-col gap-1 pl-4 list-disc">
                        {g.items.map((it, i) => (
                          <li key={i} className="text-xs" style={muted}>
                            {it}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
              </section>
            )}

            {data.files.length > 0 && (
              <section className="flex flex-col gap-2">
                <button
                  type="button"
                  className="text-left text-xs font-medium cursor-pointer"
                  style={{ color: 'var(--color-text)' }}
                  onClick={() => setFilesOpen((v) => !v)}
                >
                  {filesOpen ? '▾' : '▸'} Files (Markdown) · {data.files.length}
                </button>
                {filesOpen && (
                  <div className="hud-panel p-3 flex flex-col gap-2">
                    <div className="text-xs" style={muted}>
                      Folder: {data.dir}
                    </div>
                    {data.files.map((f) => (
                      <div key={f.name} className="flex flex-col">
                        <div className="text-xs" style={{ color: 'var(--color-text)' }}>
                          {f.name}{' '}
                          <span style={muted}>
                            · {new Date(f.modified * 1000).toLocaleDateString()}
                          </span>
                        </div>
                        {f.purpose && (
                          <div className="text-xs" style={muted}>
                            {f.purpose}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}
