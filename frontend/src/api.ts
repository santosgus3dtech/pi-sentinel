import { useCallback, useEffect, useRef, useState } from 'react';
import type { DashboardData, Device, Incident, Settings, Summary, Target } from './types';
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, credentials: 'same-origin', headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === 'string' ? body.detail : `A solicitação falhou (${response.status}).`;
    if (response.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event('pisentinel:unauthorized'));
    throw new Error(detail);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export function useDashboard(role: 'admin' | 'guest') {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const inFlight = useRef(false);
  const alive = useRef(true);
  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const [summary, devices, targets, incidents, settings] = role === 'admin' ? await Promise.all([
        api<Summary>('/summary'), api<Device[]>('/devices'), api<Target[]>('/targets'), api<Incident[]>('/incidents?limit=100'), api<Settings>('/settings'),
      ]) : [await api<Summary>('/summary'), [], [], [], { label: '', cidr: '', interface: '', ssids: [], interval_seconds: 0, discovery_interval_seconds: 0, retention_days: 0, failure_threshold: 0, recovery_threshold: 0 } as Settings];
      if (alive.current) { setData({ summary, devices, targets, incidents, settings }); setError(''); }
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : 'Não foi possível acessar o PiSentinel.'); }
    finally { inFlight.current = false; if (alive.current) setLoading(false); }
  }, [role]);
  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 15000);
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener('visibilitychange', visible);
    return () => { alive.current = false; clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [refresh]);
  return { data, error, loading, refresh };
}
