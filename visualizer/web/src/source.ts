import { createMockSource } from './mock';
import { ApiError, type DataSource } from './types';

const TOWER_ID = 'LGA';
const trim = (u: string) => u.replace(/\/+$/, '');
export const VIZ_API = trim(import.meta.env.VITE_VIZ_API || 'http://localhost:8100');
export const TOWER_API = trim(import.meta.env.VITE_TOWER_API || 'http://localhost:8000');

async function fail(res: Response): Promise<never> {
  let msg = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    const d = body?.detail ?? body?.error ?? body?.message;
    // the tower answers {detail: {error: "not_lane_owner", detail: "..."}}
    if (d && typeof d === 'object' && (d.error || d.detail)) msg = [d.error, d.detail].filter(Boolean).join(': ');
    else if (d) msg = typeof d === 'string' ? d : JSON.stringify(d);
  } catch {
    /* keep the status text */
  }
  throw new ApiError(res.status, msg);
}

async function get<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const q = new URLSearchParams({ tower_id: TOWER_ID });
  for (const [k, v] of Object.entries(params)) if (v !== undefined) q.set(k, String(v));
  const res = await fetch(`${VIZ_API}${path}?${q}`);
  if (!res.ok) await fail(res);
  return res.json();
}

async function post(path: string, body: unknown): Promise<void> {
  const res = await fetch(`${TOWER_API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) await fail(res);
}

function createHttpSource(): DataSource {
  return {
    mock: false,
    world: () => get('/viz/world'),
    bounds: () => get('/viz/bounds'),
    snapshot: (t) => get('/viz/snapshot', { t }),
    tracks: (from, to, stepS) => get('/viz/tracks', { from, to, step_s: stepS }),
    timeline: (from, to) => get('/viz/timeline', { from, to }),
    clips: (streamId, from, to) => get(`/viz/streams/${encodeURIComponent(streamId)}/clips`, { from, to }),
    audioUrl: (clipId) => `${VIZ_API}/viz/audio/${encodeURIComponent(clipId)}`,
    live(onMsg, onStatus) {
      let ws: WebSocket | null = null;
      let closed = false;
      let retry: number | undefined;
      const open = () => {
        ws = new WebSocket(`${VIZ_API.replace(/^http/, 'ws')}/viz/live?tower_id=${TOWER_ID}`);
        ws.onopen = () => onStatus('open');
        ws.onmessage = (ev) => {
          try {
            onMsg(JSON.parse(ev.data));
          } catch {
            /* ignore a malformed frame */
          }
        };
        ws.onclose = () => {
          onStatus('closed');
          if (!closed) retry = window.setTimeout(open, 2000);
        };
        ws.onerror = () => ws?.close();
      };
      open();
      return () => {
        closed = true;
        clearTimeout(retry);
        ws?.close();
      };
    },
    approve: (decision_id, controller_id) => post('/control/approve', { decision_id, controller_id }),
    reject: (decision_id, controller_id, reason) => post('/control/reject', { decision_id, controller_id, reason }),
    revoke: (decision_id, controller_id) => post('/control/revoke', { decision_id, controller_id }),
    setPermission: (agent, patch) => post(`/control/permissions/${agent}`, patch),
    audit: async (q) => {
      const res = await fetch(`${TOWER_API}/control/audit?q=${encodeURIComponent(q)}`);
      if (!res.ok) await fail(res);
      return res.json();
    },
  };
}

export function createSource(): DataSource {
  const mock = new URLSearchParams(window.location.search).get('mock') === '1';
  return mock ? createMockSource() : createHttpSource();
}
