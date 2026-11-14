import { useEffect, useRef, useState } from 'react';

export interface MenuOption<T extends string | number> {
  value: T;
  label: string;
  detail?: string; // a second, muted line
  badge?: number;
  badgeTone?: 'stop' | 'warn';
}

interface Props<T extends string | number> {
  value: T;
  options: MenuOption<T>[];
  onChange: (v: T) => void;
  label: string; // accessible name
  disabled?: boolean;
  up?: boolean; // open upwards (bottom bar)
  prefix?: string;
  left?: boolean; // align the list to the button's left edge
}

/** The one dropdown of the app: a 28px button, a popover list, keyboard and outside-click handling. */
export function Menu<T extends string | number>({ value, options, onChange, label, disabled, up, prefix, left }: Props<T>) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const cur = options.find((o) => o.value === value);
  useEffect(() => {
    if (!open) return;
    const out = (e: MouseEvent) => root.current && !root.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener('mousedown', out);
    return () => document.removeEventListener('mousedown', out);
  }, [open]);
  const show = () => {
    setActive(Math.max(0, options.findIndex((o) => o.value === value)));
    setOpen(true);
  };
  const pick = (i: number) => {
    setOpen(false);
    btn.current?.focus();
    if (options[i] && options[i].value !== value) onChange(options[i].value);
  };
  const key = (e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        e.stopPropagation();
        show();
      }
      return;
    }
    e.stopPropagation();
    if (e.key === 'Escape') {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => (a + 1) % options.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => (a - 1 + options.length) % options.length);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      pick(active);
    } else if (e.key === 'Tab') setOpen(false);
  };
  return (
    <div className={`menu ${open ? 'open' : ''} ${up ? 'up' : ''} ${left ? 'left' : ''}`} ref={root} onKeyDown={key} onClick={(e) => e.stopPropagation()}>
      <button ref={btn} type="button" className="menu-btn" aria-haspopup="listbox" aria-expanded={open} aria-label={label} disabled={disabled} onClick={() => (open ? setOpen(false) : show())}>
        {prefix && <span className="menu-prefix">{prefix}</span>}
        <span>{cur?.label ?? String(value)}</span>
        {!!cur?.badge && <i className={`dotc ${cur.badgeTone === 'stop' ? 'is-stop' : 'is-warn'}`} />}
        <svg width="8" height="5" viewBox="0 0 8 5" aria-hidden>
          <path d="M1 1l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.25" />
        </svg>
      </button>
      {open && (
        <ul className="menu-list" role="listbox" aria-label={label}>
          {options.map((o, i) => (
            <li key={String(o.value)} role="option" aria-selected={o.value === value} className={`${i === active ? 'active' : ''} ${o.value === value ? 'sel' : ''} ${o.detail ? 'tall' : ''}`} onMouseEnter={() => setActive(i)} onClick={() => pick(i)}>
              <svg width="10" height="8" viewBox="0 0 10 8" aria-hidden>
                {o.value === value && <path d="M1 4l3 3 5-6" fill="none" stroke="currentColor" strokeWidth="1.5" />}
              </svg>
              <span className="menu-main">
                <span>{o.label}</span>
                {o.detail && <small>{o.detail}</small>}
              </span>
              {!!o.badge && <span className={`countpill ${o.badgeTone === 'stop' ? 'c-stop' : 'c-warn'}`}>{o.badge}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
