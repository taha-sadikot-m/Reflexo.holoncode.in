// The two areas with sub-pages: Brain (how the employee thinks) and Quality (prove it works).
import { useQuery } from "@tanstack/react-query";
import { Activity, Bomb, BookOpenCheck, FlaskConical, Gauge, GraduationCap, History, Library, Workflow } from "lucide-react";

import { Section } from "../components/section";
import { api, type OwnerWorkspace, type PlaybookProposal, type PlaybookRuleRow } from "../lib/api";
import { useSession } from "../lib/session";

export function BrainSection() {
  const { business } = useSession();
  const bid = business?.id ?? "";
  // share the pages' queries (same keys), only for the "waiting" count on the Learning tab
  const owner = useQuery({
    queryKey: ["owner", bid],
    queryFn: () => api.get<OwnerWorkspace>("/owner", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 10000,
  });
  const book = useQuery({
    queryKey: ["playbook", bid],
    queryFn: () => api.get<{ rules: PlaybookRuleRow[]; proposals: PlaybookProposal[] }>("/playbook", { business_id: bid }),
    enabled: !!bid,
    refetchInterval: 10000,
  });
  const waiting =
    (owner.data?.questions ?? []).filter((q) => q.status === "open" || q.status === "asked").length +
    (owner.data?.approvals ?? []).filter((a) => a.status === "pending").length;
  const rules = (book.data?.rules ?? []).filter((r) => r.status === "active").length;
  return (
    <Section
      title="Brain"
      description="How your AI employee thinks: what it asked and learned, the rules it follows, the procedures it runs, what it knows, and every change to it."
      tabs={[
        { to: "learning", label: "Learning", icon: GraduationCap, count: waiting },
        { to: "rules", label: rules ? `Rules · ${rules}` : "Rules", icon: BookOpenCheck },
        { to: "procedures", label: "Procedures", icon: Workflow },
        { to: "knowledge", label: "Knowledge", icon: Library },
        { to: "history", label: "History", icon: History },
      ]}
    />
  );
}

export function QualitySection() {
  return (
    <Section
      title="Quality"
      description="Prove it works: simulate customers and the owner, watch every message through the pipeline, break things on purpose, and score the build."
      tabs={[
        { to: "lab", label: "Simulation Lab", icon: FlaskConical },
        { to: "xray", label: "Pipeline X-ray", icon: Activity },
        { to: "chaos", label: "Chaos", icon: Bomb },
        { to: "evals", label: "Evals", icon: Gauge },
      ]}
    />
  );
}
