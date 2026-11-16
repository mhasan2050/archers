// Runtime validation coverage for: authentication, permission gating, and administration forms.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, ApiError } from './api';
import { defaultSource } from './Admin';
import type { Session, Source, User } from './contracts';

vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>();
  return { ...original, api: {
    me: vi.fn(), login: vi.fn(), logout: vi.fn(),
    users: vi.fn(), createUser: vi.fn(), updateUser: vi.fn(),
    sources: vi.fn(), sourceStatus: vi.fn(), createSource: vi.fn(), updateSource: vi.fn(),
  } };
});
const account: User = { id: '11111111-1111-4111-8111-111111111111', username: 'operator', role: 'admin', disabled: false, revision: 4 };
const session: Session = { user: account, permissions: ['users.manage', 'sources.manage', 'export.manage'], csrf_token: 'test-csrf', expires_at: '2099-01-01T00:00:00Z' };
const source: Source = { ...defaultSource, id: account.id, revision: 8, connector: { kind: 'filesystem', mount_alias: 'approved_mount', sentinel_id: account.id } };
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.me).mockResolvedValue(session);
  vi.mocked(api.users).mockResolvedValue({ items: [account], next_cursor: null });
  vi.mocked(api.sources).mockResolvedValue({ items: [source], next_cursor: null });
});

