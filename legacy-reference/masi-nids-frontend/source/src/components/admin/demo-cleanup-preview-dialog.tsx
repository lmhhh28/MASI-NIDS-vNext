"use client";

import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/lib/i18n";
import type { DemoTrafficCleanupPreview } from "@/types/api";

function rowCountLabel(name: string, t: ReturnType<typeof useI18n>["t"]): string {
  const labels = {
    events: "admin.demo.rowEvents",
    alert_groups: "admin.demo.rowAlertGroups",
    workflows: "admin.demo.rowWorkflows",
    source_runs: "admin.demo.rowSourceRuns",
    directional_evidence: "admin.demo.rowDirectionalEvidence",
    raw_digest_samples: "admin.demo.rowRawDigestSamples",
    raw_digest_index_states: "admin.demo.rowRawDigestIndexes",
    raw_digest_chunks: "admin.demo.rowRawDigestChunks",
    retention_holds: "admin.demo.rowRetentionHolds",
    event_ingest_cursors: "admin.demo.rowIngestCursors",
    event_ingest_gaps: "admin.demo.rowIngestGaps",
    online_event_identities: "admin.demo.rowEventIdentities",
    online_prediction_events_v2: "admin.demo.rowPredictionEventsV2",
    directional_evidence_sample_refs: "admin.demo.rowEvidenceSampleRefs",
  } as const;
  const key = labels[name as keyof typeof labels];
  return key ? t(key) : name;
}

export function DemoCleanupPreviewDialog({
  action,
  preview,
  onlineRunId,
  confirmation,
  pending,
  runtimeUnknown,
  onConfirmationChange,
  onCancel,
  onConfirm,
}: {
  action: "run" | "cleanup";
  preview: DemoTrafficCleanupPreview;
  onlineRunId: string;
  confirmation: string;
  pending: boolean;
  runtimeUnknown: boolean;
  onConfirmationChange: (value: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useI18n();

  return (
    <Dialog open onOpenChange={(open) => !open && !pending && onCancel()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("admin.demo.previewTitle")}</DialogTitle>
          <DialogDescription>{t("admin.demo.previewDescription")}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="rounded-md border border-primary/20 bg-primary/5 p-3 text-sm">
            <p className="font-medium">
              {t("admin.demo.previewEventScope", {
                scoped: preview.row_counts.events ?? 0,
                global: preview.global_event_count_before,
              })}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("admin.demo.previewV3Unaffected")}
            </p>
          </div>
          <div className="grid gap-2 rounded-md border p-3 text-sm sm:grid-cols-2">
            {Object.entries(preview.row_counts).map(([name, count]) => (
              <p key={name}>
                <span className="text-muted-foreground">{rowCountLabel(name, t)}</span>
                <br />
                <span className="font-mono">{count}</span>
              </p>
            ))}
          </div>
          <details className="rounded-md border p-3">
            <summary className="cursor-pointer text-sm font-medium">
              {t("admin.demo.scopedIds")}
            </summary>
            <pre className="mt-2 max-h-48 overflow-auto text-xs">
              {JSON.stringify(preview.scoped_ids, null, 2)}
            </pre>
          </details>
          <details className="rounded-md border p-3">
            <summary className="cursor-pointer text-sm font-medium">
              {t("admin.demo.paths")}
            </summary>
            <pre className="mt-2 max-h-48 overflow-auto text-xs">
              {JSON.stringify(preview.paths, null, 2)}
            </pre>
          </details>
          <div className="space-y-2">
            <Label htmlFor="demo-confirm-run-id">
              {t("admin.demo.typeRunId", { id: onlineRunId })}
            </Label>
            <Input
              id="demo-confirm-run-id"
              className="font-mono"
              value={confirmation}
              onChange={(event) => onConfirmationChange(event.currentTarget.value)}
              autoComplete="off"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={pending} onClick={onCancel}>
            {t("common.cancel")}
          </Button>
          <Button
            variant={action === "cleanup" ? "destructive" : "default"}
            disabled={confirmation !== onlineRunId || pending || runtimeUnknown}
            onClick={onConfirm}
          >
            {pending ? (
              <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : null}
            {action === "cleanup" ? t("admin.demo.cleanup") : t("admin.demo.start")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
