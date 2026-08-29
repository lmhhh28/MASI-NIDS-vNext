"use client";

import { useState } from "react";
import { Loader2, Pencil, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useAuthStore } from "@/lib/auth";
import { useDeleteSwitch, usePatchSwitch } from "@/hooks/use-p4-admin";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import type { P4Switch } from "@/types/api";

interface SwitchActionsProps {
  sw: P4Switch;
}

export function SwitchActions({ sw }: SwitchActionsProps) {
  const { t, locale } = useI18n();
  const isAdmin = useAuthStore((s) => s.isAdmin)();
  const [editOpen, setEditOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [form, setForm] = useState({
    name: sw.name,
    grpc_addr: sw.grpc_addr,
    p4info_path: sw.p4info_path,
    pipeline_owner: sw.pipeline_owner,
  });
  const [force, setForce] = useState(false);
  const patchMut = usePatchSwitch(sw.id);
  const deleteMut = useDeleteSwitch();
  const runtimeSafety = useRuntimeSafety("p4");
  const p4Maintenance = !runtimeSafety.allowed;

  if (!isAdmin) return null;

  function handleSave() {
    if (!runtimeSafety.guard()) return;
    patchMut.mutate(
      {
        name: form.name.trim() || undefined,
        grpc_addr: form.grpc_addr.trim() || undefined,
        p4info_path: form.p4info_path.trim() || undefined,
        pipeline_owner: form.pipeline_owner,
      },
      {
        onSuccess: () => {
          toast.success(t("common.saved"));
          setEditOpen(false);
        },
        onError: (err) =>
          toast.error(t("common.failed"), {
            description: apiErrorMessage(err, locale),
          }),
      }
    );
  }

  function handleDelete() {
    if (!runtimeSafety.guard()) return;
    deleteMut.mutate(
      { switchId: sw.id, force },
      {
        onSuccess: () => {
          toast.success(t("p4.deleted"));
          setConfirmOpen(false);
          setForce(false);
        },
        onError: (err) =>
          toast.error(t("common.failed"), {
            description: apiErrorMessage(err, locale),
          }),
      }
    );
  }

  const requiresForce =
    (sw.pipeline_owner === "p4_agent" || sw.pipeline_owner === "backend_p4_manager") && sw.writes_enabled;

  return (
    <div className="flex items-center gap-1">
      <Button
        size="sm"
        variant="ghost"
        className="h-7 px-2 text-xs cursor-pointer"
        aria-label={t("p4.editSwitch")}
        disabled={p4Maintenance}
        onClick={() => setEditOpen(true)}
      >
        <Pencil className="h-3.5 w-3.5" />
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className="h-7 px-2 text-xs text-destructive hover:bg-destructive/10 hover:text-destructive cursor-pointer"
        aria-label={t("p4.deleteSwitch")}
        disabled={p4Maintenance}
        onClick={() => {
          setForce(false);
          setConfirmOpen(true);
        }}
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("p4.editSwitch")}</DialogTitle>
            <DialogDescription>
              {t("p4.registerSwitchDescription")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 mt-2">
            <div className="space-y-1.5">
              <Label htmlFor={`switch-${sw.id}-name`} className="text-xs">{t("common.name")}</Label>
              <Input
                id={`switch-${sw.id}-name`}
                className="h-8 text-xs"
                value={form.name}
                onChange={(e) =>
                  setForm((p) => ({ ...p, name: e.target.value }))
                }
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor={`switch-${sw.id}-grpc`} className="text-xs">gRPC</Label>
                <Input
                  id={`switch-${sw.id}-grpc`}
                  className="h-8 text-xs font-mono"
                  value={form.grpc_addr}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, grpc_addr: e.target.value }))
                  }
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`switch-${sw.id}-device`} className="text-xs">{t("p4.deviceId")}</Label>
                <Input
                  id={`switch-${sw.id}-device`}
                  className="h-8 text-xs font-mono"
                  value={String(sw.device_id)}
                  disabled
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`switch-${sw.id}-p4info`} className="text-xs">P4Info path</Label>
              <Input
                id={`switch-${sw.id}-p4info`}
                className="h-8 text-xs font-mono"
                value={form.p4info_path}
                onChange={(e) =>
                  setForm((p) => ({ ...p, p4info_path: e.target.value }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`switch-${sw.id}-owner`} className="text-xs">pipeline_owner</Label>
              <Select
                value={form.pipeline_owner}
                onValueChange={(v) => v && setForm((p) => ({ ...p, pipeline_owner: v }))}
              >
                <SelectTrigger id={`switch-${sw.id}-owner`} className="h-8 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="external">external</SelectItem>
                  <SelectItem value="digest_controller">digest_controller</SelectItem>
                  <SelectItem value="p4_agent">p4_agent</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              className="h-8 cursor-pointer"
              onClick={() => setEditOpen(false)}
              disabled={patchMut.isPending}
            >
              {t("common.cancel")}
            </Button>
            <Button
              size="sm"
              className="h-8 cursor-pointer"
              onClick={handleSave}
              disabled={
                p4Maintenance ||
                patchMut.isPending ||
                !form.name.trim() ||
                !form.grpc_addr.trim() ||
                !form.p4info_path.trim()
              }
            >
              {patchMut.isPending && (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
              )}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={confirmOpen}
        onOpenChange={(open) => {
          setConfirmOpen(open);
          setForce(false);
        }}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{t("p4.deleteConfirm")}</DialogTitle>
            <DialogDescription>
              {requiresForce
                ? t("p4.deleteForceWarning")
                : t("p4.deleteSwitch")}
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-lg border border-border bg-secondary/30 p-3 space-y-1">
            <p className="text-xs font-mono">{sw.name}</p>
            <p className="text-xs text-muted-foreground font-mono">
              {sw.grpc_addr} · owner={sw.pipeline_owner} · writes=
              {sw.writes_enabled ? "on" : "off"}
            </p>
          </div>
          {requiresForce && (
            <label className="flex items-center justify-between rounded-lg border border-warning/40 bg-warning/5 px-3 py-2">
              <span className="text-xs">{t("p4.deleteForceCheckbox")}</span>
              <Switch checked={force} onCheckedChange={setForce} />
            </label>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              className="h-8 cursor-pointer"
              onClick={() => setConfirmOpen(false)}
              disabled={deleteMut.isPending}
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              size="sm"
              className="h-8 cursor-pointer"
              onClick={handleDelete}
              disabled={
                p4Maintenance || deleteMut.isPending || (requiresForce && !force)
              }
            >
              {deleteMut.isPending && (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
              )}
              {t("admin.llm.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
