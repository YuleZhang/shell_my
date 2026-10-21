import { useCallback, useEffect, useRef, useState } from 'react';
import type { JobDiscoveryResponse, JobResponse } from '../../shared/contracts';
import { isActiveJob } from './jobs';
import { errorMessage, request } from './client';

export function useResource<T>(path: string) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  const load = useCallback(async (writeBody?: unknown, writePath?: string) => {
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    setLoading(true); setError(null);
    try {
      const value = await request<T>(writePath ?? path, { signal: controller.signal,
        ...(writePath ? { method: 'POST' as const, body: writeBody } : {}) });
      if (!controller.signal.aborted) setData(value);
      return value;
    } catch (e) { if (!controller.signal.aborted) setError(errorMessage(e)); return null; }
    finally { if (!controller.signal.aborted) setLoading(false); }
  }, [path]);
  useEffect(() => { setData(null); void load(); return () => active.current?.abort(); }, [load]);
  return { data, setData, loading, error, load };
}
export type Resource<T> = ReturnType<typeof useResource<T>>;
export function useJob() {
  const [response, setResponse] = useState<JobResponse | JobDiscoveryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(true);
  const [reading, setReading] = useState(false);
  const selection = useRef(0);
  const readController = useRef<AbortController | null>(null);
  const jobId = response?.job.id;
  const state = response?.job.state;
  useEffect(() => () => { selection.current++; readController.current?.abort(); }, []);
  useEffect(() => {
    if (!jobId || !state || !isActiveJob(state) || paused) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await request<JobDiscoveryResponse>(`/api/jobs/${encodeURIComponent(jobId)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setResponse(value); setError(null);
        if (isActiveJob(value.job.state)) timer = setTimeout(poll, 1600);
      } catch (e) { if (!controller.signal.aborted) { setError(errorMessage(e)); setPaused(true); } }
    };
    timer = setTimeout(poll, 500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, state, paused]);
  const start = (value: JobResponse) => {
    selection.current++; readController.current?.abort(); setReading(false);
    setResponse(value); setError(null); setPaused(false);
  };
  const inspect = async (id: string) => {
    const sequence = ++selection.current;
    readController.current?.abort();
    const controller = new AbortController(); readController.current = controller;
    setPaused(true); setResponse(null); setReading(true); setError(null);
    try {
      const value = await request<JobDiscoveryResponse>(`/api/jobs/${encodeURIComponent(id)}`, { signal: controller.signal });
      if (sequence !== selection.current || controller.signal.aborted) return null;
      setResponse(value); return value;
    } catch (e) { if (!controller.signal.aborted) setError(errorMessage(e)); return null; }
    finally { if (sequence === selection.current && !controller.signal.aborted) setReading(false); }
  };
  return { response, error, paused, reading, setPaused, start, inspect,
    active: !!state && isActiveJob(state), busy: !!state && isActiveJob(state) && !paused };
}
