import { useState } from 'react';
import { api } from './api';
import type { AuthSession } from './types';
import { Brand, Icon } from './ui';

export function LoginScreen({ onAuthenticated, notice = '' }: { onAuthenticated: (session: AuthSession) => void; notice?: string }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState<'login' | 'guest' | ''>('');
  const [error, setError] = useState('');

  async function login(event: React.FormEvent) {
    event.preventDefault(); setBusy('login'); setError('');
    try {
      onAuthenticated(await api<AuthSession>('/auth/login', {
        method: 'POST', body: JSON.stringify({ username: username.trim(), password, remember }),
      }));
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Não foi possível entrar.'); }
    finally { setBusy(''); }
  }

  async function guest() {
    setBusy('guest'); setError('');
    try { onAuthenticated(await api<AuthSession>('/auth/guest', { method: 'POST', body: '{}' })); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Não foi possível entrar como visitante.'); }
    finally { setBusy(''); }
  }

  return <main className="auth-page">
    <section className="auth-card">
      <Brand/>
      <div className="auth-heading"><span className="auth-symbol"><Icon name="shield" size={27}/></span><div><h1>Acesse o PiSentinel</h1><p>Entre para administrar sua rede ou continue com acesso de visitante.</p></div></div>
      {notice && <div className="notice" role="status">{notice}</div>}
      {error && <div className="error-banner" role="alert"><strong>Acesso não autorizado</strong><span>{error}</span></div>}
      <form onSubmit={login}>
        <label>Usuário<input value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" required autoFocus/></label>
        <label>Senha<input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required/></label>
        <label className="auth-remember"><input type="checkbox" checked={remember} onChange={event => setRemember(event.target.checked)}/><span><strong>Lembrar de mim</strong><small>Mantém o acesso neste navegador mesmo depois de fechá-lo.</small></span></label>
        <button className="button primary" disabled={!!busy}>{busy === 'login' ? 'Entrando…' : 'Entrar'}</button>
      </form>
      <div className="auth-divider"><span>ou</span></div>
      <button className="button secondary auth-guest" disabled={!!busy} onClick={() => void guest()}><Icon name="visitors"/>{busy === 'guest' ? 'Abrindo acesso…' : 'Entrar como visitante'}</button>
      <p className="auth-footnote">O visitante vê somente o resumo operacional e o estado das impressoras. Dados de rede, câmera, arquivos e controles ficam protegidos.</p>
    </section>
  </main>;
}

export function ChangePasswordScreen({ username, onChanged, onLogout }: { username: string; onChanged: (notice: string) => void; onLogout: () => void }) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError('');
    if (newPassword !== confirmation) { setError('A confirmação não corresponde à nova senha.'); return; }
    setSaving(true);
    try {
      await api('/auth/change-password', { method: 'POST', body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
      onChanged('Senha alterada. Entre novamente com a nova senha.');
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Não foi possível alterar a senha.'); }
    finally { setSaving(false); }
  }
  return <main className="auth-page"><section className="auth-card"><Brand/><div className="auth-heading"><span className="auth-symbol"><Icon name="key" size={27}/></span><div><h1>Defina uma nova senha</h1><p>O acesso de {username} exige uma troca de senha antes de continuar.</p></div></div>{error && <div className="error-banner" role="alert"><strong>Não foi possível salvar</strong><span>{error}</span></div>}<form onSubmit={submit}><label>Senha atual<input type="password" value={currentPassword} onChange={event => setCurrentPassword(event.target.value)} autoComplete="current-password" required autoFocus/></label><label>Nova senha<input type="password" value={newPassword} onChange={event => setNewPassword(event.target.value)} autoComplete="new-password" minLength={12} required/><small>Use no mínimo 12 caracteres, com maiúscula, minúscula, número e símbolo.</small></label><label>Confirme a nova senha<input type="password" value={confirmation} onChange={event => setConfirmation(event.target.value)} autoComplete="new-password" minLength={12} required/></label><button className="button primary" disabled={saving}>{saving ? 'Salvando…' : 'Alterar senha'}</button><button type="button" className="text-button auth-cancel" onClick={onLogout}>Sair</button></form></section></main>;
}
