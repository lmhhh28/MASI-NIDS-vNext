"use client";

import { Lightbulb, AlertCircle, HelpCircle, Ban, Sparkles } from "lucide-react";

import { formatEvidenceFact, formatReviewFreeText } from "@/lib/workflow-facts";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";

interface ReviewPacketShape {
  review_packet_id?: string;
  packet_hash?: string | null;
  artifact_id?: string;
  plan_revision?: number | string | null;
  risk_policy_version?: number | string | null;
  p4info_hash?: string | null;
  table_schema_hash?: string | null;
  known_facts?: string[];
  assumptions?: string[];
  unknowns?: string[];
  recommended_action?: string | null;
  blocked?: boolean;
  requires_human?: boolean;
}

interface ReviewPacketCardProps {
  packet: ReviewPacketShape | null | undefined;
}

export function ReviewPacketCard({ packet }: ReviewPacketCardProps) {
  const { t, locale } = useI18n();
  if (!packet) {
    return (
      <div className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">
        —
      </div>
    );
  }
  const facts = packet.known_facts ?? [];
  const assumptions = packet.assumptions ?? [];
  const unknowns = packet.unknowns ?? [];
  const recommended = packet.recommended_action ?? "—";
  const blocked = !!packet.blocked;

  return (
    <div className="min-w-0 rounded-xl border border-border bg-card p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Sparkles className="h-3.5 w-3.5 text-primary" aria-hidden />
        <span className="text-xs font-medium">{t("workflow.review.recommended")}</span>
        <Badge
          variant={blocked ? "destructive" : "default"}
          className="h-auto max-w-full whitespace-normal py-1 text-xs leading-snug"
        >
          {recommended}
        </Badge>
        {blocked && (
          <Badge variant="destructive" className="text-xs">
            <Ban className="mr-1 h-3 w-3" aria-hidden />
            {t("workflow.review.blocked")}
          </Badge>
        )}
      </div>

      <div className="mb-3 flex min-w-0 flex-wrap gap-1.5">
        <ReviewBadge label="packet" value={packet.review_packet_id} />
        <ReviewBadge label="hash" value={packet.packet_hash} />
        <ReviewBadge label="plan rev" value={packet.plan_revision} />
        <ReviewBadge label="risk" value={packet.risk_policy_version != null ? `v${packet.risk_policy_version}` : null} />
        <ReviewBadge label="p4info" value={packet.p4info_hash} />
        <ReviewBadge label="schema" value={packet.table_schema_hash} />
      </div>

      <div className="grid min-w-0 gap-3 md:grid-cols-3">
        <ReviewColumn
          icon={<Lightbulb className="h-3.5 w-3.5 text-primary" aria-hidden />}
          title={t("workflow.review.knownFacts")}
          items={facts}
          formatItem={(item) => formatEvidenceFact(item, locale, t)}
          tone="primary"
        />
        <ReviewColumn
          icon={<AlertCircle className="h-3.5 w-3.5 text-warning" aria-hidden />}
          title={t("workflow.review.assumptions")}
          items={assumptions}
          formatItem={(item) => formatReviewFreeText(item, t)}
          tone="warning"
        />
        <ReviewColumn
          icon={<HelpCircle className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />}
          title={t("workflow.review.unknowns")}
          items={unknowns}
          formatItem={(item) => formatReviewFreeText(item, t)}
          tone="muted"
        />
      </div>
    </div>
  );
}

function ReviewBadge({ label, value }: { label: string; value: unknown }) {
  if (value == null || value === "") return null;
  const text = String(value);
  return (
    <Badge
      variant="outline"
      className="h-auto max-w-full justify-start whitespace-normal py-1 text-left text-xs font-mono leading-snug"
      title={`${label}:${text}`}
    >
      <span className="min-w-0 break-all">
        {label}:{text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text}
      </span>
    </Badge>
  );
}

function ReviewColumn({
  icon,
  title,
  items,
  formatItem,
  tone,
}: {
  icon: React.ReactNode;
  title: string;
  items: string[];
  formatItem?: (item: string) => string;
  tone: "primary" | "warning" | "muted";
}) {
  const toneClass =
    tone === "primary"
      ? "border-primary/20 bg-primary/5"
      : tone === "warning"
        ? "border-warning/30 bg-warning/5"
        : "border-border bg-secondary/30";
  return (
    <div className={`min-w-0 rounded-lg border ${toneClass} p-3`}>
      <div className="mb-2 flex items-center gap-1.5">
        {icon}
        <span className="text-xs font-medium">{title}</span>
        <span className="ml-auto text-xs text-muted-foreground">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">—</p>
      ) : (
        <ul className="space-y-1.5">
          {items.map((item, idx) => (
            <li
              key={idx}
              className="break-words text-xs leading-snug"
              title={item}
            >
              {formatItem ? formatItem(item) : item}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
