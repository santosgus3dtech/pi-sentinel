import { useCallback, useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import { dateTime, duration, milliseconds, percentage } from './format';
import type { SpeedtestConfig, SpeedtestDashboard, SpeedtestResult } from './types';
import { Empty, Icon, Modal } from './ui';

const mbps = (value: number | null) => value == null ? '—' : `${value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} Mbps`;
const bytes = (value: number) => {
  if (value < 1024 ** 2) return `${(value / 1024).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} MiB`;
  return `${(value / 1024 ** 3).toLocaleString('pt-BR', { maximumFractionDigits: 2 })} GiB`;
};

function SpeedChart({ results }: { results: SpeedtestResult[] }) {
  const plot = useMemo(() => {
    const samples = results.filter(item => item.success && item.download_mbps != null && item.upload_mbps != null);
    const width = 700, height = 155, left = 43, top = 12;
    const max = Math.max(10, Math.ceil(Math.max(...samples.flatMap(item => [item.download_mbps!, item.upload_mbps!]), 0) * 1.15));
    const start = new Date(results[0]?.completed_at ?? 0).getTime();
    const end = new Date(results.at(-1)?.completed_at ?? 0).getTime();
    const point = (item: SpeedtestResult, value: number) => ({
      x: left + (new Date(item.completed_at).getTime() - start) / Math.max(1, end - start) * (width - left - 15),
      y: top + height - value / max * height,
    });
    const path = (key: 'download_mbps' | 'upload_mbps') => samples.map((item, index) => {
      const p = point(item, item[key]!);
      return `${index ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    }).join(' ');
    return { width, height, left, top, max, start, end, samples, download: path('download_mbps'), upload: path('upload_mbps') };
  }, [results]);
  if (!results.length) return <Empty title="Nenhum teste concluído">Use “Testar agora” ou aguarde a primeira execução agendada.</Empty>;
  const time = (value: number) => new Date(value).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
  return <div className="speed-chart"><svg viewBox="0 0 700 203" role="img" aria-label={`Histórico de ${results.length} testes Ookla`}>
    {[0, .5, 1].map(value => <g key={value}><line x1="43" x2="685" y1={12 + value * 155} y2={12 + value * 155} className="chart-grid"/><text x="34" y={16 + value * 155} textAnchor="end">{Math.round(plot.max * (1 - value))}</text></g>)}
    <text x="43" y="9">Mbps</text>
    {plot.download && <path d={plot.download} className="speed-download"/>}
    {plot.upload && <path d={plot.upload} className="speed-upload"/>}
    {plot.samples.map(item => { const d = item.download_mbps!, u = item.upload_mbps!; const dx = plot.left + (new Date(item.completed_at).getTime() - plot.start) / Math.max(1, plot.end - plot.start) * (plot.width - plot.left - 15); return <g key={item.id}><circle cx={dx} cy={plot.top + plot.height - d / plot.max * plot.height} r="3" className="speed-dot-download"><title>{dateTime(item.completed_at)}: download {mbps(d)}</title></circle><circle cx={dx} cy={plot.top + plot.height - u / plot.max * plot.height} r="3" className="speed-dot-upload"><title>{dateTime(item.completed_at)}: upload {mbps(u)}</title></circle></g>; })}
    <text x="43" y="199">{time(plot.start)}</text><text x="685" y="199" textAnchor="end">{time(plot.end)}</text>
  </svg><div className="chart-legend"><span><i className="speed-legend-download"/>Download</span><span><i className="speed-legend-upload"/>Upload</span></div></div>;
}

function SpeedtestSettings({ config, onClose, onSaved }: { config: SpeedtestConfig; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ enabled: config.enabled, interval_hours: String(config.interval_minutes / 60), download: config.download_min_mbps?.toString() ?? '', upload: config.upload_min_mbps?.toString() ?? '', failure: String(config.failure_threshold), recovery: String(config.recovery_threshold), retention: String(config.retention_days) });
  const [saving, setSaving] = useState(false), [error, setError] = useState('');
  async function save(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError('');
    try {
      await api('/speedtests/config', { method: 'PATCH', body: JSON.stringify({ enabled: form.enabled, interval_minutes: Math.round(Number(form.interval_hours) * 60), download_min_mbps: form.download ? Number(form.download) : null, upload_min_mbps: form.upload ? Number(form.upload) : null, failure_threshold: Number(form.failure), recovery_threshold: Number(form.recovery), retention_days: Number(form.retention) }) });
      onSaved(); onClose();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Não foi possível salvar.'); } finally { setSaving(false); }
  }
  return <Modal title="Configurar teste Ookla" onClose={onClose}><p className="form-context">Os limites são opcionais. Use os valores do seu plano para evitar alertas sem referência.</p><form onSubmit={save}>
    <label className="check-label"><input type="checkbox" checked={form.enabled} onChange={event => setForm(current => ({ ...current, enabled: event.target.checked }))}/>Executar automaticamente</label>
    <div className="form-row"><label>Intervalo (horas)<input type="number" min="1" max="168" step="1" required value={form.interval_hours} onChange={event => setForm(current => ({ ...current, interval_hours: event.target.value }))}/></label><label>Retenção (dias)<input type="number" min="1" max="365" required value={form.retention} onChange={event => setForm(current => ({ ...current, retention: event.target.value }))}/></label></div>
    <div className="form-row"><label>Download mínimo (Mbps)<input type="number" min="0.1" max="100000" step="0.1" value={form.download} onChange={event => setForm(current => ({ ...current, download: event.target.value }))} placeholder="Sem limite"/></label><label>Upload mínimo (Mbps)<input type="number" min="0.1" max="100000" step="0.1" value={form.upload} onChange={event => setForm(current => ({ ...current, upload: event.target.value }))} placeholder="Sem limite"/></label></div>
    <div className="form-row"><label>Medições ruins para abrir incidente<input type="number" min="1" max="10" required value={form.failure} onChange={event => setForm(current => ({ ...current, failure: event.target.value }))}/></label><label>Medições boas para recuperar<input type="number" min="1" max="10" required value={form.recovery} onChange={event => setForm(current => ({ ...current, recovery: event.target.value }))}/></label></div>
    {error && <p className="form-error" role="alert">{error}</p>}
    <footer className="modal-actions"><span className="spacer"/><button className="button secondary" type="button" onClick={onClose}>Cancelar</button><button className="button primary" disabled={saving}>{saving ? 'Salvando…' : 'Salvar'}</button></footer>
  </form></Modal>;
}

export default function SpeedTest() {
  const [days, setDays] = useState<1 | 7 | 30>(7), [data, setData] = useState<SpeedtestDashboard | null>(null);
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [settings, setSettings] = useState(false);
  const load = useCallback(async () => {
    try { setData(await api<SpeedtestDashboard>(`/speedtests?days=${days}`)); setError(''); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Teste de velocidade indisponível.'); }
    finally { setLoading(false); }
  }, [days]);
  useEffect(() => { setLoading(true); void load(); }, [load]);
  useEffect(() => { if (!data?.active_job) return; const timer = window.setInterval(() => void load(), 4000); return () => clearInterval(timer); }, [data?.active_job, load]);
  async function run() {
    setNotice('');
    try { const response = await api<{ already_pending: boolean }>('/speedtests/run', { method: 'POST', body: '{}' }); setNotice(response.already_pending ? 'Já existe um teste aguardando ou em execução.' : 'Teste enfileirado. O resultado aparecerá aqui ao terminar.'); await load(); }
    catch (cause) { setNotice(cause instanceof Error ? cause.message : 'Não foi possível solicitar o teste.'); }
  }
  const last = data?.last_result;
  return <section className="panel speedtest-panel"><header className="panel-heading"><div><h2>Velocidade da internet <span className="count">Ookla</span></h2><small>Raspberry → servidor selecionado pela Ookla</small></div><div className="speed-actions"><button className="button secondary small" onClick={() => setSettings(true)} disabled={!data}><Icon name="settings" size={16}/>Configurar</button><button className="button primary small" onClick={() => void run()} disabled={!data || Boolean(data.active_job)}><Icon name={data?.active_job ? 'refresh' : 'quality'} size={16}/>{data?.active_job?.status === 'running' ? 'Testando…' : data?.active_job ? 'Na fila…' : 'Testar agora'}</button></div></header>
    {notice && <div className="speed-notice" role="status">{notice}</div>}
    {error ? <p className="inline-error" role="alert">{error}</p> : loading && !data ? <div className="chart-loading">Carregando testes…</div> : data && <>
      <div className="speed-summary"><div><span>Download</span><strong>{mbps(last?.download_mbps ?? null)}</strong></div><div><span>Upload</span><strong>{mbps(last?.upload_mbps ?? null)}</strong></div><div><span>Ping</span><strong>{milliseconds(last?.ping_ms ?? null)}</strong><small>Jitter {milliseconds(last?.jitter_ms ?? null)}</small></div><div><span>Perda</span><strong>{percentage(last?.packet_loss_pct ?? null)}</strong></div><div><span>Tráfego no mês</span><strong>{bytes(data.month_total_bytes)}</strong><small>estimativa dos testes</small></div></div>
      <div className="quality-toolbar"><div className="segmented" aria-label="Período dos testes">{([1, 7, 30] as const).map(value => <button key={value} className={days === value ? 'selected' : ''} onClick={() => setDays(value)}>{value === 1 ? '24 horas' : `${value} dias`}</button>)}</div><div className="speed-schedule"><span>{data.config.enabled ? `A cada ${data.config.interval_minutes / 60} h` : 'Agendamento pausado'}</span><small>{data.config.next_run_at ? `Próximo: ${dateTime(data.config.next_run_at)}` : 'Sem próxima execução'}</small></div></div>
      <SpeedChart results={data.history}/>
      {last && <div className={`speed-last ${last.success ? 'ok' : 'failed'}`}><strong>{last.success ? 'Último teste concluído' : 'Último teste falhou'}</strong><span>{dateTime(last.completed_at)} · {last.server_name || 'Servidor não informado'}{last.server_location ? `, ${last.server_location}` : ''} · duração {duration(last.duration_seconds)}</span>{last.error && <small>{last.error}</small>}</div>}
      <div className="speed-incidents"><h3>Incidentes de velocidade</h3>{data.incidents.length ? <div className="table-scroll"><table><thead><tr><th>Incidente</th><th>Início</th><th>Duração</th><th>Estado</th></tr></thead><tbody>{data.incidents.map(item => <tr key={item.id}><td><strong>{item.name}</strong>{item.threshold_mbps != null && <small>Limite: {mbps(item.threshold_mbps)}</small>}</td><td>{dateTime(item.started_at)}</td><td>{duration(item.duration_seconds)}</td><td><span className={`status ${item.ongoing ? 'offline' : 'online'}`}><i/>{item.ongoing ? 'Aberto' : 'Recuperado'}</span></td></tr>)}</tbody></table></div> : <p>Nenhum incidente registrado.</p>}</div>
      <div className="panel-footnote">O teste usa banda real, pode saturar a conexão por alguns segundos e causar travamentos temporários em vídeos, jogos ou chamadas. Os limites vazios não geram incidentes de velocidade; falhas do cliente Ookla têm incidente próprio.</div>
    </>}
    {settings && data && <SpeedtestSettings config={data.config} onClose={() => setSettings(false)} onSaved={() => void load()}/>}
  </section>;
}
