"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

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
import {
  useDeleteEventSource,
  usePatchEventSource,
} from "@/hooks/use-event-sources-admin";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { EventSourceResponse } from "@/types/api";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

export function SourceActionsDialog({
  source,
  action,
  isAdmin,
  onClose,
}: {
  source: EventSourceResponse;
  action: "edit" | "delete";
  isAdmin: boolean;
  onClose: () => void;
}) {
  const { t, locale } = useI18n();
  const runtimeSafety = useRuntimeSafety("admin");
  const [form, setForm] = useState({
    name: source.name,
    endpoint: source.endpoint,
    mode: source.mode as "poll" | "sse",
    loopback_only: source.loopback_only,
  });
  const patchMutation = usePatchEventSource(source.id);
  const deleteMutation = useDeleteEventSource();
  const pending = patchMutation.isPending || deleteMutation.isPending;
  const fieldId = (name: string) => `source-${source.id}-${name}`;

  function save() {
    if (!runtimeSafety.guard()) return;
    patchMutation.mutate(
      {
        name: form.name.trim() || undefined,
        endpoint: form.endpoint.trim() || undefined,
        mode: form.mode,
        loopback_only: form.loopback_only,
      },
      {
        onSuccess: () => {
          toast.success(t("common.saved"));
          onClose();
        },
        onError: (error) =>
          toast.error(t("common.failed"), {
            description: apiErrorMessage(error, locale),
          }),
      },
    );
  }

  function remove() {
    if (!runtimeSafety.guard()) return;
    deleteMutation.mutate(source.id, {
      onSuccess: (data) => {
        toast.success(t("sources.deleted", { count: data.retained_event_count }));
        onClose();
      },
      onError: (error) =>
        toast.error(t("common.failed"), {
          description: apiErrorMessage(error, locale),
        }),
    });
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !pending && onClose()}>
      <DialogContent className={action === "edit" ? "sm:max-w-md" : "sm:max-w-sm"}>
        <DialogHeader>
          <DialogTitle>
            {action === "edit" ? t("sources.editTitle") : t("sources.confirmDelete")}
          </DialogTitle>
          <DialogDescription>
            {action === "edit"
              ? t("sources.editDescription")
              : t("sources.confirmDeleteHint")}
          </DialogDescription>
        </DialogHeader>
        {action === "edit" ? (
          <div className="mt-2 space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor={fieldId("name")}>{t("common.name")}</Label>
              <Input
                id={fieldId("name")}
                value={form.name}
                onChange={(event) =>
                  setForm((current) => ({ ...current, name: event.currentTarget.value }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={fieldId("endpoint")}>{t("dashboard.endpoint")}</Label>
              <Input
                id={fieldId("endpoint")}
                className="font-mono"
                value={form.endpoint}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    endpoint: event.currentTarget.value,
                  }))
                }
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor={fieldId("mode")}>{t("dashboard.mode")}</Label>
                <select
                  id={fieldId("mode")}
                  value={form.mode}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      mode: event.currentTarget.value as "poll" | "sse",
                    }))
                  }
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <option value="poll">poll</option>
                  <option value="sse">sse</option>
                </select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={fieldId("loopback")}>{t("sources.loopbackOnly")}</Label>
                <div className="flex min-h-9 items-center justify-between rounded-md border px-3">
                  <input
                    id={fieldId("loopback")}
                    type="checkbox"
                    checked={form.loopback_only}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        loopback_only: event.currentTarget.checked,
                      }))
                    }
                    className="h-5 w-5 accent-primary"
                  />
                  {!form.loopback_only && !isAdmin ? (
                    <span className="text-xs text-warning">{t("sources.adminOnlyHint")}</span>
                  ) : null}
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="rounded-lg border bg-secondary/30 p-3 font-mono text-xs">
            {source.name} · <span className="text-muted-foreground">{source.endpoint}</span>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={pending} onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            variant={action === "delete" ? "destructive" : "default"}
            disabled={
              !runtimeSafety.allowed || pending ||
              (action === "edit" && (!form.name.trim() || !form.endpoint.trim()))
            }
            onClick={action === "delete" ? remove : save}
          >
            {pending ? (
              <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : null}
            {action === "delete" ? t("admin.llm.delete") : t("common.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
