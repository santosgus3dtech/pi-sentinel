import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api';
import { ago, duration } from './format';
import type {
  Printer, PrinterDetailsResponse, PrinterFile, PrinterFileInspection,
  PrinterHistoryEntry,
} from './types';
import { Empty, Icon, Modal } from './ui';

const stateLabels: Record<Printer['state'], string> = {
  IDLE: 'Ociosa', PRINTING: 'Imprimindo', PAUSED: 'Pausada', COMPLETED: 'Concluída',
  ERROR: 'Erro', OFFLINE: 'Offline', UNKNOWN: 'Desconhecido',
};

function temperature(value: { current: number | null; target: number | null }) {
  if (value.current == null) return '—';
  const current = `${value.current.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} °C`;
  return value.target == null ? current : `${current} / ${value.target.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} °C`;
}

function bytes(value: number | null) {
  if (value == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let amount = value; let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toLocaleString('pt-BR', { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
}

function StateBadge({ printer }: { printer: Printer }) {
  const tone = !printer.online ? 'offline' : printer.state === 'ERROR' ? 'offline' : printer.state === 'UNKNOWN' ? 'unknown' : 'online';
  return <span className={`status ${tone}`}><i/>{stateLabels[printer.state]}</span>;
}

function HealthComponents({ printer }: { printer: Printer }) {
  const labels: Record<string, string> = { network: 'Rede', web: 'Interface web', api: 'Protocolo/API', camera: 'Câmera' };
  return <div className="printer-health-list">{Object.entries(printer.health.components).map(([name, component]) => <div key={name}>
    <span><i className={component.status}/>{labels[name] || name}</span><strong>{component.status === 'healthy' ? 'OK' : component.status === 'disabled' ? 'Desativado' : 'Indisponível'}</strong>
    {component.latency_ms != null && <small>{component.latency_ms.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} ms</small>}
    {component.message && <small title={component.message}>{component.message}</small>}
  </div>)}</div>;
}

function Progress({ value }: { value: number | null }) {
  if (value == null) return <span className="printer-muted">Sem impressão ativa</span>;
  return <div className="printer-progress"><div><span>Progresso</span><strong>{value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%</strong></div><progress max="100" value={value}>{value}%</progress></div>;
}

function PrinterCard({ printer, onOpen, visitor = false }: { printer: Printer; onOpen: () => void; visitor?: boolean }) {
  return <article className="panel printer-card">
    <header><span className="printer-icon"><Icon name="printer3d" size={25}/></span><div><h2>{printer.name}</h2><p>{printer.manufacturer} · {printer.model}</p></div><StateBadge printer={printer}/></header>
    {printer.adapter_type === 'mock' && <span className="demo-badge">Demonstração</span>}
    <Progress value={printer.progress}/>
    <dl className="printer-readings"><div><dt>Bico</dt><dd>{temperature(printer.temperatures.nozzle)}</dd></div><div><dt>Mesa</dt><dd>{temperature(printer.temperatures.bed)}</dd></div></dl>
    <div className="printer-job"><span>Arquivo atual</span><strong title={printer.current_file || undefined}>{printer.current_file || '—'}</strong><small>{printer.remaining_seconds == null ? 'Tempo restante indisponível' : `${duration(printer.remaining_seconds)} restantes`}</small></div>
    <footer><div>{!visitor && <span className="mono">{printer.ip || 'Sem device associado'}{printer.latency_ms == null ? '' : ` · ${printer.latency_ms.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} ms`}</span>}<small>{printer.last_seen ? `Última comunicação ${ago(printer.last_seen)}` : 'Sem comunicação registrada'}</small></div>{!visitor && <button className="button secondary small" onClick={onOpen}>Ver detalhes <Icon name="arrow" size={15}/></button>}</footer>
  </article>;
}

function valueText(value: unknown) {
  if (value == null || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Sim' : 'Não';
  if (typeof value === 'number') return value.toLocaleString('pt-BR', { maximumFractionDigits: 3 });
  return String(value);
}

function ParameterGrid({ values }: { values: Record<string, unknown> }) {
  return <dl className="parameter-grid">{Object.entries(values).map(([key, value]) => {
    const complex = typeof value === 'object' && value !== null;
    return <div className={complex ? 'complex' : ''} key={key}><dt>{key}</dt><dd>{complex ? <pre>{JSON.stringify(value, null, 2)}</pre> : valueText(value)}</dd></div>;
  })}</dl>;
}

function DetailsOverview({ printer, details }: { printer: Printer; details: Record<string, unknown> | null }) {
  const job = (details?.job || {}) as Record<string, unknown>;
  const performance = (details?.performance || {}) as Record<string, unknown>;
  const fans = (details?.fans || {}) as Record<string, unknown>;
  return <div className="printer-detail-grid">
    <section className="panel detail-section"><h3>Impressão atual</h3><Progress value={printer.progress}/><dl><div><dt>Arquivo</dt><dd>{printer.current_file || 'Nenhum'}</dd></div><div><dt>Tempo restante</dt><dd>{printer.remaining_seconds == null ? '—' : duration(printer.remaining_seconds)}</dd></div><div><dt>Estado</dt><dd>{stateLabels[printer.state]}</dd></div>{job.current_layer != null && <div><dt>Camada</dt><dd>{valueText(job.current_layer)}{job.total_layers != null ? ` / ${valueText(job.total_layers)}` : ''}</dd></div>}{job.elapsed_seconds != null && <div><dt>Tempo decorrido</dt><dd>{duration(Number(job.elapsed_seconds))}</dd></div>}</dl></section>
    <section className="panel detail-section"><h3>Temperaturas</h3><dl><div><dt>Bico</dt><dd>{temperature(printer.temperatures.nozzle)}</dd></div><div><dt>Mesa</dt><dd>{temperature(printer.temperatures.bed)}</dd></div></dl></section>
    <section className="panel detail-section"><h3>Desempenho</h3><ParameterGrid values={performance}/></section>
    <section className="panel detail-section"><h3>Ventoinhas</h3><ParameterGrid values={fans}/></section>
    <section className="panel detail-section"><h3>Comunicação</h3><dl><div><dt>Adapter</dt><dd>{printer.adapter_type}</dd></div><div><dt>Status</dt><dd>{printer.connection_status}</dd></div><div><dt>Último contato</dt><dd>{ago(printer.last_seen)}</dd></div></dl>{printer.error && <p className="printer-error">{printer.error}</p>}</section>
    <section className="panel detail-section"><h3>Saúde dos componentes</h3><HealthComponents printer={printer}/></section>
  </div>;
}

function CameraView({ printer }: { printer: Printer }) {
  const [failed, setFailed] = useState(false);
  if (!printer.capabilities.camera) return <section className="panel printer-placeholder"><Empty title="Câmera não suportada">O adapter não anunciou câmera para esta impressora.</Empty></section>;
  if (!printer.camera.available || !printer.camera.stream_url || failed) return <section className="panel printer-placeholder"><Empty title="Câmera indisponível">{failed ? 'O stream foi interrompido.' : printer.camera.reason || 'O endpoint de câmera não respondeu.'}</Empty></section>;
  return <section className="panel printer-camera"><header><div><h3>Câmera · {printer.name}</h3><p>Stream entregue pelo PiSentinel sem expor credenciais ou o endereço interno ao navegador.</p></div><span>{printer.camera.protocol || 'imagem HTTP'}</span></header><div><img src={printer.camera.stream_url} alt={`Câmera da impressora ${printer.name}`} onError={() => setFailed(true)}/></div></section>;
}

function NetworkView({ printer }: { printer: Printer }) {
  return <div className="printer-detail-grid">
    <section className="panel detail-section"><h3>Componentes</h3><HealthComponents printer={printer}/></section>
    <section className="panel detail-section"><h3>Device associado</h3><dl><div><dt>IP</dt><dd className="mono">{printer.ip || '—'}</dd></div><div><dt>MAC</dt><dd className="mono">{printer.mac || '—'}</dd></div><div><dt>Latência da telemetria</dt><dd>{printer.latency_ms == null ? '—' : `${printer.latency_ms.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} ms`}</dd></div><div><dt>Última comunicação</dt><dd>{ago(printer.last_seen)}</dd></div><div><dt>Monitoramento de rede</dt><dd>{printer.network_status || '—'}</dd></div></dl></section>
  </div>;
}

function PrintDialog({ printer, file, onClose, onStarted }: { printer: Printer; file: PrinterFile; onClose: () => void; onStarted: (message: string) => void }) {
  const [inspection, setInspection] = useState<PrinterFileInspection | null>(null);
  const [platePath, setPlatePath] = useState('');
  const [useAms, setUseAms] = useState(false);
  const [mapping, setMapping] = useState<number[]>([]);
  const [confirmation, setConfirmation] = useState('');
  const [timelapse, setTimelapse] = useState(false);
  const [bedLeveling, setBedLeveling] = useState(true);
  const [flowCalibration, setFlowCalibration] = useState(false);
  const [vibrationCalibration, setVibrationCalibration] = useState(true);
  const [error, setError] = useState('');
  const [sending, setSending] = useState(false);

  useEffect(() => {
    let mounted = true;
    api<PrinterFileInspection>(`/printers/${printer.id}/files/inspect?path=${encodeURIComponent(file.path)}`)
      .then(value => {
        if (!mounted) return;
        setInspection(value);
        const first = value.plates[0];
        setPlatePath(value.plates.length === 1 ? first.path : '');
        setMapping((first?.filament_ids || []).map((_id, index) => index));
      })
      .catch(cause => mounted && setError(cause instanceof Error ? cause.message : 'Não foi possível inspecionar o arquivo.'));
    return () => { mounted = false; };
  }, [file.path, printer.id]);

  const selectedPlate = inspection?.plates.find(item => item.path === platePath) || inspection?.plates[0];
  const filamentCount = Math.max(1, selectedPlate?.filament_ids?.length || 1);
  function selectPlate(path: string) {
    setPlatePath(path);
    const plate = inspection?.plates.find(item => item.path === path);
    setMapping((plate?.filament_ids || []).map((_id, index) => index));
  }
  async function submit() {
    setSending(true); setError('');
    try {
      const result = await api<{ message: string }>(`/printers/${printer.id}/print`, {
        method: 'POST',
        body: JSON.stringify({ path: file.path, confirmation, plate: platePath || null, timelapse, bed_leveling: bedLeveling, flow_calibration: flowCalibration, vibration_calibration: vibrationCalibration, use_ams: useAms, ams_mapping: useAms ? mapping : [] }),
      });
      onStarted(result.message);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'A impressão não foi iniciada.'); }
    finally { setSending(false); }
  }
  return <Modal title={`Imprimir · ${file.name}`} onClose={onClose}>
    <div className="print-dialog">
      {!inspection && !error && <div className="loading" role="status"><Icon name="refresh"/>Inspecionando o arquivo…</div>}
      {error && <div className="error-banner" role="alert"><strong>Não foi possível preparar</strong><span>{error}</span></div>}
      {inspection && <>
        <div className="print-file-summary"><div>{file.preview_available && <img src={`/api/printers/${printer.id}/files/preview?path=${encodeURIComponent(file.path)}${platePath ? `&plate=${encodeURIComponent(platePath)}` : ''}`} alt="Prévia do arquivo"/>}</div><dl><div><dt>Tamanho</dt><dd>{bytes(file.size)}</dd></div>{selectedPlate?.estimated_seconds != null && <div><dt>Estimativa</dt><dd>{duration(selectedPlate.estimated_seconds)}</dd></div>}{selectedPlate?.filament_weight_g != null && <div><dt>Filamento</dt><dd>{selectedPlate.filament_weight_g.toLocaleString('pt-BR')} g</dd></div>}{selectedPlate?.bed_type && <div><dt>Mesa</dt><dd>{selectedPlate.bed_type}</dd></div>}</dl></div>
        {inspection.plates.length > 1 && <label>Placa<select value={platePath} onChange={event => selectPlate(event.target.value)}><option value="">Selecione…</option>{inspection.plates.map(plate => <option key={plate.path} value={plate.path}>Placa {plate.index}</option>)}</select></label>}
        {printer.adapter_type === 'bambu_a1' && <fieldset><legend>Preparação da Bambu A1</legend><label className="check-line"><input type="checkbox" checked={bedLeveling} onChange={event => setBedLeveling(event.target.checked)}/>Nivelamento da mesa</label><label className="check-line"><input type="checkbox" checked={vibrationCalibration} onChange={event => setVibrationCalibration(event.target.checked)}/>Calibração de vibração</label><label className="check-line"><input type="checkbox" checked={flowCalibration} onChange={event => setFlowCalibration(event.target.checked)}/>Calibração de fluxo</label><label className="check-line"><input type="checkbox" checked={timelapse} onChange={event => setTimelapse(event.target.checked)}/>Timelapse</label><label className="check-line"><input type="checkbox" checked={useAms} onChange={event => setUseAms(event.target.checked)}/>Usar AMS Lite</label>{useAms && <div className="ams-mapping">{Array.from({ length: filamentCount }, (_value, index) => <label key={index}>Filamento {index + 1}<select value={mapping[index] ?? index} onChange={event => setMapping(current => { const next = [...current]; next[index] = Number(event.target.value); return next; })}>{[0, 1, 2, 3].map(slot => <option key={slot} value={slot}>Bandeja {slot + 1}</option>)}</select></label>)}</div>}</fieldset>}
        <label>Digite <strong>IMPRIMIR</strong> para confirmar<input value={confirmation} onChange={event => setConfirmation(event.target.value.toUpperCase())} autoComplete="off"/></label>
        <p className="printer-action-note">A impressora aquecerá e movimentará os eixos após aceitar este comando.</p>
        <div className="modal-actions"><button className="button secondary" onClick={onClose}>Cancelar</button><button className="button primary" disabled={sending || confirmation !== 'IMPRIMIR' || (inspection.plates.length > 1 && !platePath)} onClick={() => void submit()}>{sending ? 'Enviando…' : 'Iniciar impressão'}</button></div>
      </>}
    </div>
  </Modal>;
}

function FilesView({ printer, onStarted }: { printer: Printer; onStarted: (message: string) => void }) {
  const [files, setFiles] = useState<PrinterFile[] | null>(null);
  const [selected, setSelected] = useState<PrinterFile | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let mounted = true;
    api<{ files: PrinterFile[] }>(`/printers/${printer.id}/files`).then(value => mounted && setFiles(value.files)).catch(cause => mounted && setError(cause instanceof Error ? cause.message : 'Não foi possível consultar o cartão.'));
    return () => { mounted = false; };
  }, [printer.id]);
  if (!printer.capabilities.files) return <section className="panel printer-placeholder"><Empty title="Arquivos indisponíveis">O protocolo desta impressora não oferece acesso ao cartão.</Empty></section>;
  if (error) return <div className="error-banner" role="alert"><strong>Cartão indisponível</strong><span>{error}</span></div>;
  if (!files) return <div className="loading"><Icon name="refresh"/>Lendo o cartão…</div>;
  const ready = printer.online && (printer.state === 'IDLE' || printer.state === 'COMPLETED');
  return <section className="panel printer-files"><header><div><h3>Arquivos no cartão</h3><p>{files.length} arquivo(s) encontrados na interface local.</p></div></header>{files.length ? <div className="file-table-wrap"><table><thead><tr><th>Arquivo</th><th>Tamanho</th><th>Modificado</th><th>Dados do fatiamento</th><th/></tr></thead><tbody>{files.map(file => <tr key={file.path}><td><strong>{file.name}</strong><small className="mono">{file.path}</small></td><td>{bytes(file.size)}</td><td>{file.modified || (file.modified_at_epoch ? new Date(file.modified_at_epoch * 1000).toLocaleString('pt-BR') : '—')}</td><td>{Object.entries(file.metadata || {}).filter(([, value]) => value != null && value !== '').slice(0, 3).map(([key, value]) => <small key={key}>{key}: {valueText(value)}</small>)}</td><td><button className="button secondary small" disabled={!file.print_ready || !ready || !printer.capabilities.controls} title={!file.print_ready ? 'Este item não é um arquivo imprimível compatível.' : !ready ? 'A impressora precisa estar online e ociosa.' : !printer.capabilities.controls ? 'Os controles estão desativados no servidor.' : ''} onClick={() => setSelected(file)}>Preparar impressão</button></td></tr>)}</tbody></table></div> : <Empty title="Cartão vazio">Nenhum arquivo imprimível foi encontrado.</Empty>}{selected && <PrintDialog printer={printer} file={selected} onClose={() => setSelected(null)} onStarted={message => { setSelected(null); onStarted(message); }}/>}</section>;
}

function HistoryView({ printer }: { printer: Printer }) {
  const [history, setHistory] = useState<PrinterHistoryEntry[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!printer.capabilities.history) return;
    let mounted = true;
    api<{ history: PrinterHistoryEntry[] }>(`/printers/${printer.id}/history`).then(value => mounted && setHistory(value.history)).catch(cause => mounted && setError(cause instanceof Error ? cause.message : 'Não foi possível consultar o histórico.'));
    return () => { mounted = false; };
  }, [printer.capabilities.history, printer.id]);
  if (!printer.capabilities.history) return <section className="panel printer-placeholder"><Empty title="Histórico local indisponível">O protocolo local desta impressora não fornece histórico de trabalhos.</Empty></section>;
  if (error) return <div className="error-banner"><strong>Histórico indisponível</strong><span>{error}</span></div>;
  if (!history) return <div className="loading"><Icon name="refresh"/>Carregando histórico…</div>;
  return <section className="panel printer-history"><h3>Histórico da impressora</h3><div className="history-list">{history.map((item, index) => <article key={`${item.id}-${index}`}><i className={item.completed ? 'done' : 'stopped'}>{item.completed ? '✓' : '!'}</i><div><strong>{item.file_name || 'Arquivo desconhecido'}</strong><span>{item.started_at || 'Data indisponível'}</span></div><dl><div><dt>Duração</dt><dd>{item.duration_seconds == null ? '—' : duration(item.duration_seconds)}</dd></div><div><dt>Filamento</dt><dd>{item.filament_mm == null ? '—' : `${(item.filament_mm / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 2 })} m`}</dd></div></dl></article>)}</div></section>;
}

