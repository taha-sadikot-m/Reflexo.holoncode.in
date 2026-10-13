// The owner's "Try & teach": set the shop up by chatting with the AI employee on the owner's phone, and test it
// as a customer on the other. Until a real WhatsApp number is connected these are the shop's channels.
import { useQuery } from "@tanstack/react-query";
import { Check, FileImage, MessageCircleQuestion, Rocket, ShoppingBag, type LucideIcon } from "lucide-react";

import { PageHeader } from "../components/bits";
import { CustomerPhone, OwnerPhone, useShopPhones } from "../components/shop-phones";
import { cx } from "../components/ui";
import { api } from "../lib/api";
import { useSession } from "../lib/session";

interface Onboarding {
  state: string | null;
  business_status: string;
  products: number;
  answers: number;
  imports: number;
  customer_chats: number;
}

const AFTER_SOURCES = ["extracting", "gap_questions", "preview", "live"];

function Step({ icon: Icon, title, text, done, current }: { icon: LucideIcon; title: string; text: string; done: boolean; current: boolean }) {
  return (
    <div className={cx("flex gap-3 rounded-2xl border bg-white p-4 transition", current ? "border-signal shadow-glow" : done ? "border-verdant/30" : "border-sand")}>
      <span
        className={cx(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-xl",
          done ? "bg-verdant-l text-verdant" : current ? "bg-signal text-white" : "bg-bone text-stone",
        )}
      >
        {done ? <Check size={17} /> : <Icon size={17} />}
      </span>
      <div className="min-w-0">
        <div className="text-[14px] font-semibold text-ink">{title}</div>
        <div className="mt-0.5 text-[12.5px] leading-snug text-dusk">{text}</div>
      </div>
    </div>
  );
}

export function Setup() {
  const { business, me } = useSession();
  const bid = business?.id ?? "";
  const ownerName = me?.user ?? "Owner";
  const phones = useShopPhones(bid, "Test customer");
  const setup = useQuery({
    queryKey: ["onboarding", bid],
    queryFn: () => api.get<Onboarding>("/onboarding", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 3000,
  });
  const o = setup.data;
  const sent = !!o && (o.products > 0 || o.imports > 0 || AFTER_SOURCES.includes(o.state ?? ""));
  const answered = !!o && ["preview", "live"].includes(o.state ?? "");
  const live = o?.business_status === "active";
  const tested = !!o && o.customer_chats > 0;
  const steps = [
    { icon: FileImage, title: "Send your menu", text: "On your phone (right): a photo, PDF or Excel of your menu or price list — or just type it.", done: sent },
    { icon: MessageCircleQuestion, title: "Answer a few questions", text: "Delivery, payments, timings — in plain words. Skip anything you like.", done: answered },
    { icon: Rocket, title: "Approve and go live", text: "It shows you what it understood. Say yes and it starts answering customers.", done: live },
    { icon: ShoppingBag, title: "Test it as a customer", text: "Chat or call your shop from the customer phone (left) — ask, order, pay.", done: tested },
  ];
  const current = steps.findIndex((s) => !s.done);
  return (
    <div className="space-y-6">
      <PageHeader title="Try & teach">Set your AI employee up by chatting with it, then test it the way your customers will.</PageHeader>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {steps.map((s, i) => (
          <Step key={s.title} {...s} current={i === current} />
        ))}
      </div>
      {bid && (
        <div className="grid gap-6 lg:grid-cols-2 2xl:max-w-[1000px]">
          <CustomerPhone phones={phones} shopName={business?.name ?? "Your shop"} label="A customer" />
          <OwnerPhone
            phones={phones}
            ownerName={ownerName}
            label={`You — ${ownerName}'s phone`}
            emptyHint="Say hi, or send a photo of your menu with 📎 to get started."
          />
        </div>
      )}
    </div>
  );
}
