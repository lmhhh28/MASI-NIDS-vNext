"use client";

import { Activity, AlertTriangle, CircleHelp, Shield, GitBranch } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n, type TranslationKey } from "@/lib/i18n";

interface StatsCardsProps {
  totalEvents: number | undefined;
  anomalyEvents: number | undefined;
  unknownEvents: number | undefined;
  activeAlerts: number | undefined;
  activeWorkflows: number | undefined;
  eventStatsLoading: boolean;
  workflowStatsLoading: boolean;
}

const cards = [
  {
    titleKey: "stats.totalEvents",
    icon: Activity,
    key: "totalEvents" as const,
    color: "text-chart-2",
    bgColor: "bg-chart-2/10",
  },
  {
    titleKey: "stats.anomalyEvents",
    icon: AlertTriangle,
    key: "anomalyEvents" as const,
    color: "text-destructive",
    bgColor: "bg-destructive/10",
  },
  {
    titleKey: "stats.unknownEvents",
    icon: CircleHelp,
    key: "unknownEvents" as const,
    color: "text-warning",
    bgColor: "bg-warning/10",
  },
  {
    titleKey: "stats.activeAlerts",
    icon: Shield,
    key: "activeAlerts" as const,
    color: "text-warning",
    bgColor: "bg-warning/10",
  },
  {
    titleKey: "stats.workflows",
    icon: GitBranch,
    key: "activeWorkflows" as const,
    color: "text-chart-5",
    bgColor: "bg-chart-5/10",
  },
] satisfies {
  titleKey: TranslationKey;
  icon: typeof Activity;
  key: keyof Pick<
    StatsCardsProps,
    "totalEvents" | "anomalyEvents" | "unknownEvents" | "activeAlerts" | "activeWorkflows"
  >;
  color: string;
  bgColor: string;
}[];

export function StatsCards({
  totalEvents,
  anomalyEvents,
  unknownEvents,
  activeAlerts,
  activeWorkflows,
  eventStatsLoading,
  workflowStatsLoading,
}: StatsCardsProps) {
  const values = { totalEvents, anomalyEvents, unknownEvents, activeAlerts, activeWorkflows };
  const { t, formatNumber } = useI18n();

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
      {cards.map((card) => (
        <Card key={card.key} className="border-border bg-card">
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              {t(card.titleKey)}
            </CardTitle>
            <div
              className={`flex h-8 w-8 items-center justify-center rounded-lg ${card.bgColor}`}
            >
              <card.icon className={`h-4 w-4 ${card.color}`} />
            </div>
          </CardHeader>
          <CardContent>
            {(card.key === "activeWorkflows" ? workflowStatsLoading : eventStatsLoading) ? (
              <Skeleton className="h-8 w-20" />
            ) : (
              <div className={`text-2xl font-heading font-bold ${card.color}`}>
                {formatNumber(values[card.key])}
              </div>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
