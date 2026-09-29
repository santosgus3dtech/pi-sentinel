import { useEffect, useMemo, useState } from 'react';
import { api, useDashboard } from './api';
import { ChangePasswordScreen, LoginScreen } from './Auth';
import Devices from './Devices';
import Incidents from './Incidents';
import Printers from './Printers';
import Quality from './Quality';
import Router from './Router';
import { ago } from './format';
import type { AuthSession } from './types';
import { Brand, Empty, Icon } from './ui';

type View = 'overview' | 'devices' | 'router' | 'printers' | 'quality' | 'incidents';

const navigation: { id: View; label: string; icon: string; adminOnly?: boolean }[] = [
  { id: 'overview', label: 'Visão geral', icon: 'home' },
  { id: 'devices', label: 'Dispositivos', icon: 'devices', adminOnly: true },
  { id: 'router', label: 'Roteador', icon: 'router', adminOnly: true },
  { id: 'printers', label: 'Impressoras 3D', icon: 'printer3d' },
  { id: 'quality', label: 'Qualidade', icon: 'quality', adminOnly: true },
  { id: 'incidents', label: 'Incidentes', icon: 'incident', adminOnly: true },
];

const signedOut: AuthSession = { auth_enabled: true, authenticated: false, role: null, username: null, must_change_password: false };

export default function App() {
  const [session, setSession] = useState<AuthSession | null>(null);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    api<AuthSession>('/auth/session').then(setSession).catch(() => setSession(signedOut));
    const unauthorized = () => setSession(signedOut);
    addEventListener('pisentinel:unauthorized', unauthorized);
    return () => removeEventListener('pisentinel:unauthorized', unauthorized);
  }, []);
  async function logout() {
    try { await api('/auth/logout', { method: 'POST', body: '{}' }); } catch { /* A próxima consulta confirma a sessão encerrada. */ }
    setSession(signedOut);
  }
  if (!session) return <main className="auth-page"><div className="loading"><Icon name="refresh" size={28}/>Verificando acesso…</div></main>;
  if (!session.authenticated || !session.role) return <LoginScreen notice={notice} onAuthenticated={value => { setNotice(''); setSession(value); }}/>;
  if (session.must_change_password) return <ChangePasswordScreen username={session.username || 'administrador'} onLogout={() => void logout()} onChanged={message => { setNotice(message); setSession(signedOut); }}/>;
  return <Dashboard session={session} onLogout={() => void logout()}/>;
}

