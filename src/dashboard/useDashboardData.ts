import { useCallback, useEffect, useRef, useState } from 'react';
import { DashboardApiError, fetchDashboard } from './api';
import type { DashboardState } from './types';

export function useDashboardData(pollIntervalMs = 0): { state: DashboardState; refresh: () => void } {
  const [state, setState] = useState<DashboardState>({ status: 'loading' });
  const abortRef = useRef<AbortController | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const seq = ++requestSeq.current;
    setState({ status: 'loading' });
    try {
      const data = await fetchDashboard(controller.signal);
      if (controller.signal.aborted || seq !== requestSeq.current) return;
      setState({ status: 'ready', data });
    } catch (error) {
      if (controller.signal.aborted || seq !== requestSeq.current) return;
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (error instanceof DashboardApiError) {
        setState({ status: 'error', code: error.code, message: error.message });
      } else {
        setState({ status: 'error', code: 'DASHBOARD_FETCH_FAILED', message: 'Unexpected dashboard failure' });
      }
    }
  }, []);

  useEffect(() => {
    void load();
    if (pollIntervalMs <= 0) return;
    const timer = setInterval(() => void load(), pollIntervalMs);
    return () => {
      clearInterval(timer);
      abortRef.current?.abort();
    };
  }, [load, pollIntervalMs]);

  return { state, refresh: () => void load() };
}
