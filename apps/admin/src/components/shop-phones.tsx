// A shop's two simulated phones side by side: the customer's WhatsApp (chat, photos, pay, voice call) and the
// owner's (where the AI asks, reports and learns). The public demo and an owner's "Try & teach" page both use it.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Phone, Sparkles } from "lucide-react";
import { useState, type ReactNode } from "react";

import { api, uploadSimMedia, type SimMediaRef, type SimThread } from "../lib/api";
import { CallScreen, useWebCall } from "./call";
import { Device } from "./phone";
import { cx } from "./ui";

const ACTOR = 1;
type Body = { text?: string; option_id?: string; option_title?: string; media?: SimMediaRef };

export function useShopPhones(businessId: string, customerName: string) {
  const qc = useQueryClient();
  const bid = businessId;
  const call = useWebCall(bid, ACTOR, customerName);
  const customer = useQuery({
    queryKey: ["sim-thread", bid, ACTOR],
    queryFn: () => api.get<SimThread>("/sim/thread", { business_id: bid, actor: String(ACTOR) }),
    refetchInterval: 1500,
  });
  const owner = useQuery({
    queryKey: ["sim-owner", bid],
    queryFn: () => api.get<SimThread>("/sim/owner/thread", { business_id: bid }),
    refetchInterval: 1500,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["sim-thread", bid] });
    void qc.invalidateQueries({ queryKey: ["sim-owner", bid] });
  };
  const sendCustomer = useMutation({
    mutationFn: (body: Body) => api.post("/sim/send", { actor: ACTOR, name: customerName, ...body }, { business_id: bid }),
    onSuccess: refresh,
  });
  const sendOwner = useMutation({
    mutationFn: (body: Body) => api.post("/sim/owner/send", body, { business_id: bid }),
    onSuccess: refresh,
  });
  const pay = useMutation({ mutationFn: (id: string) => api.post(`/sim/razorpay/pay/${id}`, {}, { business_id: bid }), onSuccess: refresh });
  // the AI asked with buttons and the owner has not answered yet
  const ownerWaiting = (owner.data?.items ?? []).some(
    (it, i, all) => it.from === "business" && it.payload?.type === "interactive" && !all.slice(i + 1).some((n) => n.from !== "business"),
  );
  return { bid, call, customer, owner, sendCustomer, sendOwner, pay, ownerWaiting };
}

export type ShopPhonesState = ReturnType<typeof useShopPhones>;

export function PhoneColumn({
  label,
  hint,
  tone,
  alert,
  className,
  children,
}: {
  label: string;
  hint: string;
  tone: "signal" | "marigold";
  alert?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={cx("min-w-0", className)}>
      <div className="mb-2.5 flex items-center justify-between px-1">
        <div>
          <div className={cx("text-[12px] font-semibold uppercase tracking-wider", tone === "signal" ? "text-signal" : "text-marigold-d")}>{label}</div>
          <div className={cx("text-[12.5px]", alert ? "font-medium text-marigold-d" : "text-stone")}>{hint}</div>
        </div>
        {alert && (
          <span className="relative flex h-2.5 w-2.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-marigold opacity-75" />
            <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-marigold" />
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

export function CustomerPhone({ phones, shopName, label, className }: { phones: ShopPhonesState; shopName: string; label: string; className?: string }) {
  const [draft, setDraft] = useState("");
  const { bid, call, customer, sendCustomer, pay } = phones;
  return (
    <PhoneColumn className={className} label={label} hint="WhatsApp, as your customers see it" tone="signal">
      <Device
        title={shopName}
        subtitle="online · replies instantly"
        thread={customer.data}
        error={customer.error ?? sendCustomer.error ?? pay.error}
        busy={sendCustomer.isPending || pay.isPending}
        onSend={(text) => sendCustomer.mutate({ text })}
        onTap={(o) => sendCustomer.mutate({ option_id: o.id, option_title: o.title })}
        onPay={(id) => pay.mutate(id)}
        draft={draft}
        onDraft={setDraft}
        onAttach={async (file, caption) => {
          const media = await uploadSimMedia(bid, file);
          await sendCustomer.mutateAsync({ media, text: caption || undefined });
        }}
        emptyHint={
          <>
            Say hi to {shopName} — or tap a suggestion.
            <br />
            Tap <Phone size={11} className="inline" /> above to call.
          </>
        }
        actions={
          <button onClick={() => void call.start()} disabled={call.state !== "idle"} className="rounded-full p-2 transition hover:bg-white/15 disabled:opacity-50" title="Voice call">
            <Phone size={18} />
          </button>
        }
        overlay={<CallScreen call={call} shop={shopName} />}
        height="h-[620px]"
      />
    </PhoneColumn>
  );
}

export function OwnerPhone({
  phones,
  ownerName,
  label,
  emptyHint,
  className,
}: {
  phones: ShopPhonesState;
  ownerName: string;
  label: string;
  emptyHint?: string;
  className?: string;
}) {
  const [draft, setDraft] = useState("");
  const { bid, owner, sendOwner, ownerWaiting } = phones;
  return (
    <PhoneColumn
      className={className}
      label={label}
      hint={ownerWaiting ? "The AI is waiting for your decision" : "Where the AI asks, reports and learns"}
      tone="marigold"
      alert={ownerWaiting}
    >
      <Device
        title={`Reflexo · ${ownerName}'s assistant`}
        subtitle="your AI employee"
        avatar={<Sparkles size={16} />}
        thread={owner.data}
        error={owner.error ?? sendOwner.error}
        busy={sendOwner.isPending}
        onSend={(text) => sendOwner.mutate({ text })}
        onTap={(o) => sendOwner.mutate({ option_id: o.id, option_title: o.title })}
        draft={draft}
        onDraft={setDraft}
        onAttach={async (file, caption) => {
          const media = await uploadSimMedia(bid, file);
          await sendOwner.mutateAsync({ media, text: caption || undefined });
        }}
        placeholder="Message your AI employee"
        emptyHint={
          emptyHint ??
          "When the AI isn't sure, it asks you here — with buttons. You can also tell it to change prices, timings or rules, or send a photo of a new menu."
        }
        height="h-[620px]"
      />
    </PhoneColumn>
  );
}