function Dashboard({ session, onLogout }: { session: AuthSession; onLogout: () => void }) {
  const role = session.role === 'guest' ? 'guest' : 'admin';
  const { data, error, loading, refresh } = useDashboard(role);
  const allowedNavigation = useMemo(() => navigation.filter(item => role === 'admin' || !item.adminOnly), [role]);
  const [view, setView] = useState<View>('overview');
  const [discovering, setDiscovering] = useState(false);
  const [notice, setNotice] = useState('');

  useEffect(() => {
    const onHash = () => {
      const hash = location.hash.slice(1) as View;
      const next = allowedNavigation.some(item => item.id === hash) ? hash : 'overview';
      setView(next);
      if (next !== hash) history.replaceState(null, '', '#overview');
    };
    onHash();
    addEventListener('hashchange', onHash);
    return () => removeEventListener('hashchange', onHash);
  }, [allowedNavigation]);

  function navigate(next: View) {
    setView(next);
    history.replaceState(null, '', `#${next}`);
    scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function discover() {
    setDiscovering(true); setNotice('');
    try {
      await api('/discovery', { method: 'POST', body: '{}' });
      setNotice('Descoberta solicitada. O coletor fará a varredura no próximo ciclo.');
      window.setTimeout(() => void refresh(), 2500);
    } catch (cause) { setNotice(cause instanceof Error ? cause.message : 'Não foi possível solicitar a descoberta.'); }
    finally { setDiscovering(false); }
  }

  const controlCenter = `${location.protocol}//${location.hostname}:8080`;
  const summary = data?.summary;
  const pageLabel = view === 'overview' ? (role === 'guest' ? 'Visão geral da operação' : 'Sua rede, em foco.') : navigation.find(item => item.id === view)?.label;
  const description = role === 'guest'
    ? view === 'printers' ? 'Estado atual das impressoras, sem acesso a câmera, arquivos ou controles.' : 'Resumo de disponibilidade autorizado para visitantes.'
    : view === 'printers' ? 'Telemetria completa, arquivos locais e impressão com confirmação.'
      : summary ? `${summary.network.cidr || 'Rede em detecção'} · ${summary.network.ssids.join(' / ') || 'SSID não identificado'}` : 'Disponibilidade, conexão e histórico em um só lugar.';

  return <div className="app-shell">
    <aside className="sidebar">
      <Brand/>
      <nav aria-label="Seções do PiSentinel">{allowedNavigation.map(item => <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => navigate(item.id)}><Icon name={item.icon}/><span>{item.label}</span></button>)}</nav>
      {role === 'admin' && <a className="control-link" href={controlCenter}><Icon name="settings"/>Control Center <Icon name="external" size={15}/></a>}
      <p className="sidebar-note">{role === 'guest' ? 'Acesso de visitante somente para consulta.' : <>Inventário e telemetria local.<br/>Credenciais ficam somente no servidor.</>}</p>
    </aside>
    <main>
      <header className="topbar">
        <div><span>{role === 'guest' ? 'Acesso' : 'Rede'}</span><b>›</b><strong>{role === 'guest' ? 'Visitante' : summary?.network.label || 'PiSentinel'}</strong></div>
        <div className="topbar-actions">
          <div className={`collector-state ${summary?.collector.stale ? 'stale' : ''}`}><i/><span><strong>{summary?.collector.stale ? 'Coleta desatualizada' : 'Coletor ativo'}</strong><small>{summary?.collector.last_seen ? `Última coleta ${ago(summary.collector.last_seen)}` : 'Ainda sem coleta confirmada'}</small></span></div>
          <div className="session-chip"><span><strong>{session.username}</strong><small>{role === 'guest' ? 'Visitante' : 'Administrador'}</small></span><button type="button" onClick={onLogout} title="Sair" aria-label="Sair"><Icon name="logout" size={17}/></button></div>
        </div>
      </header>
      <div className="content">
        <section className="page-title"><div><h1>{pageLabel}</h1><p>{description}</p></div>{role === 'admin' && view !== 'printers' && <button className="button primary" disabled={discovering || !data} onClick={() => void discover()}><Icon name={discovering ? 'refresh' : 'plus'}/>{discovering ? 'Solicitando…' : 'Descobrir dispositivos'}</button>}</section>
        {notice && <div className="notice" role="status">{notice}</div>}
        {error && <div className="error-banner" role="alert"><strong>PiSentinel indisponível</strong><span>{error}</span><button className="button secondary small" onClick={() => void refresh()}>Tentar novamente</button></div>}
        {loading && !data ? <div className="loading" role="status"><Icon name="refresh" size={28}/>Carregando dados da rede…</div> : data ? <>
          {summary?.collector.stale && <div className="stale-banner"><Icon name="incident"/><div><strong>Os estados atuais são desconhecidos</strong><span>O coletor não concluiu uma observação recente. Isso não confirma que os equipamentos estejam desligados.</span></div></div>}
          {view === 'overview' && <>
            <section className="metrics" aria-label="Resumo da rede">
              <Metric icon="devices" label="Dispositivos" value={summary!.counts.devices}/>
              <Metric dot="online" label="Respondendo" value={summary!.counts.online}/>
              <Metric dot="offline" label="Sem resposta" value={summary!.counts.offline}/>
              <Metric icon="incident" label="Incidentes abertos" value={summary!.counts.open_incidents}/>
            </section>
            {role === 'admin' ? <><Devices devices={data.devices} compact onViewAll={() => navigate('devices')} refresh={refresh}/><div className="overview-grid"><Quality targets={data.targets} compact refresh={refresh}/><Diagnosis items={summary!.diagnosis}/></div><Incidents incidents={data.incidents} compact onViewAll={() => navigate('incidents')}/></> : <div className="guest-overview"><section className="panel guest-access-panel"><span className="auth-symbol"><Icon name="shield" size={25}/></span><div><h2>Consulta protegida</h2><p>Você pode acompanhar a disponibilidade geral e o estado das impressoras. Entre como administrador para acessar dispositivos, endereços, qualidade detalhada, incidentes, câmera, arquivos e controles.</p></div></section><Diagnosis items={summary!.diagnosis}/></div>}
          </>}
          {role === 'admin' && view === 'devices' && <Devices devices={data.devices} refresh={refresh}/>}
          {role === 'admin' && view === 'router' && <Router/>}
          {view === 'printers' && <Printers visitor={role === 'guest'}/>}
          {role === 'admin' && view === 'quality' && <Quality targets={data.targets} refresh={refresh}/>}
          {role === 'admin' && view === 'incidents' && <Incidents incidents={data.incidents}/>}
        </> : !error && <Empty title="Aguardando o PiSentinel">O painel aparecerá quando a API responder.</Empty>}
      </div>
    </main>
  </div>;
}

function Metric({ icon, dot, label, value }: { icon?: string; dot?: string; label: string; value: number }) {
  return <div className="metric"><div className="metric-label">{icon ? <Icon name={icon}/> : <i className={`metric-dot ${dot}`}/>}<span>{label}</span></div><strong>{value}</strong></div>;
}

function Diagnosis({ items }: { items: { level: string; title: string; detail: string }[] }) {
  return <section className="panel diagnosis"><header className="panel-heading"><h2>Diagnóstico por etapas</h2></header>{items.length ? <div className="diagnosis-list">{items.map((item, index) => <article key={`${item.title}-${index}`} className={item.level}><i>{item.level === 'ok' ? '✓' : '!'}</i><div><strong>{item.title}</strong><p>{item.detail}</p></div></article>)}</div> : <Empty title="Aguardando diagnóstico">Os testes locais e externos alimentarão hipóteses de diagnóstico.</Empty>}<div className="panel-footnote">As mensagens são hipóteses baseadas nos testes, não diagnósticos definitivos.</div></section>;
}
