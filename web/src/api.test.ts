// Runtime validation coverage for: same-origin session and fenced administration requests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from './api';
import { defaultSource } from './Admin';
import { canManage } from './contracts';
import type { Session, Source, User } from './contracts';

const user: User = { id: '11111111-1111-4111-8111-111111111111', username: 'admin', role: 'admin', disabled: false, revision: 7 };
const session: Session = { user, permissions: ['users.manage', 'sources.manage'], csrf_token: 'synthetic-csrf', expires_at: '2099-01-01T00:00:00Z' };
const fetchMock = vi.fn<typeof fetch>();
beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation(async () => new Response(JSON.stringify(user), { status: 200 }));
});
afterEach(() => vi.unstubAllGlobals());

function lastRequest() {
  const call = fetchMock.mock.calls.at(-1)!;
  return { url: call[0], options: call[1]!, headers: new Headers(call[1]?.headers) };
}

describe('session HTTP contract', () => {
  it('sends only username/password at login, with the HttpOnly cookie managed by the browser', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(session)));
    expect(await api.login('admin', 'synthetic-password')).toEqual(session);
    const { url, options, headers } = lastRequest();
    expect(url).toBe('/api/v1/auth/login');
    expect(options.credentials).toBe('same-origin');
    expect(JSON.parse(options.body as string)).toEqual({ username: 'admin', password: 'synthetic-password' });
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.has('Authorization')).toBe(false);
    expect(headers.has('Idempotency-Key')).toBe(false);
  });
  it('loads session metadata without caching', async () => {
    await api.me();
    expect(lastRequest().url).toBe('/api/v1/auth/me');
    expect(lastRequest().options.cache).toBe('no-store');
  });
  it('logs out using the session CSRF token and no JSON payload', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(api.logout(session.csrf_token)).resolves.toBeUndefined();
    const { url, headers, options } = lastRequest();
    expect(url).toBe('/api/v1/auth/logout');
    expect(headers.get('X-CSRF-Token')).toBe(session.csrf_token);
    expect(options.body).toBeUndefined();
  });
});

describe('administration wire contract', () => {
  it('provisions a user with CSRF and a unique UUID idempotency key', async () => {
    const body = { username: 'new-reader', password: 'synthetic-password', role: 'reader' as const };
    await api.createUser(body, session.csrf_token);
    const first = lastRequest();
    await api.createUser(body, session.csrf_token);
    const second = lastRequest();
    expect(first.url).toBe('/api/v1/admin/users');
    expect(first.options.method).toBe('POST');
    expect(first.headers.get('X-CSRF-Token')).toBe(session.csrf_token);
    expect(first.headers.get('Idempotency-Key')).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    expect(second.headers.get('Idempotency-Key')).not.toBe(first.headers.get('Idempotency-Key'));
    expect(JSON.parse(first.options.body as string)).toEqual(body);
  });
  it('patches users with a quoted revision and never a creation key', async () => {
    await api.updateUser(user, { role: 'curator', disabled: true }, session.csrf_token);
    const { url, options, headers } = lastRequest();
    expect(url).toBe(`/api/v1/admin/users/${user.id}`);
    expect(options.method).toBe('PATCH');
    expect(headers.get('If-Match')).toBe('"7"');
    expect(headers.get('X-CSRF-Token')).toBe(session.csrf_token);
    expect(headers.has('Idempotency-Key')).toBe(false);
  });
  it('creates a typed filesystem source with both export gates denied', async () => {
    const body = { ...defaultSource, connector: { kind: 'filesystem' as const, mount_alias: 'approved_nas', sentinel_id: user.id } };
    await api.createSource(body, session.csrf_token);
    const { url, options, headers } = lastRequest();
    expect(url).toBe('/api/v1/admin/sources');
    expect(headers.has('Idempotency-Key')).toBe(true);
    expect(JSON.parse(options.body as string)).toEqual(body);
    expect(body.export_policy).toEqual({ allow_external_generation: false, allow_external_embeddings: false });
  });
  it('patches source policy only using CSRF and its current revision', async () => {
    const source: Source = { ...defaultSource, id: user.id, revision: 12 };
    await api.updateSource(source, { enabled: false }, session.csrf_token);
    const { url, options, headers } = lastRequest();
    expect(url).toBe(`/api/v1/admin/sources/${user.id}`);
    expect(headers.get('If-Match')).toBe('"12"');
    expect(headers.get('X-CSRF-Token')).toBe(session.csrf_token);
    expect(JSON.parse(options.body as string)).toEqual({ enabled: false });
  });
  it('encodes opaque pagination cursors rather than treating them as URL syntax', async () => {
    await api.users('a&b=c');
    expect(lastRequest().url).toBe('/api/v1/admin/users?cursor=a%26b%3Dc');
    await api.sources('a/b');
    expect(lastRequest().url).toBe('/api/v1/admin/sources?cursor=a%2Fb');
  });
  it('rejects missing CSRF and invalid revisions before sending requests', async () => {
    expect(() => api.updateUser(user, { disabled: true }, '')).toThrow(ApiError);
    expect(() => api.updateUser({ ...user, revision: 0 }, { disabled: true }, session.csrf_token)).toThrow(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('explicit safe failures and deny-by-default affordances', () => {
  it.each([401, 403, 409, 422, 429, 503])('surfaces HTTP %s without leaking response bodies', async status => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'VALIDATION_ERROR', message: 'private body must not be echoed' } }), { status }));
    await expect(api.me()).rejects.toMatchObject({ status, code: 'VALIDATION_ERROR' });
    fetchMock.mockResolvedValueOnce(new Response('private body must not be echoed', { status }));
    await expect(api.me()).rejects.not.toThrow('private body');
  });
  it('surfaces network failures with no transport exception content', async () => {
    fetchMock.mockRejectedValueOnce(new Error('private secret'));
    await expect(api.me()).rejects.toMatchObject({ status: 0, code: 'NETWORK_ERROR' });
  });
  it.each(['reader', 'curator', 'future-admin', 'ADMIN'])('denies role %s even with supplied admin permissions', role => {
    expect(canManage({ ...session, user: { ...user, role } }, 'users.manage')).toBe(false);
  });
  it('requires enabled, unexpired admin and exact permission', () => {
    expect(canManage(session, 'users.manage')).toBe(true);
    expect(canManage({ ...session, permissions: [] }, 'users.manage')).toBe(false);
    expect(canManage({ ...session, user: { ...user, disabled: true } }, 'users.manage')).toBe(false);
    expect(canManage({ ...session, expires_at: '2020-01-01T00:00:00Z' }, 'users.manage')).toBe(false);
  });
});
