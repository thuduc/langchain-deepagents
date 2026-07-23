export interface CurrentUser {
  id: string;
  subject: string;
  roles: string[];
  is_project_admin: boolean;
}

export interface DevelopmentIdentity {
  subject: string;
  project_admin: boolean;
}

export interface AuthConfig {
  development_login_enabled: boolean;
  cdx_header_present: boolean;
  development_identity?: DevelopmentIdentity;
}

export interface AppSettings {
  available_models: string[];
  default_model: string;
  max_sessions_per_project: number;
}

interface Skill {
  name: string;
  description: string;
}

export interface Project {
  id: string;
  name: string;
  slug: string;
  status: string;
  content_revision: number;
  created_at: string;
  updated_at: string;
  skills?: Skill[];
}

export interface Session {
  id: string;
  project_id: string;
  title: string;
  thread_id?: string;
  created_at: string;
  updated_at: string;
  active_run_id?: string | null;
  active_run_status?: string | null;
}

export interface ChatMessage {
  id?: number;
  role: "user" | "assistant";
  content: string;
  created_at?: string;
  run_id?: string | null;
  duration_seconds?: number | null;
  optimistic?: boolean;
}

export interface TaskRun {
  id: string;
  status: "running" | "completed" | "failed" | "cancelled";
  latest_status: string;
  project_revision: number;
  created_at: string;
  completed_at?: string | null;
}

export interface ActiveRun {
  key: string;
  projectId: string;
  sessionId: string;
  runId: string | null;
  status: string;
  partialResponse: string;
  streamConnected: boolean;
  error: string | null;
}

export interface FileItem {
  path: string;
  name?: string;
  type: "file" | "directory";
  size: number;
  updated_at?: string;
  has_children?: boolean;
  mime_type?: string;
}

export interface ProjectFilePreview extends FileItem {
  kind: "markdown" | "code" | "text" | "csv" | "image" | "pdf" | "video" | "audio" | "unsupported";
  language?: string | null;
  content?: string;
  columns?: string[];
  rows?: string[][];
  truncated?: boolean;
  message?: string;
}

export interface UploadPreview {
  upload_token: string;
  entries: FileItem[];
  total_size: number;
  entry_count: number;
}

export interface StreamEvent<T = Record<string, unknown>> {
  type: string;
  data: T;
}
