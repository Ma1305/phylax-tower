import { useEffect, useRef, useState } from 'react';
import type { AgentMode, DataSource, Permission, PermissionAgent, PermissionPatch, PermissionTier } from '../types';

const AGENTS: PermissionAgent[] = ['listener', 'weather', 'safety', 'procedures'];
const TIERS: PermissionTier[] = ['autonomous', 'needs_approval', 'never'];
const MODES: AgentMode[] = ['reactive', 'proactive'];
const cap = (s: string) => s[0].toUpperCase() + s.slice(1);

/** The agents as pill toggles in the top bar; the caret opens mode and tier. */
export function AgentPills({ ds, permissions, by, readOnly }: { ds: DataSource; permissions: Permission[]; by: string; readOnly: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState('');
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const h = (e: MouseEvent) => root.current && !root.current.contains(e.target as Node) && setOpen(null);
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, []);
  const get = (id: string) => permissions.find((p) => p._id === id || p.agent === id);
  const master = get('system');
  const masterOn = master?.enabled ?? true;
  const set = async (agent: PermissionAgent, patch: Omit<PermissionPatch, 'by'>) => {
    setError('');
    try {
      await ds.setPermission(agent, { by, ...patch });
    } catch (e) {
      setError(`${agent}: ${e instanceof Error ? e.message : String(e)}`);
      setTimeout(() => setError(''), 5000);
    }
  };
  return (
    <div className="agents-bar" ref={root}>
      {AGENTS.map((id) => {
        const p = get(id);
        const on = !!p?.enabled && masterOn;
        const big = id === 'safety';
        return (
          <div key={id} className={`apill ${on ? 'on' : 'off'} ${big ? 'big' : ''}`} data-agent={id}>
            <button type="button" role="switch" aria-checked={p?.enabled ?? false} aria-label={`${id} enabled`} disabled={readOnly || !p} className={`toggle ${p?.enabled ? 'on' : ''}`} onClick={() => set(id, { enabled: !p?.enabled })}>
              <span />
            </button>
            <span className="apill-name">
              <b>{cap(id)}</b>
              {big && <small>{p ? (on ? `${p.mode} · ${p.tier.replace('_', ' ')}` : masterOn ? 'off' : 'held by master') : 'no record'}</small>}
            </span>
            <button type="button" className="caret" aria-label={`${id} settings`} onClick={() => setOpen(open === id ? null : id)}>
              ▾
            </button>
            {open === id && (
              <div className="popover">
                <div className="pop-title">{cap(id)} agent</div>
                <label>Mode</label>
                <div className="seg small" role="group" aria-label={`${id} mode`}>
                  {MODES.map((m) => (
                    <button key={m} className={p?.mode === m ? 'on' : ''} disabled={readOnly || !p} onClick={() => p?.mode !== m && set(id, { mode: m })} title={m === 'reactive' ? 'may only warn' : 'may also stop or propose'}>
                      {m}
                    </button>
                  ))}
                </div>
                <label>Tier</label>
                <div className="seg small" role="group" aria-label={`${id} tier`}>
                  {TIERS.map((t) => (
                    <button key={t} className={p?.tier === t ? 'on' : ''} disabled={readOnly || !p} onClick={() => p?.tier !== t && set(id, { tier: t })}>
                      {t.replace('_', ' ')}
                    </button>
                  ))}
                </div>
                {readOnly && <p className="pop-note">History: switches are read-only.</p>}
              </div>
            )}
          </div>
        );
      })}
      <label className={`master ${masterOn ? 'on' : 'off'}`} title="Master switch: turns every agent off at once">
        <button type="button" role="switch" aria-checked={masterOn} aria-label="Master switch" disabled={readOnly || !master} className={`toggle sm ${masterOn ? 'on' : ''}`} onClick={() => set('system', { enabled: !masterOn })}>
          <span />
        </button>
        <span>Master</span>
      </label>
      {error && (
        <div className="top-error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
