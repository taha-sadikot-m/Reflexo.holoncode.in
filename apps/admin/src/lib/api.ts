// Admin API client: cookie session (sent automatically), CSRF header on mutations, problem+json errors.

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

let csrfToken: string | null = null;
export const setCsrf = (token: string | null) => {
  csrfToken = token;
};

const BASE = "/admin/api";

async function request<T>(method: string, path: string, body?: unknown, params?: Record<string, string>): Promise<T> {
  const url = new URL(BASE + path, window.location.origin);
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== "") url.searchParams.set(k, v);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const resp = await fetch(url, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    let code = "ERROR";
    let detail = resp.statusText;
    try {
      const problem = await resp.json();
      code = problem.code ?? code;
      detail = problem.detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, code, detail);
  }
  return (await resp.json()) as T;
}

export const api = {
  get: <T>(path: string, params?: Record<string, string>) => request<T>("GET", path, undefined, params),
  post: <T>(path: string, body?: unknown, params?: Record<string, string>) => request<T>("POST", path, body ?? {}, params),
};

// ---------------------------------------------------------------- types (mirror reflexo.admin.views)

export interface Me {
  user: string;
  csrf: string;
  expires_at: number;
  env: string;
  demo_time_scale: number;
  links: { jaeger: string; temporal: string };
}
export interface Business {
  id: string;
  name: string;
  slug: string;
  status: string;
  kill_switch: boolean;
}
export interface TraceSummary {
  trace_id: string;
  started_at: string | null;
  total_ms: number;
  stages: number;
  provider: string | null;
  conversation_id: string | null;
  turn_id: string | null;
  outcome: string | null;
  model: string | null;
  hedged: boolean;
  gate: boolean | null;
  simulated: boolean;
  errors: number;
  gate_repairs?: number;
  last_stage: string;
  snippet?: string | null;
}
export interface TraceEvent {
  id: string;
  stage: string;
  status: string;
  start_ms: number;
  duration_ms: number | null;
  at: string | null;
  turn_id: string | null;
  attrs: Record<string, unknown>;
}
export interface TraceDetail {
  summary: TraceSummary;
  events: TraceEvent[];
  runs: Record<string, unknown>[];
  outbound: { id: string; part: number; status: string; provider_message_id: string | null; idempotency_key: string; attempts: number; sent_at: string | null }[];
}
export interface ConversationItem {
  id: string;
  channel: string;
  role: string;
  customer: string | null;
  last_text: string;
  last_direction: string;
  last_at: string | null;
  needs_attention: boolean;
  human_active: boolean;
  simulated: boolean;
}
export interface ThreadMessage {
  id: string;
  direction: string;
  author: string;
  text: string | null;
  blocks: Record<string, unknown>[] | null;
  attachments: Record<string, unknown>[];
  interactive: { option_id?: string; title?: string } | null;
  status: string;
  turn_id: string | null;
  error: Record<string, unknown> | null;
  at: string | null;
  delivered_at: string | null;
  read_at: string | null;
}
export interface Thread {
  id: string;
  channel: string;
  role: string;
  needs_attention: boolean;
  simulated: boolean;
  human_active_until: string | null;
  customer: { id: string; name: string | null; phone: string | null; language: string | null; facts: { key: string; value: unknown; source?: string }[] } | null;
  messages: ThreadMessage[];
}
export interface Health {
  postgres: string;
  redis: string;
  workers: { id: string; service: string; pools: string[]; age_s: number; alive: boolean; stats: Record<string, unknown> }[];
  queues: { pool: string; depth: number; oldest_s: number }[];
  turns_running?: number;
  dead_letters?: number;
}
export interface XrayEvent {
  type: "xray";
  id: string;
  trace_id: string;
  stage: string;
  status: string;
  duration_ms: number | null;
  conversation_id: string | null;
  turn_id: string | null;
  attrs: Record<string, unknown>;
  at: string | null;
}
export interface ChaosOutcome {
  kind: string;
  expected: string;
  ok: boolean;
  checks: Record<string, unknown>;
  seconds: number;
  conversation_id: string | null;
}
export interface ChaosRun {
  id: string;
  kind: string;
  state: "running" | "done" | "abandoned";
  actor: number | null;
  outcome: ChaosOutcome | null;
  at: string;
}
export interface ChaosOverview {
  actions: { kind: string; expected: string }[];
  flags: Record<string, { value: unknown; expires_at: string | null }>;
  recent: ChaosRun[];
}
