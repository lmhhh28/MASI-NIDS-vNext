"use client";

import { ErrorState } from "@/components/async-state";

export default function DashboardError({ error, reset }: { error: Error; reset: () => void }) {
  return <ErrorState error={error} title="控制台数据加载失败" onRetry={reset} />;
}
