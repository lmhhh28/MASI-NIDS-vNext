"use client";

import { useWorkflows } from "@/hooks/use-workflows";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import {
  AlertTriangle,
  Clock,
} from "lucide-react";
import Link from "next/link";
import { useI18n } from "@/lib/i18n";

export default function ReviewsPage() {
  const workflowsQ = useWorkflows({ status: "open", limit: 50 });
  const { t, formatDateTime } = useI18n();

  const pendingReviews = workflowsQ.data?.items.filter(
    (w) => w.review_status === "open" || w.status === "needs_human_review"
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t("reviews.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("reviews.subtitle")}
        </p>
      </div>

      {workflowsQ.isLoading ? (
        <LoadingState rows={3} />
      ) : workflowsQ.isError ? (
        <ErrorState error={workflowsQ.error} onRetry={() => workflowsQ.refetch()} />
      ) : !pendingReviews || pendingReviews.length === 0 ? (
        <EmptyState title={t("workflows.noPendingReviews")} description={t("reviews.emptyHint")} />
      ) : (
        <div className="space-y-3">
          {pendingReviews.map((w) => (
            <Link key={w.id} href={`/workflows/${encodeURIComponent(w.id)}?tab=overview`}>
              <Card className="border-border bg-card hover:border-warning/30 transition-colors cursor-pointer">
                <CardContent className="p-4">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <AlertTriangle className="h-5 w-5 shrink-0 text-warning" />
                      <div>
                        <p className="text-sm font-mono font-medium">
                          {w.id.slice(0, 12)}…
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {t("workflows.alert")}: {w.alert_group_id?.slice(0, 8) ?? "—"} ·{" "}
                          {w.created_at
                            ? formatDateTime(w.created_at, {
                                month: "short",
                                day: "numeric",
                                hour: "2-digit",
                                minute: "2-digit",
                              })
                            : "—"}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge variant="secondary" className="text-xs">
                        <Clock className="mr-1 h-3 w-3" />
                        {w.status}
                      </Badge>
                    </div>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
