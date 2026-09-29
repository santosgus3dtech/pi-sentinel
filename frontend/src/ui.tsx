import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import type { Status } from './types';
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    home: <><path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/></>,
    devices: <><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></>,
    computer: <><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></>,
    smartphone: <><rect x="7" y="2" width="10" height="20" rx="2"/><path d="M10 5h4m-3 14h2"/></>,
    gamepad: <><path d="M7 8h10a4 4 0 0 1 3.7 5.5l-1.4 3.4a2 2 0 0 1-3.2.8L14.4 16H9.6l-1.7 1.7a2 2 0 0 1-3.2-.8l-1.4-3.4A4 4 0 0 1 7 8Z"/><path d="M7 12h4m-2-2v4m6-2h.01m3 2h.01"/></>,
    router: <><rect x="3" y="10" width="18" height="9" rx="2"/><path d="M7 14h.01m3 0h.01M8 10 5 4m11 6 3-6M9 7a5 5 0 0 1 6 0"/></>,
    visitors: <><circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2"/><path d="M3 20a6 6 0 0 1 12 0m0-5a5 5 0 0 1 6 5"/></>,
    unknownDevice: <><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M10 9a2 2 0 1 1 3 1.7c-.7.4-1 1-1 1.8m0 3.5h.01"/></>,
    printer3d: <><path d="M5 3h14v5H5zM7 8v11h10V8M9 12h6M12 8v4M9 19v2m6-2v2"/><path d="M10 15h4v2h-4z"/></>,
    quality: <><path d="M5 19V9m7 10V4m7 15v-7"/></>,
    incident: <><path d="m12 3 10 18H2Z"/><path d="M12 9v5m0 3h.01"/></>,
    arrow: <><path d="M5 12h14m-5-5 5 5-5 5"/></>,
    external: <><path d="M13 4h7v7m0-7L10 14m-1-9H4v15h15v-5"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
    search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3"/></>,
    close: <path d="m6 6 12 12M6 18 18 6"/>,
    check: <path d="m5 12 4 4L19 6"/>,
    settings: <><circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/></>,
    shield: <><path d="M12 3 4 6v5c0 5.2 3.4 8.5 8 10 4.6-1.5 8-4.8 8-10V6Z"/><path d="m9 12 2 2 4-5"/></>,
    key: <><circle cx="8" cy="15" r="4"/><path d="m11 12 8-8m-3 3 2 2m-5 1 2 2"/></>,
    logout: <><path d="M10 4H5v16h5m4-4 4-4-4-4m4 4H9"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.devices}</svg>;
}
export function Brand() { return <div className="brand"><img src="/favicon.svg" alt=""/><div><strong>PiSentinel</strong><span>MONITORAMENTO DE REDE</span></div></div>; }
export function StatusBadge({ status, disabled = false }: { status: Status; disabled?: boolean }) {
  const label = disabled ? 'Pausado' : status === 'online' ? 'Respondendo' : status === 'offline' ? 'Sem resposta' : 'Aguardando';
  return <span className={`status ${disabled ? 'unknown' : status}`}><i/>{label}</span>;
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) { return <div className="empty"><Icon name="quality" size={30}/><strong>{title}</strong>{children && <p>{children}</p>}</div>; }
export function Modal({ title, children, onClose, className = '' }: { title: string; children: ReactNode; onClose: () => void; className?: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const el = dialog.current; el?.showModal(); return () => { el?.close(); }; }, []);
  return <dialog ref={dialog} className={`modal ${className}`.trim()} onCancel={onClose} onClick={e => { if (e.target === dialog.current) { const rect = dialog.current.getBoundingClientRect(); if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) onClose(); } }} aria-labelledby="modal-title"><div className="modal-heading"><h2 id="modal-title">{title}</h2><button className="icon-button" type="button" aria-label="Fechar" onClick={onClose}><Icon name="close"/></button></div>{children}</dialog>;
}
