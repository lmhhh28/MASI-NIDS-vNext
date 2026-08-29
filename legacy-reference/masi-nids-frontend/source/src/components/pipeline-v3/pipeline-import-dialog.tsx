"use client";

import { useState } from "react";
import { FileInput, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useImportPipelineBundleV3 } from "@/hooks/use-pipeline-control-v3";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export function PipelineImportDialog({ open, onClose, writeAllowed, writeGuard }: { open: boolean; onClose: () => void; writeAllowed: boolean; writeGuard: () => boolean }) {
  const [sourcePath, setSourcePath] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const mutation = useImportPipelineBundleV3();
  const { t, locale } = useI18n();

  function close() {
    onClose();
    setSourcePath("");
    setFormError(null);
    mutation.reset();
  }

  async function submit() {
    if (!writeGuard()) return;
    const path = sourcePath.trim();
    if (!path || path.startsWith("/") || path.split("/").some((part) => !part || part === "." || part === "..")) {
      setFormError(t("pipeline.v3.importPathInvalid"));
      return;
    }
    setFormError(null);
    try {
      const bundle = await mutation.mutateAsync(path);
      toast.success(t("pipeline.v3.imported"), { description: `${bundle.bundle_name} ${bundle.bundle_version}` });
      close();
    } catch (error) {
      setFormError(apiErrorMessage(error, locale));
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !mutation.isPending) close(); }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("pipeline.v3.importBundleTitle")}</DialogTitle>
          <DialogDescription>{t("pipeline.v3.importBundleDescription")}</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="pipeline-import-source">{t("pipeline.v3.importPath")}</Label>
          <Input
            id="pipeline-import-source"
            value={sourcePath}
            onChange={(event) => { setSourcePath(event.target.value); setFormError(null); }}
            placeholder="releases/masi-ae-nids/3.0.0"
            className="font-mono"
            aria-invalid={Boolean(formError)}
            aria-describedby="pipeline-import-help pipeline-import-error"
          />
          <p id="pipeline-import-help" className="text-xs text-muted-foreground">{t("pipeline.v3.importPathHelp")}</p>
          {formError ? <Alert id="pipeline-import-error" variant="destructive"><AlertDescription>{formError}</AlertDescription></Alert> : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={mutation.isPending}>{t("common.cancel")}</Button>
          <Button onClick={() => void submit()} disabled={!writeAllowed || mutation.isPending || !sourcePath.trim()}>
            {mutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <FileInput aria-hidden="true" />}
            {t("pipeline.v3.verifyAndImport")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
