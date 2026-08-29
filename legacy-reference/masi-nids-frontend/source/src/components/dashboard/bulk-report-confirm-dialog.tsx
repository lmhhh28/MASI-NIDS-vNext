"use client";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useI18n } from "@/lib/i18n";

interface BulkReportConfirmDialogProps {
  selectedCount: number;
  acknowledged: boolean;
  disabled: boolean;
  onAcknowledgedChange: (acknowledged: boolean) => void;
  onCancel: () => void;
  onConfirm: () => void;
}

export function BulkReportConfirmDialog({
  selectedCount,
  acknowledged,
  disabled,
  onAcknowledgedChange,
  onCancel,
  onConfirm,
}: BulkReportConfirmDialogProps) {
  const { t } = useI18n();

  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("events.bulk.report.warning.title")}</DialogTitle>
          <DialogDescription>
            {t("events.bulk.report.warning.description")}
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
          {t("events.bulk.selected", { n: selectedCount })}
        </div>
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm">
          <Checkbox
            checked={acknowledged}
            onCheckedChange={(value) => onAcknowledgedChange(value === true)}
            aria-label={t("events.bulk.report.warning.ack")}
          />
          <span>{t("events.bulk.report.warning.ack")}</span>
        </label>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            {t("common.cancel")}
          </Button>
          <Button disabled={disabled || !acknowledged} onClick={onConfirm}>
            {t("events.bulk.start.report")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
