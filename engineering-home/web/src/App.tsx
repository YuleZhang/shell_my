import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Blocks, FileText, GitPullRequest, House, KeyRound, LayoutDashboard, LogOut, Menu, ShieldCheck, X } from 'lucide-react';
import type { LoginResponse, SessionResponse } from '../../shared/contracts';
import { errorMessage, request, setSessionToken, watchUnauthorized } from './client';
import { Loading, Notice } from './presentation';
import { Overview } from './pages/Overview';
import { Mrs } from './pages/Mrs';
import { Accounts } from './pages/Accounts';
import { Services } from './pages/Services';
import { Reports } from './pages/Reports';

const navigation = [
  { id: 'overview', label: '总览', icon: LayoutDashboard }, { id: 'mrs', label: '我的 MR', icon: GitPullRequest },
  { id: 'accounts', label: 'API 账户', icon: KeyRound }, { id: 'reports', label: '周报', icon: FileText },
  { id: 'services', label: '服务目录', icon: Blocks },
];
function currentPage() { const id = window.location.hash.slice(1); return navigation.some(item => item.id === id) ? id : 'overview'; }
export default function App() {
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(currentPage);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const sessionRequest = useRef(0);
  async function loadSession() {
    const sequence = ++sessionRequest.current;
    setChecking(true); setError(null);
    try {
      const value = await request<SessionResponse>('/api/session');
      if (sequence === sessionRequest.current) { setSessionToken(value.csrfToken); setSession(value); }
    } catch (e) {
      if (sequence === sessionRequest.current) { setSession(null); setSessionToken(null); if (!(e instanceof Error && 'status' in e && e.status === 401)) setError(errorMessage(e)); }
    } finally { if (sequence === sessionRequest.current) setChecking(false); }
  }
  useEffect(() => {
    const cleanup = watchUnauthorized(() => { sessionRequest.current++; setChecking(false); setSession(null); setSessionToken(null); setError('会话已失效，请重新登录。'); });
    void loadSession();
    return () => { sessionRequest.current++; cleanup(); };
  }, []);
  useEffect(() => { const change = () => { setPage(currentPage()); setMobileOpen(false); }; window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change); }, []);
  useEffect(() => { document.title = `${navigation.find(item => item.id === page)?.label ?? '总览'} · 工程快捷台`; }, [page]);
  async function logout() {
    setLoggingOut(true);
    try { await request('/api/auth/logout', { method: 'POST' }); setSessionToken(null); setSession(null); }
    catch (e) { setError(errorMessage(e)); } finally { setLoggingOut(false); }
  }
  if (checking) return <main className="auth-screen"><Loading label="正在验证安全会话…" /></main>;
  if (!session) return <Login error={error} onLogin={loadSession} />;
  return <div className="app-shell"><a href="#main-content" className="skip-link" onClick={e => { e.preventDefault(); document.getElementById('main-content')?.focus(); }}>跳到内容</a>
    <aside className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`} aria-label="主导航">
      <a href="#overview" className="brand"><span className="brand-icon"><House size={22} /></span><span>工程快捷台<small>ENGINEERING HOME</small></span></a>
      <button className="icon-button mobile-close" aria-label="关闭导航" onClick={() => setMobileOpen(false)}><X size={20} /></button>
      <div className="nav-caption">工作空间</div><nav>{navigation.map(({ id, label, icon: Icon }) => <a key={id} href={`#${id}`} className={`nav-item ${page === id ? 'active' : ''}`} aria-current={page === id ? 'page' : undefined} onClick={() => setMobileOpen(false)}><Icon size={19} aria-hidden="true" />{label}</a>)}</nav>
      <div className="sidebar-footer"><ShieldCheck size={18} aria-hidden="true" /><strong>个人 · 同源安全会话</strong><p>凭据留在服务器。<br />只在你确认后采集或生成。</p><button className="button button-ghost" onClick={() => void logout()} disabled={loggingOut}><LogOut size={15} aria-hidden="true" />退出登录</button></div>
    </aside>
    {mobileOpen && <button className="nav-backdrop" aria-label="关闭导航遮罩" onClick={() => setMobileOpen(false)} />}
    <div className="workspace"><header className="topbar"><div className="topbar-location"><button className="icon-button mobile-toggle" aria-label="打开导航" aria-expanded={mobileOpen} onClick={() => setMobileOpen(true)}><Menu size={21} /></button><span>个人工作空间</span><span className="topbar-slash">/</span><strong>{navigation.find(item => item.id === page)?.label}</strong></div><span className="connection"><span />{window.location.host}</span></header>
      <main id="main-content" tabIndex={-1} className="main-content">{error && <Notice tone="error">{error}</Notice>}
        {page === 'overview' && <Overview capabilities={session.capabilities} />}
        {page === 'mrs' && <Mrs enabled={session.capabilities.mrs} />}
        {page === 'accounts' && <Accounts capabilities={session.capabilities} />}
        {page === 'services' && <Services enabled={session.capabilities.services} />}
        {page === 'reports' && <Reports capabilities={session.capabilities} />}
        <footer className="page-footer">个人工程快捷入口 · 不替代 herdr · 无自动外部刷新或模型调用</footer>
      </main>
    </div>
  </div>;
}
function Login({ error: sessionError, onLogin }: { error: string | null; onLogin: () => Promise<void> }) {
  const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault(); if (busy) return; setBusy(true); setError(null);
    try { const result = await request<LoginResponse>('/api/auth/login', { method: 'POST', body: { password } }); setPassword(''); setSessionToken(result.csrfToken); await onLogin(); }
    catch (e) { setPassword(''); setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <main className="auth-screen"><section className="login-card"><span className="brand-icon"><House size={28} aria-hidden="true" /></span><div className="eyebrow">YOUR ENGINEERING WORKSPACE</div><h1>回到你的工程桌面</h1><p>MR、账户、周报与服务，一个安静的入口。</p>{(error || sessionError) && <Notice tone="error">{error ?? sessionError}</Notice>}<form onSubmit={e => void submit(e)}><label className="field">访问密码<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" maxLength={512} required disabled={busy} /></label><button className="button button-primary" disabled={busy}>{busy ? '正在登录…' : '安全登录'}<ShieldCheck size={17} aria-hidden="true" /></button></form><small>使用服务器配置的访问密码，不是 API Key。<br />密码与会话令牌不存入浏览器持久存储。</small></section></main>;
}
