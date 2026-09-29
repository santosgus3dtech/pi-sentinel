import { useEffect, useState } from 'react';
import { api } from './api';
import { ago, dateTime, duration } from './format';
import type { RouterDashboard, RouterStatus } from './types';
import { Empty, Icon } from './ui';

const interfaceLabel: Record<string, string> = {
  wired: 'Ethernet', wifi_2_4: 'Wi‑Fi 2,4 GHz', wifi_5: 'Wi‑Fi 5 GHz', unknown: 'Não identificada',
};
const eventLabel: Record<string, string> = {
  authentication: 'Autenticação', associated: 'Conectou', reassociated: 'Reconectou',
  disconnected: 'Desconectou', deauthenticated: 'Perdeu autenticação',
};

function value(value: number | null | undefined, suffix = '') {
  return value == null ? '—' : `${value}${suffix}`;
}

function signal(rssi: number | null) {
  if (rssi == null) return 'Sem medição';
  if (rssi >= -55) return 'Excelente';
  if (rssi >= -67) return 'Boa';
  if (rssi >= -75) return 'Regular';
  return 'Fraca';
}

function traffic(status: RouterStatus, key: string, direction: 'rx_mbps' | 'tx_mbps') {
  const result = status.traffic_rates?.[key]?.[direction];
  return result == null ? 'Aguardando 2 amostras' : `${result.toFixed(result >= 10 ? 1 : 3)} Mbps`;
}

