import { useState } from 'react';
import type { Incident } from './types';
import { dateTime, duration } from './format';
import { Empty, Icon } from './ui';
export default function Incidents({ incidents, compact = false, onViewAll }: { incidents: Incident[]; compact?: boolean; onViewAll?: () => void }) {
  const [filter, setFilter] = useState('all');
  const filtered = incidents.filter(i => filter === 'all' || (filter === 'open' ? i.ongoing : !i.ongoing));
  const visible = compact ? filtered.slice(0, 5) : filtered;
  return <section className="panel"><header className="panel-heading"><h2>Incidentes recentes</h2>{compact ? <button className="text-button" onClick={onViewAll}>Ver todos <Icon name="arrow" size={17}/></button> : <select aria-label="Filtrar incidentes" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">Todos os incidentes</option><option value="open">Em andamento</option><option value="closed">Recuperados</option></select>}</header>
    {visible.length ? <div className="table-scroll"><table><thead><tr><th>Dispositivo / alvo</th><th>Início</th><th>Recuperação</th><th>Duração estimada</th><th>Estado</th></tr></thead><tbody>{visible.map(i => <tr key={i.id}><td><strong>{i.name}</strong><small>{i.entity_type === 'device' ? 'Dispositivo' : 'Alvo de qualidade'} · ausência de resposta</small></td><td>{dateTime(i.started_at)}</td><td>{i.recovered_at ? dateTime(i.recovered_at) : '—'}</td><td>{duration(i.duration_seconds ?? (i.ongoing ? Math.max(0, (Date.now() - new Date(i.started_at).getTime()) / 1000) : null))}</td><td><span className={`status ${i.ongoing ? 'offline' : 'online'}`}><i/>{i.ongoing ? 'Em andamento' : 'Recuperado'}</span></td></tr>)}</tbody></table></div> : <Empty title="Nenhum incidente neste filtro">As falhas e recuperações detectadas pelo coletor aparecerão aqui.</Empty>}
    {!compact && <div className="panel-footnote">Até 100 eventos recentes. Os horários seguem o fuso do navegador; a duração é estimada a partir dos testes periódicos.</div>}
  </section>;
}
