import type { Decision } from './types';

export function hhmmss(t: string | number): string {
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? '--:--:--' : d.toISOString().slice(11, 19);
}

export function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, '0')}s`;
}

const STOP_SHOWN_MS = 10 * 60_000; // as long as an alert stays in the agents' context

/** Executed stops that no later decision has revoked: the stops still in effect at viewT. */
export function activeStops(decisions: Decision[], viewT: number): Decision[] {
  const lifted = new Set(decisions.map((d) => d.revokes).filter(Boolean));
  return decisions
    .filter((d) => d.intent_action === 'stop' && d.outcome === 'executed' && !lifted.has(d._id))
    .filter((d) => viewT - Date.parse(d.t) < STOP_SHOWN_MS)
    .sort((a, b) => b.t.localeCompare(a.t));
}

/** Display name for a station id. The data keeps "HOLDSHORT" (contracts); people see "PHYLAX". */
const STATION_NAMES: Record<string, string> = { HOLDSHORT: 'PHYLAX' };
export function stationName(id: string): string {
  return STATION_NAMES[id] ?? id;
}