export default function Router() {
  const [data, setData] = useState<RouterDashboard | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    const load = () => api<RouterDashboard>('/router').then(result => {
      if (active) { setData(result); setError(''); }
    }).catch(cause => { if (active) setError(cause instanceof Error ? cause.message : 'Falha ao consultar o roteador.'); });
    void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 15000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  if (!data && !error) return <div className="loading"><Icon name="refresh" size={26}/>Carregando telemetria do roteador…</div>;
  if (!data) return <div className="error-banner"><strong>Telemetria indisponível</strong><span>{error}</span></div>;
  const router = data.router;
  const cpu = router.cpu_memory;
  const wan = router.traffic_rates?.internet;
  return <div className="router-page">
    {!router.available && <div className="error-banner"><strong>Roteador sem coleta recente</strong><span>{router.error || 'Aguardando resposta do ASUSWRT.'}</span></div>}
    <section className="router-metrics">
      <article><span>Roteador</span><strong>{router.model || 'ASUSWRT'}</strong><small>{router.firmware ? `Firmware ${router.firmware}` : 'Firmware não informado'}</small></article>
      <article><span>CPU</span><strong>{router.cpu_memory?.cpu_percent?.length ? `${Math.max(...router.cpu_memory.cpu_percent).toFixed(1)}%` : '—'}</strong><small>{router.cpu_memory?.cpu_percent?.map((item, index) => `N${index + 1} ${item}%`).join(' · ') || 'Sem leitura'}</small></article>
      <article><span>Memória</span><strong>{value(cpu?.memory_used_pct, '%')}</strong><small>{value(cpu?.memory_used_mb, ' MB')} de {value(cpu?.memory_total_mb, ' MB')}</small></article>
      <article><span>Clientes online</span><strong>{router.client_counts?.online ?? '—'}</strong><small>{router.client_counts ? `${router.client_counts.wired} cabo · ${router.client_counts.wifi_2_4} em 2,4 · ${router.client_counts.wifi_5} em 5 GHz` : 'Sem leitura'}</small></article>
    </section>

    <div className="router-grid">
      <section className="panel"><header className="panel-heading"><h2>Rádios Wi‑Fi</h2><span className="count">{router.bands?.length || 0} bandas</span></header>
        {router.bands?.length ? <div className="router-band-list">{router.bands.map(band => <article key={`${band.band}-${band.ssid}`}><div><strong>{band.band}</strong><span>{band.ssid}</span></div><dl><div><dt>Canal</dt><dd>{value(band.channel)} · {value(band.channel_width_mhz, ' MHz')}</dd></div><div><dt>Uso do canal</dt><dd>{value(band.utilization_pct, '%')}</dd></div><div><dt>Ruído</dt><dd>{value(band.noise_dbm, ' dBm')}</dd></div><div><dt>Interferência</dt><dd>{band.interference === 'Acceptable' ? 'Aceitável' : band.interference || '—'}</dd></div></dl></article>)}</div> : <Empty title="Sem dados de rádio">O ASUSWRT ainda não retornou o log sem fio.</Empty>}
      </section>
      <section className="panel"><header className="panel-heading"><h2>Tráfego em tempo real</h2></header><div className="router-traffic">
        <article><span>WAN recebendo</span><strong>{traffic(router, 'internet', 'rx_mbps')}</strong></article><article><span>WAN enviando</span><strong>{traffic(router, 'internet', 'tx_mbps')}</strong></article>
        <article><span>Wi‑Fi 2,4 GHz</span><strong>↓ {traffic(router, 'wireless0', 'rx_mbps')}</strong><small>↑ {traffic(router, 'wireless0', 'tx_mbps')}</small></article>
        <article><span>Wi‑Fi 5 GHz</span><strong>↓ {traffic(router, 'wireless1', 'rx_mbps')}</strong><small>↑ {traffic(router, 'wireless1', 'tx_mbps')}</small></article>
      </div><div className="panel-footnote">Taxas calculadas entre duas leituras dos contadores do roteador. Não há inspeção do conteúdo trafegado.{wan ? '' : ' A primeira taxa aparece após o próximo ciclo.'}</div></section>
    </div>

    <section className="panel"><header className="panel-heading"><h2>Instabilidade Wi‑Fi nas últimas 24 horas</h2><span className="count">{data.wifi_alerts.length} alertas</span></header>
      {data.wifi_alerts.length ? <div className="router-alert-list">{data.wifi_alerts.map(alert => <article key={alert.mac}><div><strong>{alert.display_name}</strong><small className="mono">{alert.mac}</small></div><div><strong>{alert.event_count} eventos de desconexão</strong><small>Último {ago(alert.last_event_at)}</small></div><div><strong>{alert.min_rssi_dbm == null ? 'Sinal não informado' : `${alert.min_rssi_dbm} a ${alert.max_rssi_dbm} dBm`}</strong><small>Faixa de sinal registrada</small></div></article>)}</div> : <Empty title="Nenhuma repetição detectada">Não houve três ou mais eventos de desconexão do mesmo aparelho neste período.</Empty>}
      <div className="panel-footnote">A coleta considera somente eventos de associação do ASUSWRT e não armazena o log bruto.</div>
    </section>

    <section className="panel"><header className="panel-heading"><h2>Eventos Wi‑Fi recentes <span className="count">{data.wifi_events.length}</span></h2></header>
      {data.wifi_events.length ? <div className="table-scroll"><table><thead><tr><th>Quando</th><th>Dispositivo</th><th>Evento</th><th>Motivo / sinal</th></tr></thead><tbody>{data.wifi_events.map((event, index) => <tr key={`${event.timestamp}-${event.mac}-${index}`}><td>{dateTime(event.timestamp)}<small>{ago(event.timestamp)}</small></td><td><strong>{event.display_name}</strong><small className="mono">{event.mac}</small></td><td>{eventLabel[event.event] || event.event}</td><td>{event.reason || 'Sem motivo informado'}<small>{event.rssi_dbm == null ? 'RSSI não informado' : `${event.rssi_dbm} dBm · ${signal(event.rssi_dbm)}`}</small></td></tr>)}</tbody></table></div> : <Empty title="Sem eventos coletados">O histórico aparecerá após a leitura periódica do log do roteador.</Empty>}
    </section>

    <section className="panel"><header className="panel-heading"><h2>Clientes vistos pelo roteador <span className="count">{data.clients.length}</span></h2><span className="router-updated">Atualizado {ago(router.observed_at || null)}</span></header>
      {data.clients.length ? <div className="table-scroll"><table><thead><tr><th>Dispositivo</th><th>Conexão</th><th>Sinal</th><th>PHY</th><th>Tx / Rx</th><th>Conectado</th><th>DHCP</th></tr></thead><tbody>{data.clients.map(client => <tr key={client.device_id}><td><strong>{client.display_name || client.ip || 'Sem nome'}</strong><small className="mono">{client.ip} · {client.mac}</small></td><td>{interfaceLabel[client.interface]}<small>{client.router_online ? (client.ssid || (client.interface === 'wired' ? 'LAN' : 'SSID não informado')) : 'Histórico · offline no roteador'}</small></td><td><strong>{client.rssi_dbm == null ? '—' : `${client.rssi_dbm} dBm`}</strong><small>{signal(client.rssi_dbm)}</small></td><td>{client.phy_mode?.toUpperCase() || '—'}<small>{client.spatial_streams ? `${client.spatial_streams} fluxo(s) · ${client.channel_width_mhz} MHz` : '—'}</small></td><td>{value(client.tx_rate_mbps, ' Mbps')}<small>{value(client.rx_rate_mbps, ' Mbps')}</small></td><td>{client.connected_seconds == null ? '—' : duration(client.connected_seconds)}</td><td>{client.ip_method || '—'}<small>{client.dhcp_expires_seconds == null ? '—' : `renova em ${duration(client.dhcp_expires_seconds)}`}</small></td></tr>)}</tbody></table></div> : <Empty title="Nenhum cliente correlacionado">A lista aparecerá após a primeira coleta autenticada.</Empty>}
      <div className="panel-footnote">Última comunicação: {dateTime(router.observed_at || null)} · dados da interface local do ASUSWRT.</div>
    </section>
  </div>;
}
