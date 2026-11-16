import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, ApiError, errorText } from './api';
import { canManage } from './contracts';
import type { Session } from './contracts';
import { UsersPanel, SourcesPanel } from './Admin';

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<'users' | 'sources'>('users');
  const sessionRequest = useRef(0);
  const sessionMutation = useRef(false);

  async function refresh() {
    if (sessionMutation.current) return;
    const request = ++sessionRequest.current;
    try {
      const current = await api.me();
      if (request === sessionRequest.current) setSession(current);
    } catch (failure) {
      if (request === sessionRequest.current) {
        setSession(null);
        if (!(failure instanceof ApiError && failure.status === 401)) setError(errorText(failure));
      }
    } finally {
      if (request === sessionRequest.current) setLoading(false);
    }
  }
  useEffect(() => {
    void refresh();
    const onFocus = () => { void refresh(); };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);
  useEffect(() => {
    if (!session) return;
    const delay = Date.parse(session.expires_at) - Date.now();
    const timeout = window.setTimeout(() => {
      sessionRequest.current++;
      setSession(null);
      setError('Your session expired. Sign in again.');
    }, Math.max(0, Math.min(delay, 2_147_483_647)));
    return () => window.clearTimeout(timeout);
  }, [session]);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const passwordField = form.elements.namedItem('password') as HTMLInputElement;
    passwordField.value = '';
    sessionRequest.current++;
    sessionMutation.current = true;
    setBusy(true);
    setError('');
    try {
      setSession(await api.login(String(data.get('username')), String(data.get('password'))));
      setTab('users');
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      sessionMutation.current = false;
      setBusy(false);
    }
  }
  async function logout() {
    if (!session) return;
    sessionRequest.current++;
    sessionMutation.current = true;
    setBusy(true);
    setError('');
    try {
      await api.logout(session.csrf_token);
      setSession(null);
    } catch (failure) {
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 401) setSession(null);
    } finally { sessionMutation.current = false; setBusy(false); }
  }
  function authFailure(failure: unknown) {
    if (failure instanceof ApiError && (failure.status === 401 || failure.status === 403)) {
      setError(errorText(failure));
      sessionRequest.current++;
      setSession(null);
      if (failure.status === 403) void refresh();
    }
  }
  const usersAllowed = session ? canManage(session, 'users.manage') : false;
  const sourcesAllowed = session ? canManage(session, 'sources.manage') : false;
  const activeTab = tab === 'users' && usersAllowed ? 'users' : sourcesAllowed ? 'sources' : 'users';

  return <div className="shell">
    <header className="header">
      <div><span className="brand">ARCHERS</span><p>Document knowledge · Administration</p></div>
      {session && <div className="identity"><span>{session.user.username} · {session.user.role}</span>
        <button type="button" onClick={() => void logout()} disabled={busy}>Sign out</button></div>}
    </header>
    <main id="main">
      {error && <p role="alert" className="error">{error}</p>}
      {loading ? <p role="status">Checking your session…</p> : !session ?
        <section className="card login" aria-labelledby="login-heading">
          <h1 id="login-heading">Sign in</h1><p>Use an account provisioned by your administrator.</p>
          <form onSubmit={event => void login(event)}>
            <fieldset disabled={busy}>
              <label>Username<input name="username" required maxLength={128} autoComplete="username" /></label>
              <label>Password<input name="password" type="password" required maxLength={1024} autoComplete="current-password" /></label>
              <button type="submit">{busy ? 'Signing in…' : 'Sign in'}</button>
            </fieldset>
          </form>
        </section> :
        <>
          <h1>{usersAllowed || sourcesAllowed ? 'Administration' : 'Welcome'}</h1>
          <p>Query and ingestion interfaces are not yet implemented. Configured folders can be scanned and indexed automatically when the watcher, worker, local embedding model and Qdrant are running. Manual uploads are available through the API. Answer generation is not connected yet.</p>
          <p className="muted">The server enforces access. UI visibility does not grant permissions.</p>
          {session.user.disabled && <p role="alert" className="error">This account is disabled. Sign out and contact your administrator.</p>}
          {(usersAllowed || sourcesAllowed) && <>
            <nav className="tabs" aria-label="Administration sections">
              {usersAllowed && <button aria-current={activeTab === 'users' ? 'page' : undefined} onClick={() => setTab('users')}>Users</button>}
              {sourcesAllowed && <button aria-current={activeTab === 'sources' ? 'page' : undefined} onClick={() => setTab('sources')}>Sources</button>}
            </nav>
            {activeTab === 'users' && usersAllowed ?
              <UsersPanel session={session} onAuthFailure={authFailure} onSessionRefresh={refresh} /> :
              sourcesAllowed && <SourcesPanel session={session} onAuthFailure={authFailure} />}
          </>}
        </>}
    </main>
    <footer>Local administration · Approved sources only · External export off by default</footer>
  </div>;
}
