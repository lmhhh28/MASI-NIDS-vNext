"use client";

import { useState } from "react";
import { Loader2, Plus, RefreshCw, Save } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useCreatePipelineTargetV3, useReplacePipelineTargetV3 } from "@/hooks/use-pipeline-control-v3";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { isCanonicalUint64 } from "@/lib/pipeline-v3";
import type {
  PipelineTargetRegistrationRequestV3,
  PipelineTargetV3,
  PipelineTransportMode,
} from "@/types/api";

type FormState = {
  targetUuid: string;
  connectUri: string;
  deviceId: string;
  role: string;
  transportMode: PipelineTransportMode;
  serverName: string;
  caBundleRef: string;
  clientCertificateRef: string;
};

function initialForm(target: PipelineTargetV3 | null): FormState {
  const registration = target?.registration;
  return {
    targetUuid: target?.target_uuid ?? "",
    connectUri: registration?.connect_uri ?? "grpc://127.0.0.1:50051",
    deviceId: registration ? String(registration.device_id) : "0",
    role: registration?.role ?? "",
    transportMode: registration?.transport_security.mode ?? "plaintext",
    serverName: registration?.transport_security.server_name ?? "",
    caBundleRef: registration?.transport_security.ca_bundle_ref ?? "",
    clientCertificateRef: registration?.transport_security.client_certificate_ref ?? "",
  };
}

