import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api, errorText } from './api';
import { canManage, formats, roles } from './contracts';
import type { CreateSource, Format, Page, Role, Session, Source, UpdateSource, UpdateUser, User } from './contracts';

interface PanelProps {
  session: Session;
  onAuthFailure: (failure: unknown) => void;
}

function usePage<T extends { id: string }>(load: (cursor?: string) => Promise<Page<T>>, onAuthFailure: PanelProps['onAuthFailure']) {
  const [page, setPage] = useState<Page<T>>({ items: [], next_cursor: null });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function refresh(cursor?: string) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const next = await load(cursor);
      setPage(previous => ({
        items: cursor ? [...previous.items, ...next.items.filter(item => !previous.items.some(old => old.id === item.id))] : next.items,
        next_cursor: next.next_cursor,
      }));
    } catch (failure) {
      setError(errorText(failure));
      onAuthFailure(failure);
    } finally { setBusy(false); }
  }
  useEffect(() => { void refresh(); }, []);
  async function save(action: () => Promise<T>, after?: () => void | Promise<void>) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const item = await action();
      setPage(previous => ({ ...previous, items: [item, ...previous.items.filter(old => old.id !== item.id)] }));
      setNotice('Saved successfully.');
      await after?.();
    } catch (failure) {
      setError(errorText(failure));
      onAuthFailure(failure);
    } finally { setBusy(false); }
  }
  return { page, busy, error, notice, refresh, save };
}
function PanelStatus({ error, notice, busy }: { error: string; notice: string; busy: boolean }) {
  return <>{error && <p role="alert" className="error">{error}</p>}
    <p role="status">{busy ? 'Working…' : notice}</p></>;
}
function Pagination({ cursor, busy, refresh }: { cursor: string | null; busy: boolean; refresh: (cursor?: string) => Promise<void> }) {
  return <div className="actions"><button disabled={busy} onClick={() => void refresh()}>Refresh list</button>
    {cursor && <button disabled={busy} onClick={() => void refresh(cursor)}>Load more</button>}</div>;
}
function RoleSelect({ value = 'reader' }: { value?: string }) {
  const known = roles.includes(value as Role);
  return <label>Role<select name="role" defaultValue={known ? value : ''} required>
    {!known && <option value="" disabled>Unknown role (choose a known role)</option>}
    {roles.map(role => <option key={role}>{role}</option>)}
  </select></label>;
}
export function UsersPanel({ session, onAuthFailure, onSessionRefresh }: PanelProps & { onSessionRefresh: () => Promise<void> }) {
  const state = usePage(api.users, onAuthFailure);
  const [editing, setEditing] = useState<User | null>(null);
  if (!canManage(session, 'users.manage')) return null;
  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    (form.elements.namedItem('password') as HTMLInputElement).value = '';
    void state.save(() => api.createUser({
      username: String(data.get('username')), password: String(data.get('password')), role: data.get('role') as Role,
    }, session.csrf_token), () => form.reset());
  }
  function update(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editing) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    const body: UpdateUser = { role: data.get('role') as Role, disabled: data.has('disabled') };
    if (!roles.includes(body.role as Role)) return;
    const password = String(data.get('password'));
    if (password) body.password = password;
    (form.elements.namedItem('password') as HTMLInputElement).value = '';
    void state.save(() => api.updateUser(editing, body, session.csrf_token), async () => {
      setEditing(null);
      await onSessionRefresh();
    });
  }
  return <section aria-labelledby="users-heading">
    <h2 id="users-heading">Users</h2>
    <p>Role changes, disable, and password resets revoke sessions. The server protects the last enabled administrator.</p>
    <PanelStatus {...state} />
    <Pagination cursor={state.page.next_cursor} busy={state.busy} refresh={async cursor => { setEditing(null); await state.refresh(cursor); }} />
    <div className="grid">
      <div className="card">
        <h3>Accounts</h3>
        {!state.busy && state.page.items.length === 0 && !state.error && <p>No users found.</p>}
        <ul className="resource-list">{state.page.items.map(user => <li key={user.id}>
          <div><strong>{user.username}</strong><span>{user.role} · {user.disabled ? 'Disabled' : 'Enabled'} · revision {user.revision}</span></div>
          <button disabled={state.busy} aria-label={`Edit user ${user.username}`} onClick={() => setEditing(user)}>Edit</button>
        </li>)}</ul>
      </div>
      <div className="card">
        {editing ? <form key={`${editing.id}-${editing.revision}`} onSubmit={update}>
          <h3>Edit {editing.username}</h3>
          <fieldset disabled={state.busy}>
            <RoleSelect value={editing.role} />
            <label className="check"><input name="disabled" type="checkbox" defaultChecked={editing.disabled} />Disable account</label>
            <label>New password (optional)<input name="password" type="password" minLength={15} maxLength={1024} autoComplete="new-password" /></label>
            <p className="muted">Leave empty to preserve the password. New passwords require 15–1024 characters.</p>
            <div className="actions"><button type="submit">Save user</button><button type="button" onClick={() => setEditing(null)}>Cancel</button></div>
          </fieldset>
        </form> : <form onSubmit={create}>
          <h3>Provision user</h3>
          <fieldset disabled={state.busy}>
            <label>Username<input name="username" required maxLength={128} autoComplete="off" /></label>
            <RoleSelect />
            <label>Initial password<input name="password" type="password" required minLength={15} maxLength={1024} autoComplete="new-password" /></label>
            <p className="muted">15–1024 characters. Share securely outside this application.</p>
            <button type="submit">Create user</button>
          </fieldset>
        </form>}
      </div>
    </div>
  </section>;
}

