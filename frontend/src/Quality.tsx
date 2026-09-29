import { useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import { dateTime, milliseconds, percentage } from './format';
import type { Sample, Target } from './types';
import { Empty, Icon, Modal, StatusBadge } from './ui';
import SpeedTest from './SpeedTest';

export function LatencyChart({ samples }: { samples: Sample[] }) {
  const plot = useMemo(() => {
    const width = 700, height = 145, left = 40, top = 12;
    const values = samples.filter(s => s.success && s.rtt_ms != null).map(s => s.rtt_ms!);
    const max = Math.max(5, Math.ceil(Math.max(...values, 0) * 1.2));
    const start = new Date(samples[0]?.timestamp ?? 0).getTime(), end = new Date(samples.at(-1)?.timestamp ?? 0).getTime();
    const intervals = samples.slice(1).map((s, i) => new Date(s.timestamp).getTime() - new Date(samples[i].timestamp).getTime()).filter(v => v > 0).sort((a,b) => a-b);
    const typical = intervals[Math.floor(intervals.length / 2)] || 60000;
    const points = samples.map(s => ({ x: left + (new Date(s.timestamp).getTime() - start) / Math.max(1, end - start) * (width-left-15), y: top + height - (s.rtt_ms ?? 0) / max * height, s }));
    const paths: string[] = []; let current = '';
    points.forEach((p, index) => {
      const gap = index > 0 && new Date(p.s.timestamp).getTime() - new Date(points[index - 1].s.timestamp).getTime() > typical * 2.5;
      if (!p.s.success || p.s.rtt_ms == null || gap) { if (current) paths.push(current); current = ''; }
      if (p.s.success && p.s.rtt_ms != null) current += `${current ? ' L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    }); if (current) paths.push(current);
    return { width, height, left, top, max, paths, points, start, end, values };
  }, [samples]);
  if (!samples.length) return <Empty title="Aguardando amostras">O gráfico será preenchido conforme o coletor realizar os testes.</Empty>;
  const time = (value: number) => new Date(value).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  return <div className="latency-chart"><svg viewBox="0 0 700 193" role="img" aria-label={`Histórico com ${samples.length} testes. Lacunas representam falhas ou ausência de coleta.`}>
    {[0, 0.5, 1].map(v => <g key={v}><line x1="40" x2="685" y1={12 + v * 145} y2={12 + v * 145} className="chart-grid"/><text x="31" y={16 + v * 145} textAnchor="end">{Math.round(plot.max * (1-v))}</text></g>)}
    <text x="3" y="12" className="chart-unit">ms</text>
    {plot.paths.map((path, index) => <path key={index} d={path} className="chart-line"/>)}
    {plot.points.map((p,i) => p.s.success && p.s.rtt_ms != null ? (samples.length < 40 && <circle key={i} cx={p.x} cy={p.y} r="2.5" fill="var(--accent)"><title>{dateTime(p.s.timestamp)}: {milliseconds(p.s.rtt_ms)}</title></circle>) : <path key={i} d={`M${p.x-2} 163l4 4m-4 0 4-4`} stroke="var(--red)"><title>{dateTime(p.s.timestamp)}: sem resposta</title></path>)}
    <text x="40" y="190">{time(plot.start)}</text><text x="685" y="190" textAnchor="end">{time(plot.end)}</text>
  </svg><div className="chart-legend"><span><i className="legend-line"/>Tempo de resposta</span><span><i className="legend-gap"/>Falha / intervalo sem coleta</span></div></div>;
}
function TargetEditor({ target, onClose, onSaved }: { target: Target | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ name: target?.name ?? '', host: target?.host ?? '', scope: target?.scope ?? 'local', kind: target?.kind ?? 'icmp', port: target?.port?.toString() ?? '443', query: target?.query ?? 'example.com', enabled: target?.enabled ?? true });
  const [saving, setSaving] = useState(false), [error, setError] = useState(''), [confirmDelete, setConfirmDelete] = useState(false);
  function field(key: string, value: string | boolean) { setForm(f => ({ ...f, [key]: value })); }
  async function save(e: FormEvent) {
    e.preventDefault(); setSaving(true); setError('');
    try { await api(target ? `/targets/${target.id}` : '/targets', { method: target ? 'PATCH' : 'POST', body: JSON.stringify({ ...form, name: form.name.trim(), host: form.host.trim(), port: form.kind === 'tcp' ? Number(form.port) : null, query: form.kind === 'dns' ? form.query.trim() : null }) }); onSaved(); onClose(); }
    catch(e) { setError(e instanceof Error ? e.message : 'Não foi possível salvar.'); } finally { setSaving(false); }
  }
  async function remove() { setSaving(true); setError(''); try { await api(`/targets/${target!.id}`, { method: 'DELETE' }); onSaved(); onClose(); } catch(e) { setError(e instanceof Error ? e.message : 'Não foi possível remover.'); } finally { setSaving(false); } }
  return <Modal title={target ? 'Editar alvo' : 'Adicionar alvo'} onClose={onClose}><p className="form-context">Escolha um destino e o teste usado para acompanhar sua conexão.</p><form onSubmit={save}>
    <label>Nome<input autoFocus required maxLength={120} value={form.name} onChange={e => field('name', e.target.value)} placeholder="Ex.: Roteador principal"/></label>
    <label>Endereço IP ou nome do destino<input required maxLength={253} value={form.host} onChange={e => field('host', e.target.value)} placeholder="Ex.: 192.168.0.1" spellCheck={false}/></label>
    <div className="form-row"><label>Rede<select value={form.scope} onChange={e => field('scope', e.target.value)}><option value="local">Local</option><option value="external">Externa</option></select></label><label>Tipo de teste<select value={form.kind} onChange={e => field('kind', e.target.value)}><option value="icmp">Ping (ICMP)</option><option value="tcp">Conexão TCP</option><option value="dns">Consulta DNS</option></select></label></div>
    {form.kind === 'tcp' && <label>Porta TCP<input required type="number" min={1} max={65535} value={form.port} onChange={e => field('port', e.target.value)}/></label>}
    {form.kind === 'dns' && <label>Nome consultado no servidor DNS<input required maxLength={253} value={form.query} onChange={e => field('query', e.target.value)} spellCheck={false}/><small>O endereço acima deve ser o servidor DNS a testar.</small></label>}
    <label className="check-label"><input type="checkbox" checked={form.enabled} onChange={e => field('enabled', e.target.checked)}/>Monitoramento habilitado</label>
    {error && <p className="form-error" role="alert">{error}</p>}
    {confirmDelete && <div className="delete-confirm"><p>Remover este alvo e encerrar seu monitoramento?</p><button className="button danger" type="button" disabled={saving} onClick={() => void remove()}>Confirmar remoção</button><button className="text-button" type="button" onClick={() => setConfirmDelete(false)}>Manter alvo</button></div>}
    <footer className="modal-actions">{target && !confirmDelete && <button className="text-button danger-text" type="button" onClick={() => setConfirmDelete(true)} disabled={saving}>Remover alvo</button>}<span className="spacer"/><button className="button secondary" type="button" onClick={onClose} disabled={saving}>Cancelar</button><button className="button primary" disabled={saving}>{saving ? 'Salvando…' : 'Salvar alvo'}</button></footer>
  </form></Modal>;
}
export default function Quality({ targets, compact = false, refresh }: { targets: Target[]; compact?: boolean; refresh: () => void }) {
  const [scope, setScope] = useState<'local'|'external'>('local');
  const [selected, setSelected] = useState<number | null>(null);
  const [hours, setHours] = useState(24);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [historyError, setHistoryError] = useState(''), [historyLoading, setHistoryLoading] = useState(false);
  const [editor, setEditor] = useState<Target | 'new' | null>(null);
  const scoped = targets.filter(t => t.scope === scope);
  const active = scoped.find(t => t.id === selected) ?? scoped[0];
  const targetId = active?.id;
  const lastChecked = active?.last_checked;
  useEffect(() => {
    if (targetId == null) { setSamples([]); return; }
    const controller = new AbortController();
    setHistoryLoading(true); setHistoryError(''); setSamples([]);
    api<Sample[]>(`/targets/${targetId}/history?hours=${hours}`, { signal: controller.signal }).then(setSamples).catch(e => { if (!controller.signal.aborted) setHistoryError(e instanceof Error ? e.message : 'Histórico indisponível.'); }).finally(() => { if (!controller.signal.aborted) setHistoryLoading(false); });
    return () => controller.abort();
  }, [targetId, hours, lastChecked]);
  const successes = samples.filter(s => s.success && s.rtt_ms != null);
  const average = successes.length ? successes.reduce((sum,s) => sum+s.rtt_ms!, 0) / successes.length : null;
  const peak = successes.length ? Math.max(...successes.map(s => s.rtt_ms!)) : null;
  const loss = samples.length ? samples.filter(s => !s.success).length / samples.length * 100 : null;
  return <><section className="panel quality-panel"><header className="panel-heading"><h2>Qualidade da conexão</h2>{!compact && <button className="button secondary small" onClick={() => setEditor('new')}><Icon name="plus" size={16}/>Adicionar alvo</button>}</header>
    <div className="quality-toolbar"><div className="segmented" aria-label="Rede dos alvos"><button aria-pressed={scope === 'local'} className={scope === 'local' ? 'selected' : ''} onClick={() => setScope('local')}>Local</button><button aria-pressed={scope === 'external'} className={scope === 'external' ? 'selected' : ''} onClick={() => setScope('external')}>Externa</button></div><select value={hours} onChange={e => setHours(Number(e.target.value))} aria-label="Período do histórico"><option value={1}>Última hora</option><option value={6}>Últimas 6 horas</option><option value={24}>Últimas 24 horas</option><option value={168}>Últimos 7 dias</option></select></div>
    {scoped.length ? <><div className="target-selection"><select aria-label="Alvo do gráfico" value={active!.id} onChange={e => setSelected(Number(e.target.value))}>{scoped.map(t => <option key={t.id} value={t.id}>{t.name} · {t.host}</option>)}</select><StatusBadge status={active!.status} disabled={!active!.enabled}/></div>
      {historyError ? <p className="inline-error" role="alert">{historyError}</p> : historyLoading ? <div className="chart-loading" role="status">Carregando histórico…</div> : <LatencyChart samples={samples}/>}
      <div className="chart-metrics"><div><span>Média</span><strong>{milliseconds(average)}</strong></div><div><span>Máxima</span><strong>{milliseconds(peak)}</strong></div><div><span>Sem resposta</span><strong>{percentage(loss)}</strong></div><div><span>Testes</span><strong>{samples.length}</strong></div></div>
    </> : <Empty title={`Nenhum alvo ${scope === 'local' ? 'local' : 'externo'} cadastrado`}>Adicione um destino para acompanhar o tempo de resposta e as falhas.</Empty>}
    {!compact && <><div className="table-scroll target-table"><table><thead><tr><th>Alvo</th><th>Teste</th><th>Estado</th><th>Último tempo</th><th>Sem resposta</th><th><span className="sr-only">Ações</span></th></tr></thead><tbody>{scoped.map(t => <tr key={t.id}><td><strong>{t.name}</strong><small className="mono">{t.host}{t.kind === 'tcp' ? `:${t.port}` : ''}</small></td><td>{t.kind.toUpperCase()}{t.kind === 'dns' && <small>{t.query}</small>}</td><td><StatusBadge status={t.status} disabled={!t.enabled}/></td><td title={dateTime(t.last_checked)}>{milliseconds(t.rtt_ms)}</td><td>{percentage(t.loss_pct)}<small>{t.samples_count} testes</small></td><td><button className="text-button" onClick={() => setEditor(t)} aria-label={`Editar alvo ${t.name}`}>Editar</button></td></tr>)}</tbody></table></div><div className="panel-footnote">Os valores do gráfico usam o período selecionado. “Sem resposta” é a proporção de testes que falharam; bloqueio de ICMP também pode causar falhas.</div></>}
    {editor && <TargetEditor target={editor === 'new' ? null : editor} onClose={() => setEditor(null)} onSaved={refresh}/>}
  </section>{!compact && <SpeedTest/>}</>;
}