export function TargetRegistrationDialog({
  target,
  onClose,
  writeAllowed,
  writeGuard,
}: {
  target: PipelineTargetV3 | null;
  onClose: () => void;
  writeAllowed: boolean;
  writeGuard: () => boolean;
}) {
  const [form, setForm] = useState(() => initialForm(target));
  const [formError, setFormError] = useState<string | null>(null);
  const createMutation = useCreatePipelineTargetV3();
  const replaceMutation = useReplacePipelineTargetV3();
  const { t, locale } = useI18n();
  const isEditing = Boolean(target);
  const isPending = createMutation.isPending || replaceMutation.isPending;

  function buildPayload(): PipelineTargetRegistrationRequestV3 | null {
    const targetUuid = form.targetUuid.trim();
    const connectUri = form.connectUri.trim();
    const deviceId = form.deviceId.trim();
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(targetUuid)) {
      setFormError(t("pipeline.v3.targetUuidInvalid"));
      return null;
    }
    if (!/^(grpc|grpcs):\/\/[^/\s]+$/.test(connectUri) && !/^unix:\/\/\/[A-Za-z0-9._/-]+$/.test(connectUri)) {
      setFormError(t("pipeline.v3.connectUriInvalid"));
      return null;
    }
    if (!isCanonicalUint64(deviceId)) {
      setFormError(t("pipeline.v3.deviceIdInvalid"));
      return null;
    }
    if (connectUri.startsWith("grpcs://") !== (form.transportMode !== "plaintext")) {
      setFormError(t("pipeline.v3.transportSchemeMismatch"));
      return null;
    }
    if (form.transportMode !== "plaintext" && (!form.serverName.trim() || !form.caBundleRef.trim())) {
      setFormError(t("pipeline.v3.tlsFieldsRequired"));
      return null;
    }
    if (form.transportMode === "mtls" && !form.clientCertificateRef.trim()) {
      setFormError(t("pipeline.v3.mtlsCertificateRequired"));
      return null;
    }

    const transportSecurity = form.transportMode === "plaintext"
      ? { mode: "plaintext" as const }
      : form.transportMode === "tls"
        ? {
            mode: "tls" as const,
            server_name: form.serverName.trim(),
            ca_bundle_ref: form.caBundleRef.trim(),
          }
        : {
            mode: "mtls" as const,
            server_name: form.serverName.trim(),
            ca_bundle_ref: form.caBundleRef.trim(),
            client_certificate_ref: form.clientCertificateRef.trim(),
          };
    return {
      schema_version: 3,
      target_uuid: targetUuid,
      connect_uri: connectUri,
      device_id: deviceId,
      role: form.role.trim(),
      transport_security: transportSecurity,
    };
  }

  async function submit() {
    if (!writeGuard()) return;
    const payload = buildPayload();
    if (!payload) return;
    setFormError(null);
    try {
      if (target) {
        await replaceMutation.mutateAsync({
          registration: payload,
          expectedVersion: target.registration_version,
        });
        toast.success(t("pipeline.v3.targetUpdated"));
      } else {
        await createMutation.mutateAsync(payload);
        toast.success(t("pipeline.v3.targetCreated"));
      }
      onClose();
    } catch (error) {
      setFormError(apiErrorMessage(error, locale));
    }
  }

  return (
    <Dialog open onOpenChange={(next) => { if (!next && !isPending) onClose(); }}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{isEditing ? t("pipeline.v3.editTarget") : t("pipeline.v3.createTarget")}</DialogTitle>
          <DialogDescription>{t("pipeline.v3.targetDialogDescription")}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5 sm:col-span-2">
            <Label htmlFor="pipeline-target-uuid">{t("pipeline.v3.targetUuid")}</Label>
            <div className="flex gap-2">
              <Input id="pipeline-target-uuid" value={form.targetUuid} readOnly={isEditing} onChange={(event) => { setForm((current) => ({ ...current, targetUuid: event.target.value })); setFormError(null); }} className="font-mono" />
              {!isEditing ? (
                <Button type="button" variant="outline" size="icon" aria-label={t("pipeline.v3.generateUuid")} title={t("pipeline.v3.generateUuid")} onClick={() => setForm((current) => ({ ...current, targetUuid: crypto.randomUUID() }))}>
                  <RefreshCw aria-hidden="true" />
                </Button>
              ) : null}
            </div>
          </div>
          <div className="space-y-1.5 sm:col-span-2">
            <Label htmlFor="pipeline-connect-uri">{t("pipeline.v3.connectUri")}</Label>
            <Input id="pipeline-connect-uri" value={form.connectUri} onChange={(event) => { setForm((current) => ({ ...current, connectUri: event.target.value })); setFormError(null); }} className="font-mono" />
            <p className="text-xs text-muted-foreground">{t("pipeline.v3.connectUriHelp")}</p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="pipeline-device-id">{t("pipeline.v3.deviceId")}</Label>
            <Input id="pipeline-device-id" type="number" inputMode="numeric" min="0" max={Number.MAX_SAFE_INTEGER} value={form.deviceId} onChange={(event) => { setForm((current) => ({ ...current, deviceId: event.target.value })); setFormError(null); }} className="font-mono" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="pipeline-role">{t("common.role")}</Label>
            <Input id="pipeline-role" value={form.role} onChange={(event) => setForm((current) => ({ ...current, role: event.target.value }))} placeholder={t("pipeline.v3.roleOptional")} />
          </div>
          <div className="space-y-1.5 sm:col-span-2">
            <Label htmlFor="pipeline-transport-mode">{t("pipeline.v3.transportSecurity")}</Label>
            <Select value={form.transportMode} onValueChange={(value) => value && setForm((current) => ({ ...current, transportMode: value as PipelineTransportMode }))}>
              <SelectTrigger id="pipeline-transport-mode"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="plaintext">plaintext</SelectItem>
                <SelectItem value="tls">TLS</SelectItem>
                <SelectItem value="mtls">mTLS</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {form.transportMode !== "plaintext" ? (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="pipeline-server-name">{t("pipeline.v3.serverName")}</Label>
                <Input id="pipeline-server-name" value={form.serverName} onChange={(event) => setForm((current) => ({ ...current, serverName: event.target.value }))} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="pipeline-ca-ref">{t("pipeline.v3.caBundleRef")}</Label>
                <Input id="pipeline-ca-ref" value={form.caBundleRef} onChange={(event) => setForm((current) => ({ ...current, caBundleRef: event.target.value }))} className="font-mono" />
              </div>
            </>
          ) : null}
          {form.transportMode === "mtls" ? (
            <div className="space-y-1.5 sm:col-span-2">
              <Label htmlFor="pipeline-client-cert-ref">{t("pipeline.v3.clientCertificateRef")}</Label>
              <Input id="pipeline-client-cert-ref" value={form.clientCertificateRef} onChange={(event) => setForm((current) => ({ ...current, clientCertificateRef: event.target.value }))} className="font-mono" />
            </div>
          ) : null}
          {formError ? <Alert variant="destructive" className="sm:col-span-2"><AlertDescription>{formError}</AlertDescription></Alert> : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={isPending}>{t("common.cancel")}</Button>
          <Button onClick={() => void submit()} disabled={!writeAllowed || isPending}>
            {isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : isEditing ? <Save aria-hidden="true" /> : <Plus aria-hidden="true" />}
            {isEditing ? t("common.save") : t("common.create")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
