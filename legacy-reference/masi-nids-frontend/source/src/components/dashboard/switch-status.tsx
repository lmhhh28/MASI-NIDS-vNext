"use client";

import { Wifi, WifiOff, Pencil, Lock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/lib/i18n";
import type { P4Switch } from "@/types/api";

interface SwitchStatusProps {
  switches: P4Switch[] | undefined;
  isLoading: boolean;
}

function ownerBadgeVariant(owner: string) {
  switch (owner) {
    case "p4_agent":
    case "backend_p4_manager":
      return "default";
    case "digest_controller":
      return "secondary";
    default:
      return "outline";
  }
}

export function SwitchStatus({ switches, isLoading }: SwitchStatusProps) {
  const { t } = useI18n();

  if (isLoading) {
    return (
      <Card className="border-border bg-card">
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            {t("switches.title")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {[1, 2].map((i) => (
            <Skeleton key={i} className="h-16 w-full rounded-lg" />
          ))}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-border bg-card">
      <CardHeader>
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {t("switches.title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!switches || switches.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-muted-foreground">
            <WifiOff className="h-8 w-8 mb-2 opacity-40" />
            <p className="text-sm">{t("switches.empty")}</p>
          </div>
        ) : (
          switches.map((sw) => (
            <div
              key={sw.id}
              className="flex items-center justify-between rounded-lg border border-border bg-secondary/30 px-4 py-3 transition-colors hover:bg-secondary/50"
            >
              <div className="flex min-w-0 items-center gap-3">
                {/* Status indicator */}
                <div className="relative shrink-0">
                  {sw.read_state === "connected" ? (
                    <Wifi className="h-4 w-4 text-success" />
                  ) : (
                    <WifiOff className="h-4 w-4 text-muted-foreground" />
                  )}
                  {sw.read_state === "connected" && (
                    <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-success animate-pulse motion-reduce:animate-none" />
                  )}
                </div>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium font-mono">{sw.name}</p>
                  <p className="truncate text-xs text-muted-foreground">{sw.grpc_addr}</p>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Badge variant={ownerBadgeVariant(sw.pipeline_owner)} className="max-w-full text-xs">
                  {sw.pipeline_owner}
                </Badge>
                {sw.writes_enabled ? (
                  <Pencil className="h-3.5 w-3.5 text-primary" />
                ) : (
                  <Lock className="h-3.5 w-3.5 text-muted-foreground" />
                )}
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}
