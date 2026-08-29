"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import { Loader2, Play, RefreshCw, Square, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DemoOperationActivity } from "@/components/admin/demo-operation-activity";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  useCleanupDemoTraffic,
  useDemoTrafficStatus,
  usePreviewDemoCleanup,
  useRunDemoTraffic,
  useStopDemoTraffic,
} from "@/hooks/use-demo-traffic";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { apiErrorMessage } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import {
  actorDemoOperations,
  isDemoOperationActive,
  useDemoOperationRegistry,
} from "@/lib/demo-operations";
import { createDemoOnlineRunId } from "@/lib/demo-run-id";
import { useI18n } from "@/lib/i18n";
import type { DemoTrafficCleanupPreviewRequest, DemoTrafficRunRequest } from "@/types/api";

type Profile = DemoTrafficRunRequest["profile"];

const DemoCleanupPreviewDialog = dynamic(() =>
  import("@/components/admin/demo-cleanup-preview-dialog").then(
    (module) => module.DemoCleanupPreviewDialog,
  ),
);

const defaultTrainRunId = process.env.NEXT_PUBLIC_MASI_DEFAULT_TRAIN_RUN_ID?.trim() || "demo-train";
const defaultOnlineRunId = process.env.NEXT_PUBLIC_MASI_DEFAULT_ONLINE_RUN_ID?.trim() || "online-demo";

const defaultForm: DemoTrafficRunRequest = {
  duration_seconds: 60,
  profile: "all",
  online_run_id: defaultOnlineRunId,
  train_run_id: defaultTrainRunId,
};

