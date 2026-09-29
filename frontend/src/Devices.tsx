import { useEffect, useMemo, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api } from './api';
import { ago, dateTime, duration, milliseconds, percentage } from './format';
import { groupIcons, groups } from './types';
import type { Device, DeviceDetails, Group } from './types';
import { Empty, Icon, Modal, StatusBadge } from './ui';

const identificationLabels: Record<string, string> = {
  cadastro_impressora: 'Cadastro confirmado de impressora', mac_conhecido: 'MAC conhecido',
  hostname_oui: 'Nome de rede + fabricante do MAC', hostname: 'Nome de rede',
  oui: 'Fabricante do MAC', nao_identificado: 'Sem evidência suficiente',
};
const macTypeLabels: Record<Device['mac_address_type'], string> = {
  global: 'MAC global (fabricante identificável)', private: 'MAC privado/aleatório',
  multicast: 'Endereço multicast', unknown: 'Tipo desconhecido',
};
const routerInterfaceLabels: Record<string, string> = {
  wired: 'Ethernet', wifi_2_4: 'Wi‑Fi 2,4 GHz', wifi_5: 'Wi‑Fi 5 GHz', unknown: 'Não identificada',
};

function DeviceEditor({ device, onClose, onSaved }: { device: Device; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(device.name ?? '');
  const [notes, setNotes] = useState(device.notes ?? '');
  const [group, setGroup] = useState<Group>(device.group || 'sem_grupo');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError('');
    try { await api(`/devices/${device.id}`, { method: 'PATCH', body: JSON.stringify({ name: name.trim(), notes: notes.trim(), group }) }); onSaved(); onClose(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Não foi possível salvar.'); }
    finally { setSaving(false); }
  }
  return <Modal title="Editar dispositivo" onClose={onClose}><p className="form-context mono">{device.ip} · {device.mac || 'MAC não disponível'}</p><form onSubmit={save}>
    <label>Nome personalizado<input autoFocus value={name} maxLength={100} onChange={e => setName(e.target.value)} placeholder={device.display_name || device.hostname || 'Ex.: Computador do escritório'}/></label>
    <label>Grupo<select value={group} onChange={e => setGroup(e.target.value as Group)}>{Object.entries(groups).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <label>Observações<textarea rows={4} value={notes} maxLength={2000} onChange={e => setNotes(e.target.value)} placeholder="Localização, função ou outras informações úteis."/></label>
    {error && <p className="form-error" role="alert">{error}</p>}
    <footer className="modal-actions"><button className="button secondary" type="button" onClick={onClose} disabled={saving}>Cancelar</button><button className="button primary" disabled={saving}>{saving ? 'Salvando…' : 'Salvar alterações'}</button></footer>
  </form></Modal>;
}

function Info({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return <div className="device-info"><span>{label}</span><strong className={mono ? 'mono' : ''}>{children}</strong></div>;
}

function DeviceDetailsModal({ device, onClose }: { device: Device; onClose: () => void }) {
  const [details, setDetails] = useState<DeviceDetails | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    api<DeviceDetails>(`/devices/${device.id}`)
      .then(value => { if (active) setDetails(value); })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : 'Não foi possível carregar os detalhes.'); });
    return () => { active = false; };
  }, [device.id]);
  return <Modal title={device.display_name || device.ip || 'Detalhes do dispositivo'} onClose={onClose} className="device-details-modal">
    {!details && !error && <div className="loading device-details-loading"><Icon name="refresh" size={24}/>Coletando informações…</div>}
    {error && <div className="inline-error">{error}</div>}
    {details && <div className="device-details">
      <section className="device-detail-hero"><span className="device-detail-icon"><Icon name={groupIcons[details.group]} size={30}/></span><div><StatusBadge status={details.status}/><p>{groups[details.group]} · {details.vendor || 'Fabricante não identificado'}</p></div></section>

      <section><h3>Identidade e endereço</h3><div className="device-info-grid">
        <Info label="IPv4" mono>{details.ip || 'Sem IP atual'}</Info><Info label="MAC" mono>{details.mac || 'Não disponível'}</Info>
        <Info label="Tipo de MAC">{macTypeLabels[details.mac_address_type]}</Info><Info label="Hostname" mono>{details.hostname || 'Não informado'}</Info>
        <Info label="Nome automático">{details.auto_name || 'Não identificado'}</Info><Info label="Fonte da identificação">{identificationLabels[details.identification_source || ''] || details.identification_source || 'Não identificada'}</Info>
        <Info label="Grupo automático">{groups[details.auto_group]}</Info><Info label="Grupo atual">{groups[details.group]}{details.group_customized ? ' · definido manualmente' : ' · automático'}</Info>
        <Info label="Primeiro contato">{dateTime(details.created_at)}</Info><Info label="Identificação atualizada">{dateTime(details.identified_at)}</Info>
      </div>{details.notes && <p className="device-detail-notes"><strong>Observações</strong>{details.notes}</p>}</section>

      <section><h3>Disponibilidade</h3><div className="availability-grid">{(['1h', '24h', '7d'] as const).map(period => { const item = details.availability[period]; return <article key={period}><strong>{period}</strong><span>{item.samples ? `${item.successes}/${item.samples} respostas` : 'Sem amostras'}</span><b>{item.loss_pct == null ? '—' : `${percentage(item.loss_pct)} sem resposta`}</b><small>RTT médio {milliseconds(item.avg_rtt_ms)} · mín. {milliseconds(item.min_rtt_ms)} · máx. {milliseconds(item.max_rtt_ms)}</small></article>; })}</div><div className="device-info-grid compact-info">
        <Info label="Última resposta">{dateTime(details.last_seen)} · {ago(details.last_seen)}</Info><Info label="Última verificação">{dateTime(details.last_checked)}</Info>
        <Info label="Sequência de respostas">{details.success_streak}</Info><Info label="Sequência de falhas">{details.failure_streak}</Info>
      </div></section>

      {details.router && <section><h3>Conexão observada pelo roteador</h3><div className="device-info-grid">
        <Info label="Interface">{routerInterfaceLabels[details.router.interface] || details.router.interface}</Info><Info label="SSID">{details.router.ssid || (details.router.interface === 'wired' ? 'LAN' : 'Não informado')}</Info>
        <Info label="Sinal">{details.router.rssi_dbm == null ? 'Não se aplica' : `${details.router.rssi_dbm} dBm`}</Info><Info label="Padrão / largura">{details.router.phy_mode ? `${details.router.phy_mode.toUpperCase()} · ${details.router.channel_width_mhz || '—'} MHz` : 'Não informado'}</Info>
        <Info label="Taxa PHY Tx">{details.router.tx_rate_mbps == null ? '—' : `${details.router.tx_rate_mbps} Mbps`}</Info><Info label="Taxa PHY Rx">{details.router.rx_rate_mbps == null ? '—' : `${details.router.rx_rate_mbps} Mbps`}</Info>
        <Info label="Fluxos espaciais">{details.router.spatial_streams ?? '—'}</Info><Info label="Tempo conectado">{details.router.connected_seconds == null ? '—' : duration(details.router.connected_seconds)}</Info>
        <Info label="Endereço">{details.router.ip_method || 'Não informado'}</Info><Info label="Renovação DHCP">{details.router.dhcp_expires_seconds == null ? '—' : duration(details.router.dhcp_expires_seconds)}</Info>
        <Info label="Acesso à internet">{details.router.internet_allowed == null ? 'Não informado' : details.router.internet_allowed ? 'Permitido' : 'Bloqueado no roteador'}</Info><Info label="Leitura do roteador">{dateTime(details.router.observed_at)}</Info>
      </div></section>}

      <section><h3>Serviços locais detectados</h3><p className="section-help">Conexões curtas em {details.checked_tcp_ports} portas TCP comuns. Não há tentativa de login, exploração ou varredura completa.</p>
        {details.services.length ? <div className="service-list">{details.services.map(service => <article key={`${service.transport}-${service.port}`}><div><strong>{service.service}</strong><span className="mono">{service.transport.toUpperCase()} {service.port}</span></div><div><b>{milliseconds(service.latency_ms)}</b>{service.http_status && <span>HTTP {service.http_status}</span>}{service.server && <small>{service.server}</small>}{service.content_type && <small>{service.content_type}</small>}</div></article>)}</div> : <p className="device-empty-line">Nenhuma das portas comuns verificadas respondeu.</p>}
        <p className="section-timestamp">Última verificação: {dateTime(details.services_checked_at)}{details.services_error ? ` · ${details.services_error}` : ''}</p>
      </section>

      <section><h3>Incidentes recentes</h3>{details.recent_incidents.length ? <div className="device-incident-list">{details.recent_incidents.map(item => <article key={item.id}><span>{dateTime(item.started_at)}</span><strong>{item.ongoing ? 'Em andamento' : `Recuperado em ${duration(item.duration_seconds)}`}</strong></article>)}</div> : <p className="device-empty-line">Nenhum incidente registrado para este dispositivo.</p>}</section>
    </div>}
  </Modal>;
}

