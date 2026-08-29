"use client";

import { CheckCircle, XCircle, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import { useReviewDirectionalEvidence } from "@/hooks/use-directional-evidence";
import { useAuthStore } from "@/lib/auth";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

interface DirectionalEvidenceReviewActionsProps {
  evidenceId: string;
  /** Hide actions when evidence is already in a final state (e.g. reviewed). */
  status?: string | null;
}

export function DirectionalEvidenceReviewActions({
  evidenceId,
  status,
}: DirectionalEvidenceReviewActionsProps) {
  const { t, locale } = useI18n();
  const isAdmin = useAuthStore((s) => s.isAdmin)();
  const reviewMut = useReviewDirectionalEvidence(evidenceId);
  const runtimeSafety = useRuntimeSafety("workflow");

  if (!isAdmin) {
    return (
      <span className="text-xs text-muted-foreground">
        {t("evidence.review.adminOnly")}
      </span>
    );
  }

  if (status === "reviewed") {
    return null;
  }

  function handle(decision: "approve" | "reject") {
    if (!runtimeSafety.guard()) return;
    reviewMut.mutate(decision, {
      onSuccess: () =>
        toast.success(
          decision === "approve"
            ? t("evidence.review.approved")
            : t("evidence.review.rejected")
        ),
      onError: (err) =>
        toast.error(t("common.failed"), {
          description: apiErrorMessage(err, locale),
        }),
    });
  }

  return (
    <div className="flex items-center gap-1">
      <Button
        size="sm"
        variant="ghost"
        className="h-6 px-2 text-xs cursor-pointer text-primary hover:bg-primary/10 hover:text-primary"
        disabled={!runtimeSafety.allowed || reviewMut.isPending}
        onClick={() => handle("approve")}
        aria-label={t("evidence.review.approve")}
      >
        {reviewMut.isPending ? (
          <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" />
        ) : (
          <CheckCircle className="mr-1 h-3 w-3" aria-hidden />
        )}
        {t("evidence.review.approve")}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className="h-6 px-2 text-xs cursor-pointer text-destructive hover:bg-destructive/10 hover:text-destructive"
        disabled={!runtimeSafety.allowed || reviewMut.isPending}
        onClick={() => handle("reject")}
        aria-label={t("evidence.review.reject")}
      >
        <XCircle className="mr-1 h-3 w-3" aria-hidden />
        {t("evidence.review.reject")}
      </Button>
    </div>
  );
}
