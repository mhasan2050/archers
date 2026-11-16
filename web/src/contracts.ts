export const roles = ['reader', 'curator', 'admin'] as const;
export type Role = (typeof roles)[number];
export interface User {
  id: string;
  username: string;
  // A future/unknown server role must never inherit administration affordances.
  role: string;
  disabled: boolean;
  revision: number;
}
export interface Session {
  user: User;
  permissions: string[];
  csrf_token: string;
  expires_at: string;
}
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
export interface CreateUser {
  username: string;
  password: string;
  role: Role;
}
export interface UpdateUser {
  role?: Role;
  disabled?: boolean;
  password?: string;
}
export const formats = ['pdf', 'docx', 'pptx', 'xlsx', 'markdown', 'text', 'html', 'csv'] as const;
export type Format = (typeof formats)[number];
export interface ScanPolicy {
  poll_seconds: number;
  stable_seconds: number;
  deletion_grace_seconds: number;
  completion_mode: 'atomic_rename' | 'ready_manifest' | 'heuristic_double_hash' | 'remote_revision';
}
export interface ExportPolicy {
  allow_external_generation: boolean;
  allow_external_embeddings: boolean;
}
export type Connector =
  | { kind: 'filesystem'; mount_alias: string; sentinel_id: string }
  | { kind: 's3'; endpoint_alias: string; bucket: string; prefix: string; credential_ref: string }
  | { kind: 'sharepoint'; directory_tenant_id: string; site_or_drive_id: string; folder_id: string; credential_ref: string }
  | { kind: 'google_drive'; drive_id: string; folder_id: string; credential_ref: string };
export interface CreateSource {
  connector: Connector;
  formats: Format[];
  export_policy: ExportPolicy;
  enabled: boolean;
  scan_policy: ScanPolicy;
}
export interface Source extends CreateSource {
  id: string;
  revision: number;
}
export interface SourceStatus {
  source_id: string;
  health: 'unknown' | 'healthy' | 'degraded' | 'unavailable' | 'paused';
  last_healthy_scan_at: string | null;
  snapshot_backlog: number;
  tracked_files: number;
  missing_files: number;
  deletion_frozen: boolean;
  deletion_alert_revision: number | null;
  error_code: string | null;
  file_errors: number;
}
export type UpdateSource = Partial<Omit<CreateSource, 'connector'>>;

export function canManage(session: Session, permission: 'users.manage' | 'sources.manage') {
  return session.user.role === 'admin' && !session.user.disabled
    && Date.parse(session.expires_at) > Date.now()
    && session.permissions.includes(permission);
}
