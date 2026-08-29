"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import { Plus, Wifi, WifiOff, Pencil, Lock, Loader2, Network } from "lucide-react";
import { toast } from "sonner";

import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { P4Switch } from "@/types/api";
import {
  useSwitches,
  useConnectSwitch,
  useConnectWriteMaster,
} from "@/hooks/use-p4";
import { useAuthStore } from "@/lib/auth";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import Link from "next/link";

const RegisterSwitchDialog = dynamic(
  () => import("@/components/p4/register-switch-dialog").then((module) => module.RegisterSwitchDialog),
);
const SwitchActions = dynamic(() =>
  import("@/components/p4/switch-actions").then((module) => module.SwitchActions),
);

function SwitchCard({ sw }: { sw: P4Switch }) {
  const isAdmin = useAuthStore((s) => s.isAdmin)();
  const connectMut = useConnectSwitch();
  const writeMasterMut = useConnectWriteMaster();
  const { t, locale } = useI18n();
  const runtimeSafety = useRuntimeSafety("p4");
  const p4Maintenance = !runtimeSafety.allowed;

  const connected = sw.read_state === "connected";
  const canWriteMaster =
    isAdmin && sw.pipeline_owner === "p4_agent" && !sw.writes_enabled;

  return (
    <Card className="border-border bg-card hover:border-primary/20 transition-colors">
      <CardContent className="p-5">
        <div className="flex min-w-0 items-start justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <div className="relative shrink-0">
              {connected ? (
                <Wifi className="h-5 w-5 text-success" />
              ) : (
                <WifiOff className="h-5 w-5 text-muted-foreground" />
              )}
              {connected && (
                <span className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-success animate-pulse motion-reduce:animate-none" />
              )}
            </div>
            <div className="min-w-0">
              <Link
                href={`/p4/${sw.id}`}
                className="block truncate text-sm font-mono font-semibold hover:text-primary transition-colors"
              >
                {sw.name}
              </Link>
              <p className="truncate text-xs text-muted-foreground font-mono">
                {sw.grpc_addr} · {t("p4.deviceId")} {sw.device_id}
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Badge variant={sw.pipeline_owner === "p4_agent" ? "default" : "secondary"} className="text-xs">
              {sw.pipeline_owner}
            </Badge>
            {sw.writes_enabled ? (
              <Pencil className="h-3.5 w-3.5 text-primary" />
            ) : (
              <Lock className="h-3.5 w-3.5 text-muted-foreground" />
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 mt-4">
          {!connected && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs"
              disabled={p4Maintenance || connectMut.isPending}
              onClick={() => {
                if (!runtimeSafety.guard()) return;
                connectMut.mutate(sw.id, {
                  onSuccess: () => toast.success(t("p4.connected")),
                  onError: (e) =>
                    toast.error(t("p4.connectFailed"), {
                      description: apiErrorMessage(e, locale),
                    }),
                });
              }}
            >
              {connectMut.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" />}
              {t("p4.connect")}
            </Button>
          )}
          {canWriteMaster && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs border-primary/30 text-primary hover:bg-primary/10"
              disabled={p4Maintenance || writeMasterMut.isPending}
              onClick={() => {
                if (!runtimeSafety.guard()) return;
                writeMasterMut.mutate(sw, {
                  onSuccess: () => toast.success(t("p4.writeMasterEnabled")),
                  onError: (e) =>
                    toast.error(t("p4.writeMasterFailed"), {
                      description: apiErrorMessage(e, locale),
                    }),
                });
              }}
            >
              {writeMasterMut.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" />}
              {t("p4.enableWrites")}
            </Button>
          )}
          <Link href={`/p4/${sw.id}`} className={buttonVariants({ size: "sm", variant: "ghost", className: "h-7 text-xs" })}>
            {t("p4.tables")} →
          </Link>
          <div className="ml-auto">
            <SwitchActions sw={sw} />
          </div>
        </div>

        {sw.p4info_hash && (
          <p className="text-xs text-muted-foreground font-mono mt-3 truncate">
            p4info: {sw.p4info_hash}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export default function P4Page() {
  const switchesQ = useSwitches();
  const [dialogOpen, setDialogOpen] = useState(false);
  const isAdmin = useAuthStore((s) => s.isAdmin)();
  const { t } = useI18n();
  const runtimeSafety = useRuntimeSafety("p4");
  const runtimeQ = runtimeSafety.runtime;
  const p4Maintenance = !runtimeSafety.allowed;
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-heading font-semibold">
            {t("p4.title")}
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            {t("p4.subtitle")}
          </p>
        </div>
        <Button
          size="sm"
          className="h-8"
          disabled={p4Maintenance}
          onClick={() => { if (runtimeSafety.guard()) setDialogOpen(true); }}
        >
          <Plus className="mr-1.5 h-3.5 w-3.5" />
          {t("p4.registerSwitch")}
        </Button>
        {dialogOpen ? (
          <RegisterSwitchDialog
            isAdmin={isAdmin}
            maintenance={p4Maintenance}
            onClose={() => setDialogOpen(false)}
          />
        ) : null}
      </div>

      {runtimeQ.isError ? (
        <div role="alert" className="rounded-lg border border-warning/35 bg-warning/10 p-4 text-sm text-warning">
          {t("operations.runtimeUnknown")}
        </div>
      ) : null}

      {switchesQ.isLoading ? (
        <div className="grid gap-4 md:grid-cols-2">
          {[1, 2].map((i) => (
            <Skeleton key={i} className="h-36 rounded-xl" />
          ))}
        </div>
      ) : !switchesQ.data || switchesQ.data.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center">
          <Network className="h-10 w-10 text-muted-foreground/40 mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">{t("switches.empty")}</p>
          <p className="text-xs text-muted-foreground mt-1">
            {t("p4.emptySwitchesHint")}
          </p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {switchesQ.data.map((sw) => (
            <SwitchCard key={sw.id} sw={sw} />
          ))}
        </div>
      )}
    </div>
  );
}
