import type { CreateSource, CreateUser, Page, Session, Source, SourceStatus, UpdateSource, UpdateUser, User } from './contracts';

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...options,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json', ...options.headers },
    });
  } catch {
    throw new ApiError(0, 'NETWORK_ERROR', 'Cannot reach the server. Check your connection; refresh before retrying a creation.');
  }
  if (!response.ok) {
    let code = 'REQUEST_FAILED';
    try {
      const body: unknown = await response.json();
      if (body && typeof body === 'object' && 'error' in body) {
        const error = body.error;
        if (error && typeof error === 'object' && 'code' in error
          && typeof error.code === 'string' && /^[A-Z_]{1,80}$/.test(error.code)) code = error.code;
      }
    } catch { /* Do not expose raw error pages, content, or credentials. */ }
    const message = response.status === 401 ? (path === '/auth/login' ? 'Sign-in failed. Check your credentials.' : 'Your session is unavailable. Sign in again.')
      : response.status === 403 ? 'The server denied this action. Your permissions may have changed.'
        : response.status === 409 ? 'The resource changed or the request conflicts. Refresh and review before saving again.'
          : response.status === 429 ? 'Too many requests. Wait before trying again.'
            : `The server rejected the request (HTTP ${response.status}). Check the fields or contact your administrator.`;
    throw new ApiError(response.status, code, message);
  }
  if (response.status === 204) return undefined as T;
  try {
    return await response.json() as T;
  } catch {
    throw new ApiError(response.status, 'INVALID_RESPONSE', 'The server returned an invalid response. Refresh before retrying.');
  }
}

function mutation(method: string, csrf: string, body?: unknown, revision?: number): RequestInit {
  if (!csrf) throw new ApiError(0, 'CSRF_REQUIRED', 'Session metadata is missing. Sign in again.');
  if (revision !== undefined && (!Number.isSafeInteger(revision) || revision < 1)) {
    throw new ApiError(0, 'INVALID_REVISION', 'Refresh this resource before saving.');
  }
  return {
    method,
    headers: {
      'X-CSRF-Token': csrf,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...(method === 'POST' && body !== undefined ? { 'Idempotency-Key': crypto.randomUUID() } : {}),
      ...(revision === undefined ? {} : { 'If-Match': `"${revision}"` }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}
const cursorQuery = (cursor?: string) => cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
export const api = {
  me: () => request<Session>('/auth/me'),
  login: (username: string, password: string) => request<Session>('/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }),
  }),
  logout: (csrf: string) => request<void>('/auth/logout', mutation('POST', csrf)),
  users: (cursor?: string) => request<Page<User>>(`/admin/users${cursorQuery(cursor)}`),
  createUser: (body: CreateUser, csrf: string) => request<User>('/admin/users', mutation('POST', csrf, body)),
  updateUser: (user: User, body: UpdateUser, csrf: string) =>
    request<User>(`/admin/users/${encodeURIComponent(user.id)}`, mutation('PATCH', csrf, body, user.revision)),
  sources: (cursor?: string) => request<Page<Source>>(`/admin/sources${cursorQuery(cursor)}`),
  sourceStatus: (id: string) => request<SourceStatus>(`/sources/${encodeURIComponent(id)}/status`),
  createSource: (body: CreateSource, csrf: string) => request<Source>('/admin/sources', mutation('POST', csrf, body)),
  updateSource: (source: Source, body: UpdateSource, csrf: string) =>
    request<Source>(`/admin/sources/${encodeURIComponent(source.id)}`, mutation('PATCH', csrf, body, source.revision)),
};

export function errorText(error: unknown) {
  return error instanceof ApiError ? `${error.code}: ${error.message}` : 'An unexpected error occurred. Refresh and try again.';
}