export default function AdminDemoTrafficPage() {
  const { t, locale } = useI18n();
  const statusQ = useDemoTrafficStatus();
  const runMut = useRunDemoTraffic();
  const stopMut = useStopDemoTraffic();
  const cleanupMut = useCleanupDemoTraffic();
  const previewMut = usePreviewDemoCleanup();
  const runtimeSafety = useRuntimeSafety("admin");
  const actorUserId = useAuthStore((state) => state.user?.id ?? null);
  const trackedOperations = useDemoOperationRegistry((state) => state.operations);
  const [form, setForm] = useState<DemoTrafficRunRequest>(defaultForm);
  const [durationDraft, setDurationDraft] = useState(String(defaultForm.duration_seconds));
  const [previewAction, setPreviewAction] = useState<"cleanup" | null>(null);
  const [confirmRunId, setConfirmRunId] = useState("");
  const [resetBackendState, setResetBackendState] = useState(true);

  const durationNumber = Number(durationDraft);
  const durationValid = Number.isInteger(durationNumber) && durationNumber >= 5 && durationNumber <= 300;
  const status = statusQ.data;
  const onlineHealth = status?.online_health ?? {};
  const lastEventId = typeof onlineHealth.last_event_id === "number" ? onlineHealth.last_event_id : null;
  const runtimeUnknown = !runtimeSafety.allowed;
  const actorOperations = useMemo(
    () => actorDemoOperations(trackedOperations, actorUserId),
    [actorUserId, trackedOperations],
  );
  const activeRun = actorOperations.some(
    (operation) => operation.kind === "run" && isDemoOperationActive(operation),
  );
  const activeStop = actorOperations.some(
    (operation) => operation.kind === "stop" && isDemoOperationActive(operation),
  );
  const activeCleanup = actorOperations.some(
    (operation) => operation.kind === "cleanup" && isDemoOperationActive(operation),
  );

  useEffect(() => {
    // Generate after hydration so the random run ID does not mismatch server markup.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setForm((previous) => ({
      ...previous,
      online_run_id: createDemoOnlineRunId(defaultOnlineRunId),
    }));
  }, []);

  function payload(): DemoTrafficRunRequest {
    return { ...form, duration_seconds: durationNumber };
  }

  function cleanupPreviewPayload(): DemoTrafficCleanupPreviewRequest {
    return {
      online_run_id: form.online_run_id,
      reset_backend_state: resetBackendState,
    };
  }

  function executeRun() {
    if (!runtimeSafety.guard()) return;
    const runPayload = payload();
    runMut.mutate({ payload: runPayload, onlineRunId: runPayload.online_run_id }, {
      onSuccess: (data) => {
        toast.info(t("admin.demo.operationSubmitted"), {
          description: `${data.operation.operation_id} · ${data.operation.status}`,
        });
      },
      onError: (error) => {
        toast.error(t("admin.demo.operationFailed"), { description: apiErrorMessage(error, locale) });
      },
    });
  }

  function refreshOnlineRunId() {
    setForm((previous) => ({
      ...previous,
      online_run_id: createDemoOnlineRunId(defaultOnlineRunId),
    }));
  }

  function requestPreview() {
    if (!runtimeSafety.guard()) return;
    previewMut.mutate(cleanupPreviewPayload(), {
      onSuccess: () => {
        setConfirmRunId("");
        setPreviewAction("cleanup");
      },
      onError: (error) => toast.error(t("admin.demo.previewFailed"), { description: apiErrorMessage(error, locale) }),
    });
  }

  function runTraffic() {
    if (!durationValid) {
      toast.error(t("admin.demo.durationError"));
      return;
    }
    executeRun();
  }

  function stopTraffic() {
    if (!runtimeSafety.guard()) return;
    const activeOnlineRunId = status?.active_online_run_id ?? form.online_run_id;
    stopMut.mutate({
      payload: { online_run_id: activeOnlineRunId },
      onlineRunId: activeOnlineRunId,
    }, {
      onSuccess: (data) => toast.info(t("admin.demo.operationSubmitted"), {
        description: `${data.operation.operation_id} · ${data.operation.status}`,
      }),
    });
  }

  function cleanupTraffic() {
    requestPreview();
  }

  function executeCleanup() {
    if (!runtimeSafety.guard()) return;
    const preview = previewMut.data;
    if (!preview) return;
    cleanupMut.mutate({
      payload: { preview_id: preview.preview_id, scope_hash: preview.scope_hash },
      onlineRunId: preview.online_run_id,
    }, {
      onSuccess: (data) => {
        setPreviewAction(null);
        toast.info(t("admin.demo.operationSubmitted"), {
          description: `${data.operation.operation_id} · ${data.operation.status}`,
        });
      },
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold">{t("admin.demo.title")}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t("admin.demo.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" className="min-h-11 sm:min-h-9" onClick={() => statusQ.refetch()} disabled={statusQ.isFetching}>
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${statusQ.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`} />
          {t("common.refresh")}
        </Button>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
        <Card>
          <CardHeader>
            <CardTitle>{t("admin.demo.runTitle")}</CardTitle>
            <CardDescription>{t("admin.demo.runDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="demo-duration" className="text-xs">{t("admin.demo.duration")}</Label>
              <Input
                id="demo-duration"
                className="min-h-11 text-base sm:min-h-9 sm:text-xs"
                inputMode="numeric"
                value={durationDraft}
                aria-invalid={!durationValid}
                onChange={(event) => setDurationDraft(event.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="demo-profile" className="text-xs">{t("admin.demo.profile")}</Label>
              <select
                id="demo-profile"
                value={form.profile}
                onChange={(event) =>
                  setForm((previous) => ({
                    ...previous,
                    profile: event.currentTarget.value as Profile,
                  }))
                }
                className="min-h-11 w-full rounded-md border border-input bg-background px-3 text-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-9 sm:text-sm"
              >
                <option value="all">all</option>
                <option value="normal">normal</option>
                <option value="syn_flood">syn_flood</option>
                <option value="udp_flood">udp_flood</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="demo-train-run" className="text-xs">{t("admin.demo.trainRun")}</Label>
              <Input id="demo-train-run" className="min-h-11 text-base font-mono sm:min-h-9 sm:text-xs" value={form.train_run_id} onChange={(event) => setForm((prev) => ({ ...prev, train_run_id: event.target.value }))} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="demo-online-run" className="text-xs">{t("admin.demo.onlineRun")}</Label>
              <div className="flex gap-2">
                <Input id="demo-online-run" className="min-h-11 min-w-0 flex-1 text-base font-mono sm:min-h-9 sm:text-xs" value={form.online_run_id} onChange={(event) => setForm((prev) => ({ ...prev, online_run_id: event.target.value }))} />
                <Button type="button" variant="outline" size="icon" className="min-h-11 min-w-11 shrink-0 sm:min-h-9 sm:min-w-9" onClick={refreshOnlineRunId} title={t("admin.demo.generateRunId")} aria-label={t("admin.demo.generateRunId")}>
                  <RefreshCw className="h-4 w-4" />
                </Button>
              </div>
            </div>
            <div className="flex min-h-11 items-center justify-between rounded-md border border-border px-3 py-2">
              <Label htmlFor="demo-reset-backend" className="text-xs">{t("admin.demo.resetBackendState")}</Label>
              <input id="demo-reset-backend" type="checkbox" checked={resetBackendState} onChange={(event) => setResetBackendState(event.currentTarget.checked)} className="h-5 w-5 accent-primary" />
            </div>
            <div className="flex flex-wrap gap-2 sm:col-span-2">
              <Button size="sm" className="min-h-11 sm:min-h-9" onClick={runTraffic} disabled={runtimeUnknown || !status?.available || status?.running || activeRun || activeStop || activeCleanup || runMut.isPending || previewMut.isPending || !durationValid}>
                {runMut.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" /> : <Play className="mr-1.5 h-3.5 w-3.5" />}
                {t("admin.demo.start")}
              </Button>
              <Button size="sm" className="min-h-11 sm:min-h-9" variant="outline" onClick={stopTraffic} disabled={runtimeUnknown || !status?.running || activeStop || stopMut.isPending}>
                {stopMut.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" /> : <Square className="mr-1.5 h-3.5 w-3.5" />}
                {t("admin.demo.stop")}
              </Button>
              <Button size="sm" className="min-h-11 sm:min-h-9" variant="destructive" onClick={cleanupTraffic} disabled={runtimeUnknown || status?.running || activeRun || activeStop || activeCleanup || cleanupMut.isPending || previewMut.isPending}>
                {cleanupMut.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" /> : <Trash2 className="mr-1.5 h-3.5 w-3.5" />}
                {t("admin.demo.cleanup")}
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("admin.demo.statusTitle")}</CardTitle>
            <CardDescription>{t("admin.demo.statusDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-muted-foreground">{t("common.status")}</span>
              <Badge variant={status?.running ? "default" : status?.available ? "secondary" : "destructive"}>
                {status?.running ? t("admin.demo.running") : status?.available ? t("admin.demo.ready") : t("admin.demo.unavailable")}
              </Badge>
            </div>
            <div className="grid gap-2 text-xs">
              <div className="flex justify-between gap-4"><span className="text-muted-foreground">{t("admin.demo.container")}</span><span className="truncate font-mono">{status?.active_operation_id ?? "—"}</span></div>
              <div className="flex justify-between gap-4"><span className="text-muted-foreground">{t("admin.demo.state")}</span><span className="font-mono">{status?.runtime_state ?? "—"}</span></div>
              <div className="flex justify-between gap-4"><span className="text-muted-foreground">{t("admin.demo.lastEventId")}</span><span className="font-mono">{lastEventId ?? "—"}</span></div>
            </div>
            {status?.detail && (
              <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">
                {status.detail}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
      <DemoOperationActivity />
      {previewAction && previewMut.data ? (
        <DemoCleanupPreviewDialog
          action={previewAction}
          preview={previewMut.data}
          onlineRunId={form.online_run_id}
          confirmation={confirmRunId}
          pending={runMut.isPending || cleanupMut.isPending}
          runtimeUnknown={runtimeUnknown}
          onConfirmationChange={setConfirmRunId}
          onCancel={() => setPreviewAction(null)}
          onConfirm={executeCleanup}
        />
      ) : null}
    </div>
  );
}
