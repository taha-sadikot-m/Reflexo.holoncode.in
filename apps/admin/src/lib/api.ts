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
export interface SimThreadItem {
  at: string;
  from: "customer" | "business";
  text?: string | null;
  option?: string | null;
  payload?: Record<string, any>;
  status?: string;
}
export interface SimThread {
  conversation_id: string | null;
  items: SimThreadItem[];
  payment_links?: { id: string; amount_paise: number; status: string; url: string; order_number: string }[];
}
export interface ScenarioSummary {
  id: string;
  title: string;
  covers: string;
  steps: number;
}
export interface ScenarioStep {
  index: number;
  label: string;
  status: "pending" | "running" | "ok" | "failed" | "skipped";
  detail: string | null;
  seconds: number | null;
}
export interface ScenarioRun {
  id: string;
  scenario_id: string;
  title: string;
  business_id: string;
  status: "running" | "passed" | "failed" | "stopped";
  steps: ScenarioStep[];
  actors: Record<string, number>;
  started_at: string;
  finished_at: string | null;
}

// ---------------------------------------------------------------- workspace pages (WP-13a)

export interface OrderRow {
  id: string;
  order_number: string;
  status: string;
  payment_method: string | null;
  payment_status: string | null;
  total_rupees: number | null;
  delivery_date: string | null;
  delivery_slot: string | null;
  channel: string | null;
  customer: string | null;
  items: string;
  simulated: boolean;
  cancelled_by: string | null;
  created_at: string;
}
export interface Proposal {
  id: string;
  capability: string;
  status: string;
  summary: string | null;
  outcome: Record<string, any> | null;
  created_at: string;
  decided_at: string | null;
}
export interface OrderDetail {
  id: string;
  order_number: string;
  status: string;
  customer: string | null;
  conversation_id: string | null;
  payment_method: string | null;
  payment_status: string | null;
  subtotal_rupees: number | null;
  delivery_fee_rupees: number | null;
  total_rupees: number | null;
  delivery_date: string | null;
  delivery_slot: string | null;
  address: string | null;
  notes: string | null;
  details: Record<string, unknown>;
  missing_details: string[];
  created_by: string | null;
  created_at: string;
  confirmed_at: string | null;
  cancelled: { at: string; by: string | null; reason: string | null } | null;
  items: { name: string; quantity: number; unit_rupees: number; line_rupees: number; details: Record<string, unknown> }[];
  payment_links: { id: string; amount_rupees: number; status: string; url: string | null; created_at: string; paid_at: string | null }[];
  timeline: { type: string; at: string; source: string; payload: Record<string, unknown> }[];
  proposals: Proposal[];
}
export interface CapacityRow {
  key: string;
  used: number;
  limit: number;
  automation: string;
}
export interface AutomationRow {
  id: string;
  name: string;
  description: string | null;
  status: string;
  version: number | null;
  executor: string | null;
  readback: string | null;
  runs_today: number;
  live_runs: number;
  created_at: string;
}
export interface AutomationRun {
  id: string;
  instance_key: string;
  status: string;
  workflow_id: string | null;
  version: number;
  started_at: string;
  finished_at: string | null;
  error: Record<string, unknown> | null;
  steps: { step_id: string; status: string; output: unknown; error: unknown; started_at: string | null; finished_at: string | null }[];
  waits: { event_type: string; status: string; expires_at: string | null }[];
}
export interface AutomationDetail extends Omit<AutomationRow, "runs_today" | "live_runs" | "created_at"> {
  definition: { trigger?: unknown; steps?: { id: string; kind?: string; type?: string; [k: string]: unknown }[]; [k: string]: unknown };
  activated_at: string | null;
  runs: AutomationRun[];
}
export interface PlaybookRuleRow {
  id: string;
  capability: string;
  effect: "allow" | "deny" | "propose";
  readback: string;
  says: string;
  scope: string;
  customer: string | null;
  source: string;
  source_decision: { approval_id: string; summary: string | null; decided_at: string | null } | null;
  status: string;
  change_set_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface PlaybookProposal {
  id: string;
  capability: string;
  label: string;
  summary: string | null;
  reason: string | null;
  customer: string | null;
  owner_initiated: boolean;
  status: string;
  done: boolean | null;
  learned: string | null;
  conversation_id: string | null;
  created_at: string;
  decided_at: string | null;
  decided_via: string | null;
}
export interface OwnerWorkspace {
  questions: {
    id: string;
    question: string;
    ai_guess: string | null;
    status: string;
    code: string | null;
    answer: string | null;
    scope: string | null;
    customer: string | null;
    conversation_id: string | null;
    asked_at: string | null;
    answered_at: string | null;
    relayed_at: string | null;
    sla_due_at: string | null;
  }[];
  approvals: {
    id: string;
    kind: string;
    status: string;
    summary: string | null;
    code: string | null;
    requested_by: string | null;
    expires_at: string | null;
    decided_at: string | null;
    decided_via: string | null;
    created_at: string;
  }[];
  notifications: { kind: string; priority: string; status: string; text: string | null; delivered_via: string | null; delivered_at: string | null; created_at: string }[];
  files: { id: string; filename: string; kind: string; description: string | null; received_via: string | null; mime: string; size_bytes: number; imports: number; created_at: string }[];
}
export interface ChangeSetRow {
  id: string;
  origin: string;
  summary: string;
  status: string;
  revert_of: string | null;
  applied_at: string | null;
  reverted_at: string | null;
  created_at: string;
  items: { entity: string; op: string; before: Record<string, unknown> | null; after: Record<string, unknown> | null }[];
}
export interface ImportRow {
  id: string;
  source_kind: string;
  filename: string | null;
  status: string;
  stats: Record<string, unknown>;
  change_set_id: string | null;
  workflow_id: string | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  items: { extracted: Record<string, unknown>; confidence: number | null; issues: string[] | null; resolution: string | null }[];
}
export interface CallRow {
  id: string;
  direction: string;
  path: string;
  status: string;
  caller: string | null;
  started_at: string | null;
  seconds: number | null;
  end_reason: string | null;
  summary: string | null;
  transcript: { role?: string; text?: string; at?: string }[];
  conversation_id: string | null;
  events: { kind: string; status: string; at: string; payload: Record<string, unknown> }[];
}
