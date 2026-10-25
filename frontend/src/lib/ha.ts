import { useCallback, useEffect, useRef, useState } from 'react';
import { getBase } from './api';

export interface HaDashboard {
  ok: boolean;
  collected_at: number;
  cached?: boolean;
  stale?: boolean;
  errors: string[];
  system?: { version?: string; name?: string; state?: string };
  repairs?: {
    total: number;
    errors: number;
    warnings: number;
    by_domain: { domain: string; count: number }[];
    automation_errors: number;
    backup_issues: number;
    upcoming_breaking: { domain: string; issue: string; breaks_in: string }[];
  };
  entities?: {
    total: number;
    unavailable: number;
    worst_domains: { domain: string; unavailable: number; count: number }[];
    updates_available: number;
    automations_total: number;
    automations_unavailable: number;
  };
  integrations?: { total: number; failed: number; states: Record<string, number> };
  motion?: {
    total: number;
    offline: number;
    offline_entities: { entity_id: string; name: string; state: string }[];
  };
  logs?: {
    critical: number;
    errors: number;
    warnings: number;
    by_component: { component: string; count: number }[];
    recent: { ts: string; level: string; component: string; message: string }[];
    lines_scanned: number;
  };
}

const POLL_MS = 60_000;

/** Polls the read-only Home Assistant dashboard endpoint. */
export function useHaDashboard() {
  const [data, setData] = useState<HaDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      const res = await fetch(`${getBase()}/v1/ha/dashboard${refresh ? '?refresh=1' : ''}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as HaDashboard;
      if (!aliveRef.current) return;
      setData(json);
      setError(json.ok ? null : (json.errors?.[0] ?? 'Home Assistant unavailable'));
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : 'Request failed');
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    void load();
    const id = setInterval(() => void load(), POLL_MS);
    return () => {
      aliveRef.current = false;
      clearInterval(id);
    };
  }, [load]);

  return { data, loading, error, refresh: () => load(true) };
}
