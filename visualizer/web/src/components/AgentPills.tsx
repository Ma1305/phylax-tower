import { useState } from 'react';
import type { AgentMode, AgentState, DataSource, Permission, PermissionAgent, PermissionPatch, PermissionTier } from '../types';
import { Menu } from './Menu';

const AGENTS: { id: PermissionAgent; what: string }[] = [
  { id: 'safety', what: 'Rules: runway incursions, occupied runway' },
  { id: 'procedures', what: 'Advisor: proposes instructions' },
  { id: 'weather', what: 'Visibility and the rule set in force' },
  { id: 'listener', what: 'Speech to transcript' },
];
const TIERS: PermissionTier[] = ['autonomous', 'needs_approval', 'never'];
const MODES: AgentMode[] = ['reactive', 'proactive'];
const cap = (s: string) => s[0].toUpperCase() + s.slice(1);
const find = (permissions: Permission[], id: string) => permissions.find((p) => p._id === id || p.agent === id);

/** The whole agent state in a few words, for the top bar chip. */
export function agentSummary(permissions: Permission[]): { tone: 'ok' | 'warn'; text: string; title: string } {
  if (permissions.length === 0) return { tone: 'warn', text: 'Agents: no records', title: 'No permission records loaded' };
  const master = find(permissions, 'system');
  if (master && !master.enabled) return { tone: 'warn', text: 'Agents held · master off', title: 'The master switch is off: no agent acts' };
  const off = AGENTS.filter((a) => !find(permissions, a.id)?.enabled).map((a) => a.id);
  const safety = find(permissions, 'safety');
  if (off.length) return { tone: 'warn', text: `${off.length === AGENTS.length ? 'All agents' : off.map(cap).join(', ')} off`, title: 'Open Settings to switch agents on' };
  if (safety && safety.tier !== 'autonomous') return { tone: 'warn', text: `Agents on · safety ${safety.tier.replace('_', ' ')}`, title: 'Safety stops wait for a controller' };
  return { tone: 'ok', text: `Agents on · safety ${safety?.tier.replace('_', ' ') ?? 'unknown'}`, title: 'Every agent is on' };
}

function identity(st: AgentState | undefined): string {
  const id = st?.identity;
  if (!id) return 'no database identity reported';
  if (id.kind === 'generated') return `Atlas user ${id.username ?? '(unnamed)'}${id.role ? ` · role ${id.role}` : ''}`;
  if (id.kind === 'static') return 'static Atlas user';
  return 'shared connection';
}

/** Settings sheet: every agent's switch, mode, tier and database identity, and the master switch. */
export function AgentSettings({ ds, permissions, agentState, by, readOnly }: { ds: DataSource; permissions: Permission[]; agentState: AgentState[]; by: string; readOnly: boolean }) {
  const [error, setError] = useState('');
  const master = find(permissions, 'system');
  const masterOn = master?.enabled ?? true;
  const set = async (agent: PermissionAgent, patch: Omit<PermissionPatch, 'by'>) => {
    setError('');
    try {
      await ds.setPermission(agent, { by, ...patch });
    } catch (e) {
      setError(`${agent}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };
  return (
    <div className="settings">
      {readOnly && <p className="set-note">History: settings are read-only.</p>}
      <div className={`set-row set-master ${masterOn ? '' : 'off'}`}>
        <button type="button" role="switch" aria-checked={masterOn} aria-label="Master switch" disabled={readOnly || !master} className={`toggle ${masterOn ? 'on' : ''}`} onClick={() => set('system', { enabled: !masterOn })}>
          <span />
        </button>
        <div className="set-name">
          <b>Master switch</b>
          <span>{masterOn ? 'Agents may act within their own settings' : 'Off: every agent is held'}</span>
        </div>
      </div>
      {AGENTS.map(({ id, what }) => {
        const p = find(permissions, id);
        const st = agentState.find((s) => s._id === `${id}_agent`) ?? agentState.find((s) => s._id === id);
        return (
          <div key={id} className={`set-row ${p?.enabled && masterOn ? '' : 'off'}`} data-agent={id}>
            <button type="button" role="switch" aria-checked={p?.enabled ?? false} aria-label={`${id} enabled`} disabled={readOnly || !p} className={`toggle ${p?.enabled ? 'on' : ''}`} onClick={() => set(id, { enabled: !p?.enabled })}>
              <span />
            </button>
            <div className="set-name">
              <b>{cap(id)}</b>
              <span>{what}</span>
              <span className="mono ident">{identity(st)}</span>
            </div>
            <Menu label={`${id} mode`} prefix="Mode" value={p?.mode ?? 'reactive'} disabled={readOnly || !p} onChange={(m) => set(id, { mode: m })} options={MODES.map((m) => ({ value: m, label: m }))} />
            <Menu label={`${id} tier`} prefix="Tier" value={p?.tier ?? 'needs_approval'} disabled={readOnly || !p} onChange={(t) => set(id, { tier: t })} options={TIERS.map((t) => ({ value: t, label: t.replace('_', ' ') }))} />
          </div>
        );
      })}
      {error && (
        <div className="req-error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
