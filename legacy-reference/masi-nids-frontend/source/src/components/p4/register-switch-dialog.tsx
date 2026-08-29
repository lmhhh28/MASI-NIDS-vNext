"use client";

import { useState, type FormEvent } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useRegisterSwitch } from "@/hooks/use-p4";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

type PipelineOwner = "external" | "digest_controller" | "p4_agent";

interface RegisterSwitchDialogProps {
  isAdmin: boolean;
  maintenance: boolean;
  onClose: () => void;
}

export function RegisterSwitchDialog({
  isAdmin,
  maintenance,
  onClose,
}: RegisterSwitchDialogProps) {
  const { t, locale } = useI18n();
  const register = useRegisterSwitch();
  const runtimeSafety = useRuntimeSafety("p4");
  const [name, setName] = useState("");
  const [grpcAddress, setGrpcAddress] = useState("127.0.0.1:50051");
  const [deviceId, setDeviceId] = useState("0");
  const [p4infoPath, setP4infoPath] = useState("p4/ae_nids.p4");
  const [pipelineOwner, setPipelineOwner] = useState<PipelineOwner>(
    isAdmin ? "p4_agent" : "external",
  );
  const [errors, setErrors] = useState<Record<string, string>>({});

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!runtimeSafety.guard()) return;
    const parsedDeviceId = Number(deviceId);
    const nextErrors: Record<string, string> = {};
    if (!name.trim()) nextErrors.name = t("validation.nameRequired");
    if (!grpcAddress.trim()) nextErrors.grpc = t("validation.grpcRequired");
    if (!Number.isInteger(parsedDeviceId) || parsedDeviceId < 0) nextErrors.device = t("validation.deviceId");
    if (!p4infoPath.trim()) nextErrors.p4info = t("validation.p4infoRequired");
    if (!isAdmin && pipelineOwner === "p4_agent") nextErrors.owner = t("p4.writeMasterAdminOnly");
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    register.mutate(
      {
        name: name.trim(),
        grpc_addr: grpcAddress.trim(),
        device_id: parsedDeviceId,
        p4info_path: p4infoPath.trim(),
        pipeline_owner: pipelineOwner,
      },
      {
        onSuccess: () => {
          toast.success(t("p4.switchRegistered"));
          onClose();
        },
        onError: (error) => toast.error(t("p4.registerFailed"), {
          description: apiErrorMessage(error, locale),
        }),
      },
    );
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("p4.registerSwitchTitle")}</DialogTitle>
          <DialogDescription>{t("p4.registerSwitchDescription")}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate className="mt-2 space-y-4">
          <div className="space-y-2">
            <Label htmlFor="reg-name">{t("common.name")}</Label>
            <Input id="reg-name" value={name} onChange={(event) => setName(event.currentTarget.value)} placeholder="s1" aria-invalid={Boolean(errors.name)} />
            {errors.name ? <p role="alert" className="text-xs text-destructive">{errors.name}</p> : null}
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="reg-addr">{t("p4.grpcAddress")}</Label>
              <Input id="reg-addr" value={grpcAddress} onChange={(event) => setGrpcAddress(event.currentTarget.value)} aria-invalid={Boolean(errors.grpc)} />
              {errors.grpc ? <p role="alert" className="text-xs text-destructive">{errors.grpc}</p> : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor="reg-device">{t("p4.deviceId")}</Label>
              <Input id="reg-device" type="number" min={0} value={deviceId} onChange={(event) => setDeviceId(event.currentTarget.value)} aria-invalid={Boolean(errors.device)} />
              {errors.device ? <p role="alert" className="text-xs text-destructive">{errors.device}</p> : null}
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="reg-p4info">{t("p4.p4infoPath")}</Label>
            <Input id="reg-p4info" value={p4infoPath} onChange={(event) => setP4infoPath(event.currentTarget.value)} aria-invalid={Boolean(errors.p4info)} />
            {errors.p4info ? <p role="alert" className="text-xs text-destructive">{errors.p4info}</p> : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor="reg-pipeline-owner">{t("p4.pipelineOwner")}</Label>
            <select
              id="reg-pipeline-owner"
              value={pipelineOwner}
              onChange={(event) => setPipelineOwner(event.currentTarget.value as PipelineOwner)}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {isAdmin ? <option value="p4_agent">p4_agent</option> : null}
              <option value="digest_controller">digest_controller</option>
              <option value="external">external</option>
            </select>
            {!isAdmin ? <p className="text-xs text-muted-foreground">{t("p4.writeMasterAdminOnly")}</p> : null}
          </div>
          <Button type="submit" className="w-full" disabled={!runtimeSafety.allowed || maintenance || register.isPending}>
            {register.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" /> : null}
            {t("p4.register")}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