const tabs = ['Visão geral', 'Arquivos', 'Parâmetros', 'Histórico', 'Câmera', 'Rede'] as const;

function PrinterDetails({ printer, onBack }: { printer: Printer; onBack: () => void }) {
  const [tab, setTab] = useState<(typeof tabs)[number]>('Visão geral');
  const [details, setDetails] = useState<Record<string, unknown> | null>(null);
  const [detailsError, setDetailsError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let mounted = true;
    api<PrinterDetailsResponse>(`/printers/${printer.id}/details`).then(value => mounted && setDetails(value.details)).catch(cause => mounted && setDetailsError(cause instanceof Error ? cause.message : 'Parâmetros indisponíveis.'));
    return () => { mounted = false; };
  }, [printer.id]);
  const parameters = useMemo(() => (details?.parameters || {}) as Record<string, unknown>, [details]);
  return <section className="printer-details">
    <button className="text-button printer-back" onClick={onBack}>← Voltar para impressoras</button>
    <header className="panel printer-detail-heading"><span className="printer-icon"><Icon name="printer3d" size={28}/></span><div><h2>{printer.name}</h2><p>{printer.manufacturer} · {printer.model} · <span className="mono">{printer.ip || 'sem IP associado'}</span></p></div><StateBadge printer={printer}/></header>
    {notice && <div className="notice" role="status">{notice}</div>}
    <nav className="printer-tabs" aria-label="Detalhes da impressora">{tabs.map(item => <button key={item} className={tab === item ? 'selected' : ''} onClick={() => setTab(item)}>{item}</button>)}</nav>
    {tab === 'Visão geral' ? <DetailsOverview printer={printer} details={details}/>
      : tab === 'Arquivos' ? <FilesView printer={printer} onStarted={setNotice}/>
      : tab === 'Parâmetros' ? <section className="panel printer-parameters"><header><div><h3>Todos os parâmetros disponíveis</h3><p>{details?.source ? String(details.source) : 'Fonte específica do adapter'} · campos não confirmados não são inventados.</p></div></header>{detailsError ? <div className="error-banner"><strong>Parâmetros indisponíveis</strong><span>{detailsError}</span></div> : details ? <ParameterGrid values={parameters}/> : <div className="loading"><Icon name="refresh"/>Carregando parâmetros…</div>}</section>
      : tab === 'Histórico' ? <HistoryView printer={printer}/>
      : tab === 'Câmera' ? <CameraView printer={printer}/>
      : <NetworkView printer={printer}/>}
  </section>;
}

