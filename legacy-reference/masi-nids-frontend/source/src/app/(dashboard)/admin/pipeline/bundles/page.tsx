"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useState } from "react";
import { Boxes, FileInput, Loader2, RefreshCw, ServerCog, ShieldCheck, TriangleAlert } from "lucide-react";

import { EmptyState, ErrorState, LoadingState } from "@/components/async-state";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { usePipelineBundlesV3 } from "@/hooks/use-pipeline-control-v3";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { useI18n } from "@/lib/i18n";
import { shortIdentity } from "@/lib/pipeline-v3";
import type { PipelineBundleV3 } from "@/types/api";

function ImportDialogLoading() {
  const { t } = useI18n();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" role="status">
      <div className="rounded-lg bg-popover p-3 text-popover-foreground shadow-lg">
        <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
        <span className="sr-only">{t("pipeline.v3.loadingDialog")}</span>
      </div>
    </div>
  );
}

const PipelineImportDialog = dynamic(
  () => import("@/components/pipeline-v3/pipeline-import-dialog").then((module) => module.PipelineImportDialog),
  {
    ssr: false,
    loading: ImportDialogLoading,
  },
);

function BundleCard({ bundle }: { bundle: PipelineBundleV3 }) {
  const { t, formatDateTime } = useI18n();
  return (
    <Card>
      <CardHeader className="grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0">
          <CardTitle>{bundle.bundle_name} <span className="font-normal text-muted-foreground">{bundle.bundle_version}</span></CardTitle>
          <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{bundle.bundle_id}</p>
        </div>
        <Badge variant={bundle.status === "available" ? "success" : "secondary"}>{bundle.status}</Badge>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-x-5 gap-y-3 text-xs sm:grid-cols-2 xl:grid-cols-4">
          <div><dt className="text-muted-foreground">{t("pipeline.v3.signingKey")}</dt><dd className="mt-1 font-mono">{bundle.signing_key_id}</dd></div>
          <div><dt className="text-muted-foreground">{t("pipeline.v3.sourceRevision")}</dt><dd className="mt-1 font-mono" title={bundle.source_revision}>{shortIdentity(bundle.source_revision)}</dd></div>
          <div><dt className="text-muted-foreground">{t("pipeline.v3.bundleCreated")}</dt><dd className="mt-1 tabular-nums">{formatDateTime(bundle.bundle_created_at)}</dd></div>
          <div><dt className="text-muted-foreground">{t("pipeline.v3.importedAt")}</dt><dd className="mt-1 tabular-nums">{formatDateTime(bundle.imported_at)}</dd></div>
        </dl>
        <div className="space-y-2">
          <h3 className="text-sm font-medium">{t("pipeline.v3.variants")}</h3>
          <div className="grid gap-3 lg:grid-cols-2">
            {bundle.variants.map((variant) => (
              <div key={variant.variant_id} className="rounded-lg border bg-muted/20 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-sm font-medium">{variant.variant_id}</span>
                  <Badge variant="outline">{variant.adapter_kind}</Badge>
                </div>
                <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                  <div><dt className="text-muted-foreground">{t("pipeline.v3.activationMode")}</dt><dd className="mt-0.5 break-words font-mono">{variant.activation_mode}</dd></div>
                  <div><dt className="text-muted-foreground">{t("pipeline.v3.pipelineCookie")}</dt><dd className="mt-0.5 font-mono tabular-nums">{variant.pipeline_cookie}</dd></div>
                  <div className="sm:col-span-2"><dt className="text-muted-foreground">{t("pipeline.v3.capabilities")}</dt><dd className="mt-1 flex flex-wrap gap-1">{variant.capabilities.map((capability) => <Badge variant="secondary" key={capability}>{capability}</Badge>)}</dd></div>
                </dl>
              </div>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function PipelineBundlesV3Page() {
  const bundlesQuery = usePipelineBundlesV3();
  const p4Safety = useRuntimeSafety("p4");
  const { t } = useI18n();
  const [importOpen, setImportOpen] = useState(false);
  const writesAllowed = p4Safety.allowed;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-heading font-semibold">{t("pipeline.v3.title")}</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t("pipeline.v3.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void Promise.all([bundlesQuery.refetch(), p4Safety.retry()])} disabled={bundlesQuery.isFetching}>
          <RefreshCw className={bundlesQuery.isFetching ? "animate-spin motion-reduce:animate-none" : ""} aria-hidden="true" />{t("common.refresh")}
        </Button>
      </header>

      <Alert><ShieldCheck aria-hidden="true" /><AlertTitle>{t("pipeline.v3.fenceTitle")}</AlertTitle><AlertDescription>{t("pipeline.v3.fenceDescription")}</AlertDescription></Alert>
      {!writesAllowed ? <Alert variant="destructive"><TriangleAlert aria-hidden="true" /><AlertTitle>{t("pipeline.v3.writesUnavailable")}</AlertTitle><AlertDescription>{t(`pipeline.v3.safety.${p4Safety.reason ?? "loading"}`)}</AlertDescription></Alert> : null}

      <nav className="flex flex-wrap gap-2" aria-label={t("pipeline.v3.views")}>
        <Link href="/admin/pipeline" prefetch={false} className={buttonVariants({ variant: "outline", size: "sm" })}><ServerCog aria-hidden="true" />{t("pipeline.v3.targetsTab")}</Link>
        <Link href="/admin/pipeline/bundles" aria-current="page" className={buttonVariants({ variant: "secondary", size: "sm" })}><Boxes aria-hidden="true" />{t("pipeline.v3.bundlesTab")}</Link>
      </nav>

      <section className="space-y-4" aria-label={t("pipeline.v3.bundlesTab")}>
        <div className="flex items-center justify-between gap-3">
          <div><h2 className="text-lg font-semibold">{t("pipeline.v3.bundlesTitle")}</h2><p className="text-sm text-muted-foreground">{t("pipeline.v3.bundlesDescription")}</p></div>
          <Button size="sm" onClick={() => setImportOpen(true)} disabled={!writesAllowed}><FileInput aria-hidden="true" />{t("pipeline.v3.importBundle")}</Button>
        </div>
        {bundlesQuery.isLoading ? <LoadingState label={t("pipeline.v3.loadingBundles")} rows={3} /> : null}
        {bundlesQuery.isError ? <ErrorState error={bundlesQuery.error} title={t("pipeline.v3.bundlesLoadFailed")} onRetry={() => void bundlesQuery.refetch()} /> : null}
        {bundlesQuery.data?.length === 0 ? <EmptyState title={t("pipeline.v3.noBundles")} description={t("pipeline.v3.noBundlesDescription")} /> : null}
        {bundlesQuery.data?.length ? <div className="space-y-4">{bundlesQuery.data.map((bundle) => <BundleCard key={bundle.bundle_id} bundle={bundle} />)}</div> : null}
      </section>

      {importOpen ? <PipelineImportDialog open onClose={() => setImportOpen(false)} writeAllowed={writesAllowed} writeGuard={p4Safety.guard} /> : null}
    </div>
  );
}
