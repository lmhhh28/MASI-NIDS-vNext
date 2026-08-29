"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Loader2, Crosshair, Edit3, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useState } from "react";

import api, { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { AdminDeleteResult, ProtectedTarget, ProtectedTargetPatch } from "@/types/api";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

function useTargets() {
  return useQuery<ProtectedTarget[]>({ queryKey: ["admin-targets"], queryFn: ({ signal }) => api.get("/admin/protected-targets", { signal }).then(r => r.data) });
}

type TargetType = "cidr" | "port";
type TargetForm = { target_type: TargetType; value: string; reason: string };
type TargetPayload = TargetForm & { enabled: boolean };
type TargetEditForm = TargetPayload;

const emptyForm: TargetForm = { target_type: "cidr", value: "", reason: "" };
const emptyEditForm: TargetEditForm = { ...emptyForm, enabled: true };

function normalizeIpv4Cidr(value: string) {
  const trimmed = value.trim();
  const [address, prefixText] = trimmed.split("/");
  if (!address || trimmed.split("/").length > 2) {
    return null;
  }
  const octets = address.split(".");
  if (octets.length !== 4) {
    return null;
  }
  const normalizedOctets = octets.map((octet) => {
    if (!/^\d+$/.test(octet)) {
      return null;
    }
    const numeric = Number(octet);
    return Number.isInteger(numeric) && numeric >= 0 && numeric <= 255 ? String(numeric) : null;
  });
  if (normalizedOctets.some((octet) => octet === null)) {
    return null;
  }
  const prefix = prefixText == null || prefixText === "" ? 32 : Number(prefixText);
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) {
    return null;
  }
  return `${normalizedOctets.join(".")}/${prefix}`;
}

function normalizePort(value: string) {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) {
    return null;
  }
  const port = Number(trimmed);
  return Number.isInteger(port) && port >= 1 && port <= 65535 ? String(port) : null;
}