export const defaultSource: CreateSource = {
  connector: { kind: 'filesystem', mount_alias: '', sentinel_id: '' },
  formats: ['pdf'],
  enabled: true,
  export_policy: { allow_external_generation: false, allow_external_embeddings: false },
  scan_policy: { poll_seconds: 30, stable_seconds: 60, deletion_grace_seconds: 300, completion_mode: 'atomic_rename' },
};

export function sourcePolicy(data: FormData): UpdateSource {
  return {
    enabled: data.has('enabled'),
    formats: data.getAll('formats') as Format[],
    scan_policy: {
      poll_seconds: Number(data.get('poll_seconds')),
      stable_seconds: Number(data.get('stable_seconds')),
      deletion_grace_seconds: Number(data.get('deletion_grace_seconds')),
      completion_mode: data.get('completion_mode') as CreateSource['scan_policy']['completion_mode'],
    },
  };
}
function PolicyFields({ source }: { source: CreateSource }) {
  return <>
    <label className="check"><input type="checkbox" name="enabled" defaultChecked={source.enabled} />Enabled (pausing is not deletion)</label>
    <fieldset className="format-options"><legend>Allowed formats (select at least one)</legend>
      {formats.map(format => <label className="check" key={format}><input name="formats" value={format} type="checkbox" defaultChecked={source.formats.includes(format)} />{format}</label>)}
    </fieldset>
    <h4>Scan policy</h4>
    <p className="muted">Provisional timings; server safety gates cannot be bypassed.</p>
    <label>Poll interval (seconds)<input name="poll_seconds" type="number" min={1} step={1} required defaultValue={source.scan_policy.poll_seconds} /></label>
    <label>Stable interval (seconds)<input name="stable_seconds" type="number" min={1} step={1} required defaultValue={source.scan_policy.stable_seconds} /></label>
    <label>Deletion grace (seconds)<input name="deletion_grace_seconds" type="number" min={1} step={1} required defaultValue={source.scan_policy.deletion_grace_seconds} /></label>
    <label>Completion protocol<select name="completion_mode" defaultValue={source.scan_policy.completion_mode}>
      <option value="atomic_rename">Atomic rename</option>
      <option value="ready_manifest">Ready manifest</option>
      <option value="heuristic_double_hash">Heuristic double hash</option>
      {source.connector.kind !== 'filesystem' && <option value="remote_revision">Certified remote revision</option>}
    </select></label>
  </>;
}
function sourceLabel(source: Source) {
  return source.connector.kind === 'filesystem' ? source.connector.mount_alias : `${source.connector.kind} · ${source.id}`;
}
export function SourcesPanel({ session, onAuthFailure }: PanelProps) {
  const state = usePage(api.sources, onAuthFailure);
  const [editing, setEditing] = useState<Source | null>(null);
  const [formError, setFormError] = useState('');
  if (!canManage(session, 'sources.manage')) return null;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setFormError('');
    const policy = sourcePolicy(data);
    if (!policy.formats?.length) {
      setFormError('Select at least one allowed format.');
      return;
    }
    const exportPolicy = {
      allow_external_generation: data.has('allow_external_generation'),
      allow_external_embeddings: data.has('allow_external_embeddings'),
    };
    if (editing) {
      if (session.permissions.includes('export.manage')) policy.export_policy = exportPolicy;
      void state.save(() => api.updateSource(editing, policy, session.csrf_token), () => setEditing(null));
    } else {
      const source: CreateSource = {
        ...defaultSource,
        ...policy,
        connector: { kind: 'filesystem', mount_alias: String(data.get('mount_alias')), sentinel_id: String(data.get('sentinel_id')) },
        // Creation always starts with external export denied, even for an admin.
        export_policy: { allow_external_generation: false, allow_external_embeddings: false },
      };
      void state.save(() => api.createSource(source, session.csrf_token), () => form.reset());
    }
  }
  return <section aria-labelledby="sources-heading">
    <h2 id="sources-heading">Sources</h2>
    <p>Import only a company-approved tenant-wide corpus. Remote ACLs are not mirrored. Credentials and mounts are provisioned by operators, not this UI.</p>
    <PanelStatus {...state} />
    <Pagination cursor={state.page.next_cursor} busy={state.busy} refresh={async cursor => { setEditing(null); setFormError(''); await state.refresh(cursor); }} />
    <div className="grid">
      <div className="card"><h3>Registered sources</h3>
        {!state.busy && state.page.items.length === 0 && !state.error && <p>No sources found.</p>}
        <ul className="resource-list">{state.page.items.map(source => <li key={source.id}>
          <div><strong>{sourceLabel(source)}</strong>
            <span>{source.connector.kind} · {source.enabled ? 'Enabled' : 'Paused'} · revision {source.revision}</span>
            <span>Formats: {source.formats.join(', ')}</span>
            <span>External generation: {source.export_policy.allow_external_generation ? 'allowed' : 'denied'} · embeddings: {source.export_policy.allow_external_embeddings ? 'allowed' : 'denied'}</span>
          </div><button disabled={state.busy} aria-label={`Edit source ${sourceLabel(source)}`} onClick={() => { setEditing(source); setFormError(''); }}>Edit</button>
        </li>)}</ul>
      </div>
      <form className="card" key={editing ? `${editing.id}-${editing.revision}` : 'new'} onSubmit={submit}>
        <h3>{editing ? `Edit ${sourceLabel(editing)}` : 'Register filesystem source'}</h3>
        {formError && <p role="alert" className="error">{formError}</p>}
        <fieldset disabled={state.busy}>
          {!editing ? <>
            <label>Approved mount alias<input name="mount_alias" required maxLength={128} pattern="[A-Za-z0-9_-]+" autoComplete="off" /></label>
            <label>Approved sentinel UUID<input name="sentinel_id" required pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" autoComplete="off" /></label>
            <p className="muted">Use operator-approved identifiers, never a path or mount credential. SMB/NFS use the filesystem connector after host mounting.</p>
          </> : <p className="muted">Connector scope is immutable. Only policy and enabled status can change.</p>}
          <PolicyFields source={editing ?? defaultSource} />
          <h4>External export</h4>
          {editing && session.permissions.includes('export.manage') ? <>
            <p>Explicit company approval is required. Source flags never override deployment deny. Previously exported data cannot be recalled.</p>
            <label className="check"><input type="checkbox" name="allow_external_generation" defaultChecked={editing.export_policy.allow_external_generation} />Allow external generation</label>
            <label className="check"><input type="checkbox" name="allow_external_embeddings" defaultChecked={editing.export_policy.allow_external_embeddings} />Allow external embeddings</label>
          </> : <p>Generation and embeddings are denied for new sources. Export policy editing requires server-granted export management permission.</p>}
          <div className="actions"><button type="submit">{editing ? 'Save source' : 'Create source'}</button>
            {editing && <button type="button" onClick={() => { setEditing(null); setFormError(''); }}>Cancel</button>}</div>
        </fieldset>
      </form>
    </div>
  </section>;
}
