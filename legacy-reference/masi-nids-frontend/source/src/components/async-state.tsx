"use client";

import { AlertCircle, Inbox, LoaderCircle, RefreshCw, WifiOff } from "lucide-react";
import type { ReactNode } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api-errors";

export function LoadingState({ label = "正在加载…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div className="space-y-3" role="status" aria-label={label}>
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-16 w-full rounded-lg" />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex min-h-40 flex-col items-center justify-center rounded-lg border border-dashed p-6 text-center">
      <Inbox className="mb-3 h-6 w-6 text-muted-foreground" aria-hidden="true" />
      <h2 className="text-base font-semibold">{title}</h2>
      {description ? <p className="mt-1 max-w-prose text-sm text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  error,
  title = "数据加载失败",
  onRetry,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
}) {
  return (
    <Alert variant="destructive" role="alert">
      <AlertCircle aria-hidden="true" />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>{apiErrorMessage(error)}</p>
        {onRetry ? (
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw aria-hidden="true" />
            重试
          </Button>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}

export function StaleState({ children }: { children: ReactNode }) {
  return (
    <Alert>
      <WifiOff aria-hidden="true" />
      <AlertTitle>当前内容可能已过期</AlertTitle>
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  );
}

export function PageLoadingState() {
  return (
    <div className="flex min-h-56 items-center justify-center" role="status">
      <LoaderCircle className="h-6 w-6 animate-spin motion-reduce:animate-none" aria-hidden="true" />
      <span className="ml-2 text-sm text-muted-foreground">正在加载页面…</span>
    </div>
  );
}
