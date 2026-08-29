"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";
import { Activity, RefreshCw, Search, ShieldAlert } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/lib/i18n";

const TARGET_UUID_PARAM = "target_uuid";
const UUID_PATTERN = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

type DetectionView = "events" | "incidents";

export interface DetectionsTargetFilter {
  appliedTargetUuid: string;
  rawTargetUuid: string;
  eventsHref: string;
  incidentsHref: string;
  apply: (targetUuid: string) => void;
}

function viewHref(pathname: string, targetUuid: string) {
  if (!targetUuid) return pathname;
  const params = new URLSearchParams({ [TARGET_UUID_PARAM]: targetUuid });
  return `${pathname}?${params.toString()}`;
}

export function useDetectionsTargetFilter(onApply?: () => void): DetectionsTargetFilter {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const rawTargetUuid = searchParams.get(TARGET_UUID_PARAM) ?? "";
  const targetUuidCandidate = rawTargetUuid.trim();
  const appliedTargetUuid =
    !targetUuidCandidate || UUID_PATTERN.test(targetUuidCandidate) ? targetUuidCandidate : "";

  function apply(targetUuid: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (targetUuid) params.set(TARGET_UUID_PARAM, targetUuid);
    else params.delete(TARGET_UUID_PARAM);

    const query = params.toString();
    onApply?.();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }

  return {
    appliedTargetUuid,
    rawTargetUuid,
    eventsHref: viewHref("/detections", appliedTargetUuid),
    incidentsHref: viewHref("/detections/incidents", appliedTargetUuid),
    apply,
  };
}

function TargetUuidFilterForm({ filter }: { filter: DetectionsTargetFilter }) {
  const { t } = useI18n();
  const urlTargetCandidate = filter.rawTargetUuid.trim();
  const invalidUrlTarget = Boolean(urlTargetCandidate && !UUID_PATTERN.test(urlTargetCandidate));
  const [targetDraft, setTargetDraft] = useState(filter.rawTargetUuid);
  const [targetError, setTargetError] = useState<string | null>(
    invalidUrlTarget ? t("detections.v3.targetInvalid") : null,
  );

  function applyTargetFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = targetDraft.trim();
    if (value && !UUID_PATTERN.test(value)) {
      setTargetError(t("detections.v3.targetInvalid"));
      return;
    }

    setTargetDraft(value);
    setTargetError(null);
    filter.apply(value);
  }

  return (
    <form
      onSubmit={applyTargetFilter}
      className="grid gap-3 rounded-xl border bg-card p-4 sm:grid-cols-[minmax(16rem,1fr)_auto] sm:items-end"
    >
      <div className="space-y-1.5">
        <Label htmlFor="detections-v3-target-filter">{t("detections.v3.targetFilter")}</Label>
        <Input
          id="detections-v3-target-filter"
          value={targetDraft}
          onChange={(event) => {
            setTargetDraft(event.target.value);
            setTargetError(null);
          }}
          placeholder="00000000-0000-4000-8000-000000000000"
          className="min-h-11 font-mono sm:min-h-9"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          aria-invalid={Boolean(targetError)}
          aria-describedby={targetError ? "detections-v3-target-error" : "detections-v3-target-help"}
        />
        {targetError ? (
          <p id="detections-v3-target-error" role="alert" className="text-xs text-destructive">
            {targetError}
          </p>
        ) : (
          <p id="detections-v3-target-help" className="text-xs text-muted-foreground">
            {t("detections.v3.targetFilterHelp")}
          </p>
        )}
      </div>
      <Button type="submit" variant="secondary" className="min-h-11 w-full sm:min-h-9 sm:w-auto">
        <Search aria-hidden="true" />
        {t("detections.v3.applyFilter")}
      </Button>
    </form>
  );
}

export function DetectionsV3ListShell({
  activeView,
  filter,
  isRefreshing,
  onRefresh,
  children,
}: {
  activeView: DetectionView;
  filter: DetectionsTargetFilter;
  isRefreshing: boolean;
  onRefresh: () => void;
  children: ReactNode;
}) {
  const { t } = useI18n();

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-heading font-semibold">{t("detections.v3.title")}</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t("detections.v3.subtitle")}</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="min-h-11 w-full sm:min-h-9 sm:w-auto"
          onClick={onRefresh}
          disabled={isRefreshing}
        >
          <RefreshCw
            className={isRefreshing ? "animate-spin motion-reduce:animate-none" : ""}
            aria-hidden="true"
          />
          {t("common.refresh")}
        </Button>
      </header>

      <Alert>
        <ShieldAlert aria-hidden="true" />
        <AlertTitle>{t("detections.v3.fenceTitle")}</AlertTitle>
        <AlertDescription>{t("detections.v3.fenceDescription")}</AlertDescription>
      </Alert>

      <nav
        className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap"
        aria-label={t("detections.v3.views")}
      >
        <Link
          href={filter.eventsHref}
          prefetch={false}
          aria-current={activeView === "events" ? "page" : undefined}
          className={buttonVariants({
            variant: activeView === "events" ? "secondary" : "outline",
            size: "sm",
            className: "min-h-11 w-full sm:min-h-9 sm:w-auto",
          })}
        >
          <Activity aria-hidden="true" />
          {t("detections.v3.eventsTab")}
        </Link>
        <Link
          href={filter.incidentsHref}
          prefetch={false}
          aria-current={activeView === "incidents" ? "page" : undefined}
          className={buttonVariants({
            variant: activeView === "incidents" ? "secondary" : "outline",
            size: "sm",
            className: "min-h-11 w-full sm:min-h-9 sm:w-auto",
          })}
        >
          <ShieldAlert aria-hidden="true" />
          {t("detections.v3.incidentsTab")}
        </Link>
      </nav>

      <TargetUuidFilterForm key={filter.rawTargetUuid} filter={filter} />

      {children}
    </div>
  );
}