export default function Printers({ visitor = false }: { visitor?: boolean }) {
  const [printers, setPrinters] = useState<Printer[]>([]);
  const [selected, setSelected] = useState<Printer | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const pollDelay = useRef(20000);
  const inFlight = useRef(false);

  async function load(alive = () => true) {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const items = await api<Printer[]>('/printers');
      if (!alive()) return;
      setPrinters(items);
      setSelected(current => current ? items.find(item => item.id === current.id) ?? null : null);
      pollDelay.current = items.some(item => item.state === 'PRINTING' || item.state === 'PAUSED') ? 8000 : 20000;
      setError('');
    } catch (cause) {
      if (alive()) setError(cause instanceof Error ? cause.message : 'Não foi possível carregar as impressoras.');
    } finally {
      inFlight.current = false;
      if (alive()) setLoading(false);
    }
  }

  useEffect(() => {
    let mounted = true; let timer = 0;
    const tick = async () => { if (!document.hidden) await load(() => mounted); if (mounted) timer = window.setTimeout(tick, pollDelay.current); };
    void tick();
    return () => { mounted = false; clearTimeout(timer); };
  }, []);

  if (selected && !visitor) return <PrinterDetails printer={selected} onBack={() => setSelected(null)}/>;
  return <section className="printers-page">
    <header className="printers-toolbar"><div><strong>{printers.length}</strong><span>{printers.length === 1 ? 'impressora cadastrada' : 'impressoras cadastradas'}</span></div><button className="button secondary small" onClick={() => void load()} disabled={loading}><Icon name="refresh" size={16}/>{loading ? 'Atualizando…' : 'Atualizar'}</button></header>
    {error && <div className="error-banner" role="alert"><strong>Impressoras indisponíveis</strong><span>{error}</span></div>}
    {loading && !printers.length ? <div className="loading" role="status"><Icon name="refresh" size={28}/>Carregando impressoras…</div> : printers.length ? <div className="printer-grid">{printers.map(printer => <PrinterCard key={printer.id} printer={printer} visitor={visitor} onOpen={() => !visitor && setSelected(printer)}/>)}</div> : <section className="panel"><Empty title="Nenhuma impressora cadastrada">{visitor ? 'Nenhuma impressora está disponível para consulta.' : 'Cadastre uma impressora vinculada a um device existente.'}</Empty></section>}
  </section>;
}
