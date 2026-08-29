"use client";

import Link from "next/link";
import { Download, FileCheck2, FileSearch, GitBranchPlus, Loader2, ShieldAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useI18n, type I18nContextValue } from "@/lib/i18n";
import { isKnownAttackAnomaly } from "@/lib/nids-events";
import type { NidsEvent } from "@/types/api";

interface EventTimelineProps {
  events: NidsEvent[] | undefined;
  isLoading: boolean;
  onIngest: () => void;
  isIngesting: boolean;
  canIngest?: boolean;
  ingestDisabledReason?: string;
  onResolveEvidence?: (event: NidsEvent) => void;
  onStartInspectWorkflow?: (event: NidsEvent) => void;
  onStartBlockWorkflow?: (event: NidsEvent) => void;
  pendingEventAction?: string | null;
}

function decisionBadge(decision: string | null, t: I18nContextValue["t"]) {
  switch (decision) {
    case "anomaly":
      return (
        <Badge variant="destructive" className="text-xs font-mono">
          {t("events.decisionAnomaly")}
        </Badge>
      );
    case "normal":
      return (
        <Badge
        className="text-xs font-mono bg-primary/15 text-primary border-primary/20 hover:bg-primary/20"
      >
          {t("events.decisionNormal")}
        </Badge>
      );
    case "unknown":
      return (
        <Badge className="border-warning/30 bg-warning/10 text-xs font-mono text-warning hover:bg-warning/15">
          {t("events.decisionUnknown")}
        </Badge>
      );
    default:
      return (
        <Badge variant="outline" className="text-xs font-mono">
          {decision ?? "—"}
        </Badge>
      );
  }
}

export function EventTimeline({
  events,
  isLoading,
  onIngest,
  isIngesting,
  canIngest = true,
  ingestDisabledReason,
  onResolveEvidence,
  onStartInspectWorkflow,
  onStartBlockWorkflow,
  pendingEventAction,
}: EventTimelineProps) {
  const disabled = isIngesting || !canIngest;
  const eventActionPending = pendingEventAction != null;
  const { t, formatTime } = useI18n();
  return (
    <Card className="border-border bg-card">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {t("events.recent")}
        </CardTitle>
        <Button
          size="sm"
          variant="outline"
          onClick={onIngest}
          disabled={disabled}
          className="h-7 text-xs"
        >
          {isIngesting ? (
            <Loader2 className="mr-1.5 h-3 w-3 animate-spin motion-reduce:animate-none" />
          ) : (
            <Download className="mr-1.5 h-3 w-3" />
          )}
          {t("events.ingestNow")}
        </Button>
      </CardHeader>
      <CardContent>
        {!canIngest && ingestDisabledReason && (
          <div className="mb-3 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
            {ingestDisabledReason}
          </div>
        )}
        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3, 4, 5].map((i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : !events || events.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 text-muted-foreground">
            <p className="text-sm">{t("events.empty")}</p>
            <p className="text-xs mt-1">
              {t("events.emptyHint")}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="text-xs">{t("common.time")}</TableHead>
                  <TableHead className="text-xs">{t("events.flow")}</TableHead>
                  <TableHead className="text-xs">{t("events.class")}</TableHead>
                  <TableHead className="text-xs text-right">{t("events.score")}</TableHead>
                  <TableHead className="text-xs text-center">{t("events.decision")}</TableHead>
                  <TableHead className="text-xs text-right">{t("common.actions")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.slice(0, 20).map((ev) => {
                  const isKnownAttack = isKnownAttackAnomaly(ev);
                  const canInspect = isKnownAttack || ev.decision === "unknown" || ev.decision === "anomaly";
                  const hasResolvedEvidence =
                    ev.directional_evidence_status === "resolved" ||
                    ev.directional_evidence_status === "reviewed";
                  return (
                    <TableRow
                      key={ev.id}
                      className="border-border hover:bg-secondary/30 transition-colors"
                    >
                      <TableCell className="text-xs font-mono text-muted-foreground whitespace-nowrap">
                        {ev.ingested_at
                          ? formatTime(ev.ingested_at, {
                              hour: "2-digit",
                              minute: "2-digit",
                              second: "2-digit",
                            })
                          : "—"}
                      </TableCell>
                      <TableCell className="max-w-[200px] truncate text-xs font-mono">
                        {ev.flow_id ?? `${ev.src_ip ?? "?"}→${ev.dst_ip ?? "?"}`}
                      </TableCell>
                      <TableCell className="max-w-[120px] truncate text-xs font-mono">
                        {ev.nearest_class ?? "—"}
                      </TableCell>
                      <TableCell className="text-xs font-mono text-right tabular-nums">
                        {ev.legacy_score != null
                          ? ev.legacy_score.toFixed(4)
                          : "—"}
                      </TableCell>
                      <TableCell className="text-center">
                        {decisionBadge(ev.decision, t)}
                      </TableCell>
                      <TableCell className="min-w-[16rem]">
                        {canInspect ? (
                          <div className="flex flex-wrap justify-end gap-1.5">
                            {isKnownAttack ? (
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-9 min-w-[5.5rem] text-xs"
                                disabled={eventActionPending || hasResolvedEvidence}
                                onClick={() => onResolveEvidence?.(ev)}
                              >
                                {pendingEventAction === `evidence:${ev.id}` ? (
                                  <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" />
                                ) : hasResolvedEvidence ? (
                                  <FileCheck2 className="mr-1 h-3 w-3" />
                                ) : (
                                  <FileSearch className="mr-1 h-3 w-3" />
                                )}
                                {hasResolvedEvidence ? t("events.evidenceAlreadyResolved") : t("events.resolveEvidence")}
                              </Button>
                            ) : null}
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-9 flex-col items-start gap-0 py-1 text-xs leading-tight"
                              disabled={eventActionPending}
                              onClick={() => onStartInspectWorkflow?.(ev)}
                            >
                              <span className="flex items-center gap-1">
                                {pendingEventAction === `inspect:${ev.id}` ? (
                                  <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" />
                                ) : (
                                  <GitBranchPlus className="h-3 w-3" />
                                )}
                                <span>{t("events.generateReport")}</span>
                              </span>
                              <span className="font-mono text-xs text-muted-foreground/80">
                                · {t("events.actionInspectSubtitle")}
                              </span>
                            </Button>
                            {isKnownAttack ? (
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-9 flex-col items-start gap-0 py-1 text-xs leading-tight border-warning/40 text-warning hover:text-warning"
                                disabled={eventActionPending}
                                onClick={() => onStartBlockWorkflow?.(ev)}
                              >
                                <span className="flex items-center gap-1">
                                  {pendingEventAction === `block:${ev.id}` ? (
                                    <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" />
                                  ) : (
                                    <ShieldAlert className="h-3 w-3" />
                                  )}
                                  <span>{t("events.createBlockWorkflow")}</span>
                                </span>
                                <span className="font-mono text-xs text-muted-foreground/80">
                                  · {t("events.actionBlockSubtitle")}
                                </span>
                              </Button>
                            ) : null}
                            <Link href="/workflows" className={buttonVariants({ size: "sm", variant: "ghost", className: "h-9 text-xs" })}>
                              {t("events.viewReview")}
                            </Link>
                          </div>
                        ) : (
                          <span className="block text-right text-xs text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
