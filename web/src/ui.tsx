import { useEffect, useRef, type ReactNode } from 'react';
import { ChevronDown, CircleHelp, Search, X, type LucideIcon } from 'lucide-react';
import { platforms, statuses, type Job, type Platform as PlatformId } from './api';

export function PageTitle({ title, description, action }: {
  title: string; description: string; action?: ReactNode;
}) {
  return <div className="page-title"><div><h1>{title}</h1><p>{description}</p></div>{action}</div>;
}

export function Empty({ title, message, icon: Icon = CircleHelp }: {
  title: string; message?: string; icon?: LucideIcon;
}) {
  return <div className="empty"><span className="empty-icon"><Icon size={24} /></span>
    <strong>{title}</strong>{message && <p>{message}</p>}
  </div>;
}

export function SearchField({ label, placeholder, value, onChange, maxLength = 200 }: {
  label: string; placeholder: string; value: string; onChange: (value: string) => void; maxLength?: number;
}) {
  return <label className="search-field"><Search size={16} aria-hidden="true" />
    <input type="search" aria-label={label} placeholder={placeholder} value={value} maxLength={maxLength}
      onChange={(event) => onChange(event.target.value)} />
  </label>;
}

export function FilterSelect({ label, value, onChange, children }: {
  label: string; value: string; onChange: (value: string) => void; children: ReactNode;
}) {
  return <div className="select-wrap"><select aria-label={label} value={value}
    onChange={(event) => onChange(event.target.value)}>{children}</select><ChevronDown size={15} aria-hidden="true" /></div>;
}

export function Platform({ id }: { id: PlatformId }) {
  const tone = { douyin: 'douyin', xiaohongshu: 'red', bilibili: 'bili' }[id];
  return <span className={'platform ' + tone}><i />{platforms[id]}</span>;
}

const tones: Partial<Record<Job['status'], string>> = {
  queued: 'violet', running: 'violet', succeeded: 'green', failed: 'red',
  waiting_login: 'amber', partially_succeeded: 'amber', interrupted: 'amber',
};
export function Status({ value }: { value: Job['status'] }) {
  return <span className={'status ' + (tones[value] || '')}><i />{statuses[value]}</span>;
}

export function Values({ rows }: { rows: (readonly [string, string | number])[] }) {
  return <dl className="values">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

export function LoadState({ pending, error }: { pending: boolean; error: Error | null }) {
  if (error) return <p className="callout form-error" role="alert">{error.message}</p>;
  return pending ? <p className="callout" role="status">正在读取工作区…</p> : null;
}

export function Dialog({ title, description, close, drawer, fullWidthBody, children }: {
  title: string; description?: string; close: () => void; drawer?: boolean; fullWidthBody?: boolean; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className={'workspace-dialog ' + (drawer ? 'workspace-drawer' : '')}
    aria-label={title} onCancel={(event) => { event.preventDefault(); close(); }}>
    <header className="dialog-header"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>
      <button className="icon-button" aria-label="关闭" type="button" onClick={close}><X size={19} /></button>
    </header>
    <div className={'dialog-body' + (fullWidthBody ? ' full-width' : '')}>{children}</div>
  </dialog>;
}
