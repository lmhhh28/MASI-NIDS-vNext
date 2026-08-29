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
import { useCreateEventSource } from "@/hooks/use-events";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

export function CreateSourceDialog({
  isAdmin,
  onClose,
}: {
  isAdmin: boolean;
  onClose: () => void;
}) {
  const { t, locale } = useI18n();
  const createMutation = useCreateEventSource();
  const runtimeSafety = useRuntimeSafety("admin");
  const [form, setForm] = useState({
    name: "",
    endpoint: "http://127.0.0.1:8088",
    mode: "poll" as "poll" | "sse",
    loopback_only: true,
  });

  function submit() {
    if (!runtimeSafety.guard()) return;
    createMutation.mutate(
      {
        name: form.name.trim(),
        endpoint: form.endpoint.trim(),
        mode: form.mode,
        loopback_only: form.loopback_only,
      },
      {
        onSuccess: () => {
          toast.success(t("dashboard.sourceRegistered"));
          onClose();
        },
        onError: (error) =>
          toast.error(t("common.failed"), {
            description: apiErrorMessage(error, locale),
          }),
      },
    );
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !createMutation.isPending && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("dashboard.registerDemoSource")}</DialogTitle>
          <DialogDescription>{t("sources.editDescription")}</DialogDescription>
        </DialogHeader>
        <div className="mt-2 space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="source-create-name">{t("common.name")}</Label>
            <Input
              id="source-create-name"
              placeholder="demo-online"
              value={form.name}
              onChange={(event) =>
                setForm((current) => ({ ...current, name: event.currentTarget.value }))
              }
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="source-create-endpoint">{t("dashboard.endpoint")}</Label>
            <Input
              id="source-create-endpoint"
              className="font-mono"
              value={form.endpoint}
              onChange={(event) =>
                setForm((current) => ({ ...current, endpoint: event.currentTarget.value }))
              }
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="source-create-mode">{t("dashboard.mode")}</Label>
              <select
                id="source-create-mode"
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
              <Label htmlFor="source-create-loopback">{t("sources.loopbackOnly")}</Label>
              <div className="flex min-h-9 items-center justify-between rounded-md border px-3">
                <input
                  id="source-create-loopback"
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
        <DialogFooter>
          <Button variant="outline" disabled={createMutation.isPending} onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            disabled={
              !runtimeSafety.allowed || createMutation.isPending || !form.name.trim() || !form.endpoint.trim()
            }
            onClick={submit}
          >
            {createMutation.isPending ? (
              <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : null}
            {t("common.create")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
