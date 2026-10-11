// Stage families and colours (POC_07 §1): one colour per family, used everywhere stages appear.

export type StageFamily = "ingress" | "identity" | "automation" | "agent" | "tool" | "gate" | "outbound" | "voice" | "workflow";

const FAMILY: Record<string, StageFamily> = {
  ingress: "ingress",
  inbox: "ingress",
  parse: "ingress",
  identity: "identity",
  link_code: "identity",
  human_takeover: "identity",
  automation_route: "automation",
  call_route: "voice",
  turn_scheduled: "agent",
  turn_started: "agent",
  context: "agent",
  model_call: "agent",
  embedding: "agent",
  turn_finished: "agent",
  turn_superseded: "agent",
  turn_error: "agent",
  reply: "agent",
  tool_call: "tool",
  gate: "gate",
  outbound: "outbound",
  sim_message: "outbound",
  send: "outbound",
  sent: "outbound",
  owner_command: "identity",
  voice_turn: "voice",
  call_event: "voice",
};

export const familyOf = (stage: string): StageFamily =>
  FAMILY[stage] ??
  (stage.startsWith("outbound") ? "outbound" : stage.startsWith("wf_") ? "workflow" : stage.startsWith("voice") ? "voice" : "agent");

export const FAMILY_COLOR: Record<StageFamily, string> = {
  ingress: "var(--color-st-ingress)",
  identity: "var(--color-st-identity)",
  automation: "var(--color-st-automation)",
  agent: "var(--color-st-agent)",
  tool: "var(--color-st-tool)",
  gate: "var(--color-st-gate)",
  outbound: "var(--color-st-outbound)",
  voice: "var(--color-st-voice)",
  workflow: "var(--color-st-workflow)",
};

export const stageColor = (stage: string, status?: string) =>
  status === "error" ? "var(--color-st-error)" : FAMILY_COLOR[familyOf(stage)];

export const OUTCOME_LABEL: Record<string, string> = {
  send: "replied",
  ask_owner: "asked owner",
  handoff: "handoff",
  no_reply: "no reply",
  fallback: "fallback",
  consumed_by_automation: "automation",
  routed_to_call: "to call",
  superseded: "superseded",
  error: "error",
};

export const CHANNEL_ICON: Record<string, string> = {
  whatsapp: "🟢",
  meta_whatsapp: "🟢",
  telegram: "✈️",
  facebook_messenger: "💬",
  meta_pages: "💬",
  instagram_dm: "📸",
  instagram_comment: "📸",
  meta_instagram: "📸",
  web_widget: "🧪",
  simulator: "🧪",
  voice: "📞",
};

export const fmtMs = (ms: number | null | undefined) =>
  ms === null || ms === undefined ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;

export const fmtTime = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");