describe('authentication UX', () => {
  it('shows login after 401, clears password on submit, and signs out with CSRF', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.me).mockRejectedValueOnce(new ApiError(401, 'UNAUTHENTICATED', 'Sign in.'));
    vi.mocked(api.login).mockResolvedValueOnce(session);
    vi.mocked(api.logout).mockResolvedValueOnce(undefined);
    render(<App />);
    await visitor.type(await screen.findByLabelText('Username'), 'operator');
    await visitor.type(screen.getByLabelText('Password'), 'synthetic-password');
    await visitor.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(api.login).toHaveBeenCalledWith('operator', 'synthetic-password');
    await visitor.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(api.logout).toHaveBeenCalledWith('test-csrf');
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toHaveValue('');
  });
  it('keeps a failed login explicit and clears the password before awaiting the server', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.me).mockRejectedValueOnce(new ApiError(401, 'UNAUTHENTICATED', 'Sign in.'));
    vi.mocked(api.login).mockRejectedValueOnce(new ApiError(401, 'UNAUTHENTICATED', 'Invalid credentials.'));
    render(<App />);
    await visitor.type(await screen.findByLabelText('Username'), 'operator');
    await visitor.type(screen.getByLabelText('Password'), 'synthetic-password');
    await visitor.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid credentials');
    expect(screen.getByLabelText('Password')).toHaveValue('');
  });
  it.each(['reader', 'curator', 'future-admin'])('does not offer admin or fake query UI to %s', async role => {
    vi.mocked(api.me).mockResolvedValueOnce({ ...session, user: { ...account, role } });
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(screen.getByText(/Query and ingestion interfaces are not yet implemented/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Users' })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(api.users).not.toHaveBeenCalled();
    expect(api.sources).not.toHaveBeenCalled();
  });
  it('does not infer missing permissions from the admin role', async () => {
    vi.mocked(api.me).mockResolvedValueOnce({ ...session, permissions: ['sources.manage'] });
    render(<App />);
    expect(await screen.findByRole('button', { name: 'Sources' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Users' })).not.toBeInTheDocument();
    expect(api.users).not.toHaveBeenCalled();
  });
  it('does not silently claim logout success when the server is unavailable', async () => {
    vi.mocked(api.logout).mockRejectedValueOnce(new ApiError(503, 'DEPENDENCY_UNAVAILABLE', 'Server unavailable.'));
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Server unavailable');
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  });
});

describe('administration behavior', () => {
  it('shows real source scan health and explicit status failures', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.sourceStatus).mockResolvedValueOnce({
      source_id: source.id, health: 'degraded', last_healthy_scan_at: null,
      snapshot_backlog: 2, tracked_files: 5, missing_files: 0,
      deletion_frozen: true, deletion_alert_revision: null,
      error_code: 'SOURCE_SCAN_STALE', file_errors: 1,
    }).mockRejectedValueOnce(new ApiError(503, 'DEPENDENCY_UNAVAILABLE', 'Status unavailable.'));
    render(<App />);
    await visitor.click(await screen.findByRole('button', { name: 'Sources' }));
    await visitor.click(await screen.findByRole('button', { name: 'Check ingestion status' }));
    expect(await screen.findByText(/Source health: degraded/)).toHaveTextContent('Tracked files: 5');
    expect(screen.getByText(/Scanner code: SOURCE_SCAN_STALE/)).toBeInTheDocument();
    await visitor.click(screen.getByRole('button', { name: 'Check ingestion status' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Status unavailable');
    expect(screen.queryByText(/Source health: degraded/)).not.toBeInTheDocument();
  });
  it('creates a reader with validated password length and reloads metadata after editing', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.createUser).mockResolvedValueOnce({ ...account, username: 'new-reader', role: 'reader' });
    vi.mocked(api.updateUser).mockResolvedValueOnce({ ...account, disabled: true });
    render(<App />);
    await screen.findByRole('heading', { name: 'Provision user' });
    await visitor.type(screen.getByLabelText('Username'), 'new-reader');
    const password = screen.getByLabelText('Initial password');
    expect(password).toHaveAttribute('minlength', '15');
    expect(password).toHaveAttribute('maxlength', '1024');
    await visitor.type(password, 'synthetic-password');
    await visitor.click(screen.getByRole('button', { name: 'Create user' }));
    expect(api.createUser).toHaveBeenCalledWith({ username: 'new-reader', password: 'synthetic-password', role: 'reader' }, 'test-csrf');
    await screen.findByText('Saved successfully.');
    await visitor.click(screen.getByRole('button', { name: 'Edit user new-reader' }));
    await visitor.click(screen.getByLabelText('Disable account'));
    await visitor.click(screen.getByRole('button', { name: 'Save user' }));
    expect(api.updateUser).toHaveBeenCalledWith(expect.objectContaining({ revision: 4 }), { role: 'reader', disabled: true }, 'test-csrf');
    await waitFor(() => expect(api.me).toHaveBeenCalledTimes(2));
  });
  it('registers the exact source DTO, rejects empty formats, and starts with export denied', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.createSource).mockResolvedValueOnce(source);
    render(<App />);
    await visitor.click(await screen.findByRole('button', { name: 'Sources' }));
    await visitor.type(screen.getByLabelText('Approved mount alias'), 'approved_mount');
    await visitor.type(screen.getByLabelText('Approved sentinel UUID'), account.id);
    await visitor.click(screen.getByLabelText('pdf'));
    await visitor.click(screen.getByRole('button', { name: 'Create source' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Select at least one allowed format');
    expect(api.createSource).not.toHaveBeenCalled();
    await visitor.click(screen.getByLabelText('pdf'));
    await visitor.click(screen.getByRole('button', { name: 'Create source' }));
    expect(api.createSource).toHaveBeenCalledWith({
      connector: source.connector,
      formats: ['pdf'],
      enabled: true,
      scan_policy: { poll_seconds: 30, stable_seconds: 60, deletion_grace_seconds: 300, completion_mode: 'atomic_rename' },
      export_policy: { allow_external_generation: false, allow_external_embeddings: false },
    }, 'test-csrf');
  });
  it('patches source policy without mutating the connector and supports explicit export approval', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.updateSource).mockResolvedValueOnce({ ...source, enabled: false, revision: 9 });
    render(<App />);
    await visitor.click(await screen.findByRole('button', { name: 'Sources' }));
    await visitor.click(await screen.findByRole('button', { name: 'Edit source approved_mount' }));
    expect(screen.queryByLabelText('Approved mount alias')).not.toBeInTheDocument();
    await visitor.click(screen.getByLabelText('Enabled (pausing is not deletion)'));
    await visitor.click(screen.getByLabelText('Allow external generation'));
    await visitor.click(screen.getByRole('button', { name: 'Save source' }));
    expect(api.updateSource).toHaveBeenCalledWith(source, {
      enabled: false,
      formats: ['pdf'],
      scan_policy: source.scan_policy,
      export_policy: { allow_external_generation: true, allow_external_embeddings: false },
    }, 'test-csrf');
  });
  it('preserves export flags if export management permission is absent', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.me).mockResolvedValueOnce({ ...session, permissions: ['sources.manage'] });
    vi.mocked(api.updateSource).mockResolvedValueOnce(source);
    render(<App />);
    await visitor.click(await screen.findByRole('button', { name: 'Edit source approved_mount' }));
    expect(screen.queryByLabelText('Allow external generation')).not.toBeInTheDocument();
    await visitor.click(screen.getByRole('button', { name: 'Save source' }));
    expect(api.updateSource).toHaveBeenCalledWith(source, expect.not.objectContaining({ export_policy: expect.anything() }), 'test-csrf');
  });
  it('displays conflicts, does not auto-retry, and refreshes before another edit', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.updateUser).mockRejectedValueOnce(new ApiError(409, 'REVISION_CONFLICT', 'Refresh and review before saving again.'));
    render(<App />);
    await visitor.click(await screen.findByRole('button', { name: 'Edit user operator' }));
    await visitor.click(screen.getByRole('button', { name: 'Save user' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('REVISION_CONFLICT');
    expect(api.updateUser).toHaveBeenCalledTimes(1);
    await visitor.click(screen.getByRole('button', { name: 'Refresh list' }));
    expect(await screen.findByRole('heading', { name: 'Provision user' })).toBeInTheDocument();
    await waitFor(() => expect(api.users).toHaveBeenCalledTimes(2));
  });
  it('renders backend failures rather than success-shaped empty lists', async () => {
    vi.mocked(api.users).mockRejectedValueOnce(new ApiError(503, 'DEPENDENCY_UNAVAILABLE', 'Try later.'));
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('DEPENDENCY_UNAVAILABLE');
    expect(screen.queryByText('No users found.')).not.toBeInTheDocument();
  });
  it('removes administration immediately when a mutation is denied as unauthenticated', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.updateUser).mockRejectedValueOnce(new ApiError(401, 'UNAUTHENTICATED', 'Your session was revoked.'));
    render(<App />);
    await visitor.click(await screen.findByRole('button', { name: 'Edit user operator' }));
    await visitor.click(screen.getByRole('button', { name: 'Save user' }));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Your session was revoked');
    expect(screen.queryByRole('button', { name: 'Users' })).not.toBeInTheDocument();
  });
  it('rechecks a changed role after saving and drops administration affordances', async () => {
    const visitor = userEvent.setup();
    vi.mocked(api.me).mockResolvedValueOnce(session).mockResolvedValueOnce({
      ...session, user: { ...account, role: 'reader', revision: 5 }, permissions: ['query'],
    });
    vi.mocked(api.updateUser).mockResolvedValueOnce({ ...account, role: 'reader', revision: 5 });
    render(<App />);
    await visitor.click(await screen.findByRole('button', { name: 'Edit user operator' }));
    await visitor.selectOptions(screen.getByLabelText('Role'), 'reader');
    await visitor.click(screen.getByRole('button', { name: 'Save user' }));
    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Users' })).not.toBeInTheDocument();
  });
});
