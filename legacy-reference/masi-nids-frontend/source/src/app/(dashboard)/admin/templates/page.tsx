"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Loader2, FileCode2, Edit3, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { useState } from "react";

import api, { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { AdminDeleteResult, SemanticTemplate, SemanticTemplatePatch } from "@/types/api";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

function useTemplates() {
  return useQuery<SemanticTemplate[]>({ queryKey: ["admin-templates"], queryFn: ({ signal }) => api.get("/admin/templates", { signal }).then(r => r.data) });
}

type TemplateForm = { name: string; description: string; admin_only: boolean; enabled: boolean };
const emptyTemplateForm: TemplateForm = { name: "", description: "", admin_only: false, enabled: true };

export default function TemplatesPage() {
  const templatesQ = useTemplates();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", description: "", admin_only: false });
  const [editingTemplate, setEditingTemplate] = useState<SemanticTemplate | null>(null);
  const [editForm, setEditForm] = useState<TemplateForm>(emptyTemplateForm);
  const [deleteTemplate, setDeleteTemplate] = useState<SemanticTemplate | null>(null);
  const { t, locale } = useI18n();
  const runtimeSafety = useRuntimeSafety("admin");

  const createMut = useMutation({
    mutationFn: () => api.post("/admin/templates", { ...form, enabled: true }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["admin-templates"] }); toast.success(t("admin.templates.templateCreated")); setOpen(false); setForm({ name: "", description: "", admin_only: false }); },
    onError: (e) => toast.error(t("common.failed"), { description: apiErrorMessage(e, locale) }),
  });

  const patchMut = useMutation<SemanticTemplate, Error, { id: string; payload: SemanticTemplatePatch }>({
    mutationFn: ({ id, payload }) => api.patch(`/admin/templates/${id}`, payload).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-templates"] });
      toast.success(t("admin.templates.templateUpdated"));
      setEditingTemplate(null);
      setEditForm(emptyTemplateForm);
    },
    onError: (e) => toast.error(t("common.failed"), { description: apiErrorMessage(e, locale) }),
  });

  const deleteMut = useMutation<AdminDeleteResult, Error, string>({
    mutationFn: (id) => api.delete(`/admin/templates/${id}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-templates"] });
      toast.success(t("admin.templates.templateDeleted"));
      setDeleteTemplate(null);
    },
    onError: (e) => toast.error(t("common.failed"), { description: apiErrorMessage(e, locale) }),
  });

  function openEdit(template: SemanticTemplate) {
    setEditingTemplate(template);
    setEditForm({
      name: template.name,
      description: template.description ?? "",
      admin_only: Boolean(template.admin_only),
      enabled: Boolean(template.enabled),
    });
  }

  function submitEdit() {
    if (!runtimeSafety.guard()) return;
    if (!editingTemplate) {
      return;
    }
    patchMut.mutate({
      id: editingTemplate.id,
      payload: {
        name: editForm.name.trim(),
        description: editForm.description.trim(),
        admin_only: editForm.admin_only,
        enabled: editForm.enabled,
      },
    });
  }

  function createTemplate() {
    if (!runtimeSafety.guard()) return;
    createMut.mutate();
  }

  function removeTemplate() {
    if (!runtimeSafety.guard() || !deleteTemplate) return;
    deleteMut.mutate(deleteTemplate.id);
  }

  if (templatesQ.isError) {
    return (
      <div className="rounded-xl border border-destructive/30 bg-card p-6 text-sm text-destructive">
        {apiErrorMessage(templatesQ.error, locale)}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-heading font-semibold">{t("admin.templates.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("admin.templates.subtitle")}</p>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger render={<Button size="sm" className="h-8" disabled={!runtimeSafety.allowed}><Plus className="mr-1.5 h-3.5 w-3.5" />{t("admin.templates.addTemplate")}</Button>} />
          <DialogContent className="sm:max-w-md">
            <DialogHeader><DialogTitle>{t("admin.templates.createTemplate")}</DialogTitle><DialogDescription>{t("admin.templates.createTemplateDescription")}</DialogDescription></DialogHeader>
            <div className="space-y-3 mt-2">
              <div className="space-y-1.5"><Label htmlFor="template-create-name" className="text-xs">{t("common.name")}</Label><Input id="template-create-name" className="h-8 text-xs" placeholder="block_exact_flow" value={form.name} onChange={e => setForm(p => ({ ...p, name: e.target.value }))} /></div>
              <div className="space-y-1.5"><Label htmlFor="template-create-description" className="text-xs">{t("common.description")}</Label><Input id="template-create-description" className="h-8 text-xs" placeholder={t("admin.templates.blockExactFlow")} value={form.description} onChange={e => setForm(p => ({ ...p, description: e.target.value }))} /></div>
              <div className="flex items-center justify-between">
                <Label htmlFor="template-create-admin-only" className="text-sm">{t("admin.templates.adminOnly")}</Label>
                <Switch id="template-create-admin-only" checked={form.admin_only} onCheckedChange={v => setForm(p => ({ ...p, admin_only: v }))} />
              </div>
              <Button className="w-full" onClick={createTemplate} disabled={!runtimeSafety.allowed || createMut.isPending || !form.name}>
                {createMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" />}{t("common.create")}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>
      {templatesQ.isLoading ? (
        <div className="space-y-2">{[1, 2].map(i => <Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : !templatesQ.data || templatesQ.data.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center">
          <FileCode2 className="h-10 w-10 text-muted-foreground/40 mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">{t("admin.templates.noTemplates")}</p>
        </div>
      ) : (
        <div className="rounded-xl border border-border bg-card overflow-hidden overflow-x-auto">
          <Table>
            <TableHeader><TableRow className="border-border hover:bg-transparent"><TableHead className="text-xs">{t("common.name")}</TableHead><TableHead className="text-xs">{t("common.description")}</TableHead><TableHead className="text-xs">{t("common.access")}</TableHead><TableHead className="text-xs">{t("common.status")}</TableHead><TableHead className="text-right text-xs">{t("common.actions")}</TableHead></TableRow></TableHeader>
            <TableBody>
              {templatesQ.data.map((template) => (
                <TableRow key={template.id} className="border-border hover:bg-secondary/20">
                  <TableCell className="text-sm font-mono font-medium text-primary">{template.name}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{template.description ?? "—"}</TableCell>
                  <TableCell><Badge variant={template.admin_only ? "destructive" : "secondary"} className="text-xs">{template.admin_only ? t("admin.templates.adminOnly") : t("admin.templates.allUsers")}</Badge></TableCell>
                  <TableCell><Badge variant={template.enabled ? "success" : "secondary"} className="text-xs">{template.enabled ? t("common.active") : t("common.disabled")}</Badge></TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-2">
                      <Button size="sm" variant="outline" className="h-7" disabled={!runtimeSafety.allowed} onClick={() => openEdit(template)}>
                        <Edit3 className="mr-1.5 h-3.5 w-3.5" />{t("admin.templates.editTemplate")}
                      </Button>
                      <Button size="sm" variant="destructive" className="h-7" disabled={!runtimeSafety.allowed} onClick={() => setDeleteTemplate(template)}>
                        <Trash2 className="mr-1.5 h-3.5 w-3.5" />{t("admin.templates.deleteTemplate")}
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog open={!!editingTemplate} onOpenChange={(nextOpen) => { if (!nextOpen) { setEditingTemplate(null); setEditForm(emptyTemplateForm); } }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader><DialogTitle>{t("admin.templates.editTemplate")}</DialogTitle><DialogDescription>{editingTemplate?.name}</DialogDescription></DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5"><Label htmlFor="template-edit-name" className="text-xs">{t("common.name")}</Label><Input id="template-edit-name" className="h-8 text-xs font-mono" value={editForm.name} onChange={e => setEditForm(p => ({ ...p, name: e.target.value }))} /></div>
            <div className="space-y-1.5"><Label htmlFor="template-edit-description" className="text-xs">{t("common.description")}</Label><Input id="template-edit-description" className="h-8 text-xs" value={editForm.description} onChange={e => setEditForm(p => ({ ...p, description: e.target.value }))} /></div>
            <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
              <Label htmlFor="template-edit-admin-only" className="text-xs">{t("admin.templates.adminOnly")}</Label>
              <Switch id="template-edit-admin-only" checked={editForm.admin_only} onCheckedChange={v => setEditForm(p => ({ ...p, admin_only: v }))} />
            </div>
            <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
              <Label htmlFor="template-edit-enabled" className="text-xs">{t("common.status")}</Label>
              <Switch id="template-edit-enabled" checked={editForm.enabled} onCheckedChange={v => setEditForm(p => ({ ...p, enabled: v }))} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setEditingTemplate(null)} disabled={patchMut.isPending}>{t("common.cancel")}</Button>
            <Button size="sm" onClick={submitEdit} disabled={!runtimeSafety.allowed || patchMut.isPending || !editForm.name.trim() || !editForm.description.trim()}>
              {patchMut.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />}{t("common.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!deleteTemplate} onOpenChange={(nextOpen) => { if (!nextOpen) setDeleteTemplate(null); }}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader><DialogTitle>{t("admin.templates.confirmDelete")}</DialogTitle><DialogDescription>{deleteTemplate?.name}</DialogDescription></DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setDeleteTemplate(null)} disabled={deleteMut.isPending}>{t("common.cancel")}</Button>
            <Button variant="destructive" size="sm" onClick={removeTemplate} disabled={!runtimeSafety.allowed || deleteMut.isPending}>
              {deleteMut.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />}{t("admin.templates.deleteTemplate")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
