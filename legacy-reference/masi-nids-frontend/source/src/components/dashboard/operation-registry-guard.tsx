"use client";

import { useEffect } from "react";
import { Download, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n";
import {
  OPERATION_UNRESOLVED_LIMIT,
  serializeOperationRegistry,
  unresolvedOperationCount,
  useOperationRegistry,
} from "@/lib/operations";

export function OperationRegistryGuard() {
  const { t } = useI18n();
  const operations = useOperationRegistry((state) => state.operations);
  const pruneTerminal = useOperationRegistry((state) => state.pruneTerminal);
  const unresolved = unresolvedOperationCount(operations);

  useEffect(() => {
    pruneTerminal();
  }, [pruneTerminal]);

  if (unresolved < OPERATION_UNRESOLVED_LIMIT) return null;

  const exportRecovery = () => {
    const blob = new Blob([serializeOperationRegistry(operations)], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `nids-operation-recovery-${new Date().toISOString().replaceAll(":", "-")}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="mx-4 mt-2 flex flex-wrap items-center gap-3 rounded-lg border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm md:mx-6" role="alert">
      <ShieldAlert className="size-5 shrink-0 text-destructive" aria-hidden="true" />
      <p className="min-w-0 flex-1 font-medium">{t("operations.recoveryLimit")}</p>
      <Button type="button" size="sm" variant="outline" onClick={exportRecovery}>
        <Download aria-hidden="true" />
        {t("operations.exportRecovery")}
      </Button>
    </div>
  );
}
