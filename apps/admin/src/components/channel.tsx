// Channel identity as an icon tile (no emojis): WhatsApp, Telegram, Messenger, Instagram, simulator, voice.
import { Camera, FlaskConical, MessageCircle, MessageSquare, Phone, Send, type LucideIcon } from "lucide-react";

import { cx } from "./ui";

const CHANNELS: Record<string, { icon: LucideIcon; label: string; tone: string }> = {
  whatsapp: { icon: MessageCircle, label: "WhatsApp", tone: "bg-[#e7f8ee] text-[#128c4a]" },
  meta_whatsapp: { icon: MessageCircle, label: "WhatsApp", tone: "bg-[#e7f8ee] text-[#128c4a]" },
  telegram: { icon: Send, label: "Telegram", tone: "bg-[#e6f3fb] text-[#1d8acb]" },
  facebook_messenger: { icon: MessageSquare, label: "Messenger", tone: "bg-[#e9f0ff] text-[#2563eb]" },
  meta_pages: { icon: MessageSquare, label: "Messenger", tone: "bg-[#e9f0ff] text-[#2563eb]" },
  instagram_dm: { icon: Camera, label: "Instagram", tone: "bg-[#fdeaf3] text-[#c2185b]" },
  instagram_comment: { icon: Camera, label: "Instagram", tone: "bg-[#fdeaf3] text-[#c2185b]" },
  meta_instagram: { icon: Camera, label: "Instagram", tone: "bg-[#fdeaf3] text-[#c2185b]" },
  web_widget: { icon: FlaskConical, label: "Simulator", tone: "bg-bone text-dusk" },
  simulator: { icon: FlaskConical, label: "Simulator", tone: "bg-bone text-dusk" },
  voice: { icon: Phone, label: "Voice", tone: "bg-signal-l text-signal" },
};

export const channelLabel = (channel: string | null | undefined) =>
  CHANNELS[channel ?? ""]?.label ?? (channel ? channel.replace(/_/g, " ") : "—");

export function ChannelIcon({ channel, size = "md", className }: { channel: string | null | undefined; size?: "sm" | "md" | "lg"; className?: string }) {
  const c = CHANNELS[channel ?? ""] ?? { icon: MessageSquare, label: channel ?? "", tone: "bg-bone text-dusk" };
  const box = { sm: "h-5 w-5 rounded", md: "h-7 w-7 rounded-md", lg: "h-9 w-9 rounded-lg" }[size];
  const px = { sm: 12, md: 15, lg: 18 }[size];
  return (
    <span title={c.label} className={cx("inline-flex shrink-0 items-center justify-center", box, c.tone, className)}>
      <c.icon size={px} strokeWidth={2} />
    </span>
  );
}

/** How an identity was tied to the customer. */
export const LINKED: Record<string, string> = {
  first_contact: "first contact",
  link_code: "linked by code",
  payment: "linked by payment",
  same_wa_user: "same WhatsApp user",
  owner_confirmed: "owner confirmed",
};
