import { useState } from 'react';
import type { AgentMode, DataSource, Permission, PermissionAgent, PermissionPatch, PermissionTier } from '../types';

interface Props {
  ds: DataSource;
  permissions: Permission[];
  by: string;
  readOnly: boolean;
}

const AGENTS: { id: PermissionAgent; what: string }[] = [
  { id: 'safety', what: 'rules: incursions, occupied runway' },
  { id: 'procedures', what: 'advisor: proposes instructions' },
  { id: 'weather', what: 'visibility and rule set' },
  { id: 'listener', what: 'speech to transcript' },
];
const TIERS: PermissionTier[] = ['needs_approval', 'autonomous', 'never'];
const MODES: AgentMode[] = ['reactive', 'proactive'];

export function Agents({ ds, permissions, by, readOnly }: Props) {
  const [error, setError] = useState('');
  const get = (id: string) => permissions.find((p) => p._id === id || p.agent === id);
  const master = get('system');
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
    <section className="panel agents">
      <header className="panel-head">
        <h2>Agent switches</h2>
        <label className={`master ${masterOn ? 'on' : 'off'}`} title="Master switch: turns every agent off at once">
          <span>MASTER {masterOn ? 'ON' : 'OFF'}</span>
          <Toggle on={masterOn} disabled={readOnly || !master} onChange={(v) => set('system', { enabled: v })} label="Master switch" />
        </label>
      </header>
      <div className="panel-body">
        {AGENTS.map(({ id, what }) => {
          const p = get(id);
          const off = !p?.enabled || !masterOn;
          return (
            <div key={id} className={`agent ${off ? 'off' : ''}`}>
              <div className="agent-name">
                <Toggle on={p?.enabled ?? false} disabled={readOnly || !p} onChange={(v) => set(id, { enabled: v })} label={`${id} enabled`} />
                <strong title={!p ? 'no permission record' : !masterOn ? 'held by master switch' : p.enabled ? what : 'disabled'}>{id}</strong>
              </div>
              <div className="agent-ctl">
                <div className="seg small" role="group" aria-label={`${id} mode`}>
                  {MODES.map((m) => (
                    <button key={m} className={p?.mode === m ? 'on' : ''} disabled={readOnly || !p} onClick={() => p?.mode !== m && set(id, { mode: m })} title={m === 'reactive' ? 'may only warn' : 'may also stop or propose'}>
                      {m}
                    </button>
                  ))}
                </div>
                <select value={p?.tier ?? 'needs_approval'} disabled={readOnly || !p} onChange={(e) => set(id, { tier: e.target.value as PermissionTier })} aria-label={`${id} tier`}>
                  {TIERS.map((t) => (
                    <option key={t} value={t}>
                      {t.replace('_', ' ')}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          );
        })}
        {error && (
          <div className="req-error" role="alert">
            {error}
          </div>
        )}
      </div>
    </section>
  );
}

function Toggle({ on, disabled, onChange, label }: { on: boolean; disabled?: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} disabled={disabled} className={`toggle ${on ? 'on' : ''}`} onClick={() => onChange(!on)}>
      <span />
    </button>
  );
}