export default function Devices({ devices, compact = false, onViewAll, refresh }: { devices: Device[]; compact?: boolean; onViewAll?: () => void; refresh: () => void }) {
  const [query, setQuery] = useState('');
  const [group, setGroup] = useState('');
  const [status, setStatus] = useState('');
  const [editing, setEditing] = useState<Device | null>(null);
  const [inspecting, setInspecting] = useState<Device | null>(null);
  const filtered = useMemo(() => {
    const q = query.trim().toLocaleLowerCase('pt-BR');
    const order = { online: 0, unknown: 1, offline: 2 } as const;
    return [...devices]
      .filter(d => (!q || [d.name, d.display_name, d.auto_name, d.hostname, d.vendor, d.ip, d.mac, d.notes].some(s => s?.toLocaleLowerCase('pt-BR').includes(q))) && (!group || d.group === group) && (!status || d.status === status))
      .sort((a, b) => order[a.status] - order[b.status] || (a.display_name || a.ip || '').localeCompare(b.display_name || b.ip || '', 'pt-BR'));
  }, [devices, query, group, status]);
  const visible = compact ? filtered.slice(0, 6) : filtered;
  return <section className="panel devices-panel" aria-labelledby="device-heading"><header className="panel-heading"><h2 id="device-heading">Dispositivos na rede <span className="count">{devices.length}</span></h2>{compact && <button className="text-button" onClick={onViewAll}>Ver todos <Icon name="arrow" size={17}/></button>}</header>
    {!compact && <div className="filters"><label className="search"><Icon name="search"/><input value={query} onChange={e => setQuery(e.target.value)} aria-label="Buscar dispositivos" placeholder="Buscar nome, IP, MAC ou observação"/></label><select aria-label="Filtrar por grupo" value={group} onChange={e => setGroup(e.target.value)}><option value="">Todos os grupos</option>{Object.entries(groups).map(([v,l]) => <option key={v} value={v}>{l}</option>)}</select><select aria-label="Filtrar por estado" value={status} onChange={e => setStatus(e.target.value)}><option value="">Todos os estados</option><option value="online">Respondendo</option><option value="offline">Sem resposta</option><option value="unknown">Aguardando</option></select></div>}
    {visible.length ? <div className="table-scroll"><table><thead><tr><th>Dispositivo</th><th>IP / MAC</th><th>Grupo</th><th>Estado</th><th>Última resposta</th><th><span className="sr-only">Ações</span></th></tr></thead><tbody>{visible.map(d => <tr key={d.id}>
      <td><div className="device-name"><span className="device-icon" title={groups[d.group]}><Icon name={groupIcons[d.group]}/></span><div><strong>{d.display_name || 'Dispositivo sem nome'}</strong>{d.name && d.hostname && d.name !== d.hostname && <small>{d.hostname}</small>}{!compact && d.vendor && <small>{d.vendor} · identificação automática</small>}{!compact && d.notes && <small className="device-notes" title={d.notes}>{d.notes}</small>}</div></div></td>
      <td className="mono">{d.ip}<small>{d.mac || 'MAC não disponível'}</small></td><td><span className="group-label"><Icon name={groupIcons[d.group]} size={16}/>{groups[d.group] || 'Sem grupo'}</span></td><td><StatusBadge status={d.status}/></td><td title={dateTime(d.last_seen)}>{ago(d.last_seen)}{!compact && <small>{milliseconds(d.rtt_ms)}</small>}</td><td><div className="row-actions"><button className="text-button" onClick={() => setInspecting(d)}>Detalhes</button>{!compact && <button className="text-button" onClick={() => setEditing(d)} aria-label={`Editar ${d.display_name || d.ip}`}>Editar</button>}</div></td>
    </tr>)}</tbody></table></div> : <Empty title={devices.length ? 'Nenhum dispositivo encontrado' : 'O inventário começa na primeira descoberta'}>{devices.length ? 'Tente outro nome, grupo ou estado.' : 'Use “Descobrir dispositivos” para solicitar uma busca na rede configurada.'}</Empty>}
    {!compact && <div className="panel-footnote">{filtered.length} de {devices.length} dispositivos · detalhes incluem identidade, disponibilidade, incidentes e serviços TCP comuns.</div>}
    {editing && <DeviceEditor device={editing} onClose={() => setEditing(null)} onSaved={refresh}/>}
    {inspecting && <DeviceDetailsModal device={inspecting} onClose={() => setInspecting(null)}/>}
  </section>;
}