export default function TargetsPage() {
  const targetsQ = useTargets();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<TargetForm>(emptyForm);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [editingTarget, setEditingTarget] = useState<ProtectedTarget | null>(null);
  const [editForm, setEditForm] = useState<TargetEditForm>(emptyEditForm);
  const [editFieldError, setEditFieldError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProtectedTarget | null>(null);
  const { t, locale } = useI18n();
  const runtimeSafety = useRuntimeSafety("admin");

  const createMut = useMutation({
    mutationFn: (payload: TargetPayload) => api.post("/admin/protected-targets", payload),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["admin-targets"] }); toast.success(t("admin.targets.targetAdded")); setOpen(false); setForm(emptyForm); setFieldError(null); },
    onError: (e) => toast.error(t("common.failed"), { description: apiErrorMessage(e, locale) }),
  });

  const patchMut = useMutation<ProtectedTarget, Error, { id: string; payload: ProtectedTargetPatch }>({
    mutationFn: ({ id, payload }) => api.patch(`/admin/protected-targets/${id}`, payload).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-targets"] });
      toast.success(t("admin.targets.targetUpdated"));
      setEditingTarget(null);
      setEditForm(emptyEditForm);
      setEditFieldError(null);
    },
    onError: (e) => toast.error(t("common.failed"), { description: apiErrorMessage(e, locale) }),
  });

  const deleteMut = useMutation<AdminDeleteResult, Error, string>({
    mutationFn: (id) => api.delete(`/admin/protected-targets/${id}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-targets"] });
      toast.success(t("admin.targets.targetDeleted"));
      setDeleteTarget(null);
    },
    onError: (e) => toast.error(t("common.failed"), { description: apiErrorMessage(e, locale) }),
  });

  function normalizedPayload(source: TargetEditForm | TargetForm, enabled: boolean) {
    const normalizedValue = source.target_type === "cidr" ? normalizeIpv4Cidr(source.value) : normalizePort(source.value);
    if (!normalizedValue) {
      return null;
    }
    return {
      target_type: source.target_type,
      value: normalizedValue,
      reason: source.reason.trim(),
      enabled,
    };
  }

  function submitTarget() {
    if (!runtimeSafety.guard()) return;
    const payload = normalizedPayload(form, true);
    if (!payload) {
      setFieldError(form.target_type === "cidr" ? t("admin.targets.cidrError") : t("admin.targets.portError"));
      return;
    }
    setFieldError(null);
    createMut.mutate(payload);
  }

  function openEdit(target: ProtectedTarget) {
    setEditingTarget(target);
    setEditFieldError(null);
    setEditForm({
      target_type: target.target_type,
      value: target.value,
      reason: target.reason ?? "",
      enabled: Boolean(target.enabled),
    });
  }

  function submitEdit() {
    if (!runtimeSafety.guard()) return;
    if (!editingTarget) {
      return;
    }
    const payload = normalizedPayload(editForm, editForm.enabled);
    if (!payload) {
      setEditFieldError(editForm.target_type === "cidr" ? t("admin.targets.cidrError") : t("admin.targets.portError"));
      return;
    }
    setEditFieldError(null);
    patchMut.mutate({ id: editingTarget.id, payload });
  }

  function removeTarget() {
    if (!runtimeSafety.guard() || !deleteTarget) return;
    deleteMut.mutate(deleteTarget.id);
  }

  if (targetsQ.isError) {
    return (
      <div className="rounded-xl border border-destructive/30 bg-card p-6 text-sm text-destructive">
        {apiErrorMessage(targetsQ.error, locale)}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-heading font-semibold">{t("admin.targets.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("admin.targets.subtitle")}</p>
        </div>
        <Dialog open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (!nextOpen) { setForm(emptyForm); setFieldError(null); } }}>
          <DialogTrigger render={<Button size="sm" className="h-8" disabled={!runtimeSafety.allowed}><Plus className="mr-1.5 h-3.5 w-3.5" />{t("admin.targets.addTarget")}</Button>} />
          <DialogContent className="sm:max-w-md">
            <DialogHeader><DialogTitle>{t("admin.targets.addTargetTitle")}</DialogTitle><DialogDescription>{t("admin.targets.addTargetDescription")}</DialogDescription></DialogHeader>
            <div className="space-y-3 mt-2">
              <div className="space-y-1.5">
                <Label htmlFor="protected-target-type" className="text-xs">{t("common.type")}</Label>
                <Select value={form.target_type} onValueChange={v => { if (v) { setFieldError(null); setForm(p => ({ ...p, target_type: v as TargetType })); } }}>
                  <SelectTrigger id="protected-target-type" className="h-8 text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent><SelectItem value="cidr">{t("admin.targets.cidr")}</SelectItem><SelectItem value="port">{t("admin.targets.port")}</SelectItem></SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs" htmlFor="protected-target-value">{form.target_type === "cidr" ? t("admin.targets.cidr") : t("admin.targets.port")}</Label>
                <Input id="protected-target-value" className="h-8 text-xs font-mono" inputMode={form.target_type === "port" ? "numeric" : "text"} placeholder={form.target_type === "cidr" ? "10.0.0.1/32" : "8088"} value={form.value} aria-invalid={!!fieldError} aria-describedby="protected-target-value-error" onChange={e => { setFieldError(null); setForm(p => ({ ...p, value: e.target.value })); }} />
                {fieldError && <p id="protected-target-value-error" className="text-xs text-destructive" role="alert" aria-live="polite">{fieldError}</p>}
              </div>
              <div className="space-y-1.5"><Label htmlFor="protected-target-reason" className="text-xs">{t("admin.targets.reason")}</Label><Input id="protected-target-reason" className="h-8 text-xs" placeholder={t("admin.targets.managementHost")} value={form.reason} onChange={e => setForm(p => ({ ...p, reason: e.target.value }))} /></div>
              <Button className="w-full" onClick={submitTarget} disabled={!runtimeSafety.allowed || createMut.isPending || !form.value.trim()}>
                {createMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" />}{t("admin.targets.addTarget")}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>
      {targetsQ.isLoading ? (
        <div className="space-y-2">{[1, 2, 3].map(i => <Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : !targetsQ.data || targetsQ.data.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center">
          <Crosshair className="h-10 w-10 text-muted-foreground/40 mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">{t("admin.targets.noTargets")}</p>
        </div>
      ) : (
        <div className="rounded-xl border border-border bg-card overflow-hidden overflow-x-auto">
          <Table>
            <TableHeader><TableRow className="border-border hover:bg-transparent"><TableHead className="text-xs">{t("common.type")}</TableHead><TableHead className="text-xs">{t("common.value")}</TableHead><TableHead className="text-xs">{t("admin.targets.reason")}</TableHead><TableHead className="text-xs">{t("common.status")}</TableHead><TableHead className="text-right text-xs">{t("common.actions")}</TableHead></TableRow></TableHeader>
            <TableBody>
              {targetsQ.data.map((target) => (
                <TableRow key={target.id} className="border-border hover:bg-secondary/20">
                  <TableCell><Badge variant="outline" className="text-xs">{target.target_type}</Badge></TableCell>
                  <TableCell className="text-sm font-mono">{target.value}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{target.reason ?? "—"}</TableCell>
                  <TableCell><Badge variant={target.enabled ? "success" : "secondary"} className="text-xs">{target.enabled ? t("common.active") : t("common.disabled")}</Badge></TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-2">
                      <Button size="sm" variant="outline" className="h-7" disabled={!runtimeSafety.allowed} onClick={() => openEdit(target)}>
                        <Edit3 className="mr-1.5 h-3.5 w-3.5" />{t("admin.targets.editTarget")}
                      </Button>
                      <Button size="sm" variant="destructive" className="h-7" disabled={!runtimeSafety.allowed} onClick={() => setDeleteTarget(target)}>
                        <Trash2 className="mr-1.5 h-3.5 w-3.5" />{t("admin.targets.deleteTarget")}
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog open={!!editingTarget} onOpenChange={(nextOpen) => { if (!nextOpen) { setEditingTarget(null); setEditForm(emptyEditForm); setEditFieldError(null); } }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader><DialogTitle>{t("admin.targets.editTarget")}</DialogTitle><DialogDescription>{editingTarget?.value}</DialogDescription></DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="protected-target-edit-type" className="text-xs">{t("common.type")}</Label>
              <Select value={editForm.target_type} onValueChange={v => { if (v) { setEditFieldError(null); setEditForm(p => ({ ...p, target_type: v as TargetType })); } }}>
                <SelectTrigger id="protected-target-edit-type" className="h-8 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="cidr">{t("admin.targets.cidr")}</SelectItem><SelectItem value="port">{t("admin.targets.port")}</SelectItem></SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs" htmlFor="protected-target-edit-value">{editForm.target_type === "cidr" ? t("admin.targets.cidr") : t("admin.targets.port")}</Label>
              <Input id="protected-target-edit-value" className="h-8 text-xs font-mono" inputMode={editForm.target_type === "port" ? "numeric" : "text"} value={editForm.value} aria-invalid={!!editFieldError} aria-describedby="protected-target-edit-value-error" onChange={e => { setEditFieldError(null); setEditForm(p => ({ ...p, value: e.target.value })); }} />
              {editFieldError && <p id="protected-target-edit-value-error" className="text-xs text-destructive" role="alert" aria-live="polite">{editFieldError}</p>}
            </div>
            <div className="space-y-1.5"><Label htmlFor="protected-target-edit-reason" className="text-xs">{t("admin.targets.reason")}</Label><Input id="protected-target-edit-reason" className="h-8 text-xs" value={editForm.reason} onChange={e => setEditForm(p => ({ ...p, reason: e.target.value }))} /></div>
            <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
              <Label htmlFor="protected-target-edit-enabled" className="text-xs">{t("common.status")}</Label>
              <Switch id="protected-target-edit-enabled" checked={editForm.enabled} onCheckedChange={v => setEditForm(p => ({ ...p, enabled: v }))} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setEditingTarget(null)} disabled={patchMut.isPending}>{t("common.cancel")}</Button>
            <Button size="sm" onClick={submitEdit} disabled={!runtimeSafety.allowed || patchMut.isPending || !editForm.value.trim()}>
              {patchMut.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />}{t("common.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!deleteTarget} onOpenChange={(nextOpen) => { if (!nextOpen) setDeleteTarget(null); }}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader><DialogTitle>{t("admin.targets.confirmDelete")}</DialogTitle><DialogDescription>{deleteTarget?.value}</DialogDescription></DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setDeleteTarget(null)} disabled={deleteMut.isPending}>{t("common.cancel")}</Button>
            <Button variant="destructive" size="sm" onClick={removeTarget} disabled={!runtimeSafety.allowed || deleteMut.isPending}>
              {deleteMut.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />}{t("admin.targets.deleteTarget")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
