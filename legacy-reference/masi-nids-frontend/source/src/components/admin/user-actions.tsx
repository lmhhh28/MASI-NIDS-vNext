"use client";

import { useState } from "react";
import { Loader2, MoreHorizontal, ShieldCheck, ShieldOff, UserCog } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { usePatchAdminUser } from "@/hooks/use-admin-users";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { apiErrorMessage, parseApiError } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import type { AdminUser, AdminUserPatch } from "@/types/api";

type Change = Pick<AdminUserPatch, "enabled" | "role">;

export function UserActions({ user }: { user: AdminUser }) {
  const { t, locale } = useI18n();
  const currentUser = useAuthStore((state) => state.user);
  const runtimeSafety = useRuntimeSafety("admin");
  const patchMutation = usePatchAdminUser(user.id);
  const [menuOpen, setMenuOpen] = useState(false);
  const [pendingChange, setPendingChange] = useState<Change | null>(null);
  const [reason, setReason] = useState("");
  const enabled = Boolean(user.enabled);
  const isSelf = currentUser?.id === user.id;
  const runtimeUnknown = !runtimeSafety.allowed;
  const destructive = pendingChange?.enabled === false || pendingChange?.role === "analyze";

  function prepare(change: Change) {
    setPendingChange(change);
    setReason("");
    setMenuOpen(false);
  }

  function submit() {
    if (!runtimeSafety.guard()) return;
    if (!pendingChange || reason.trim().length < 3) return;
    patchMutation.mutate({
      ...pendingChange,
      expected_enabled: enabled,
      expected_role: user.role as "admin" | "analyze",
      change_reason: reason.trim(),
    }, {
      onSuccess: () => {
        toast.success(pendingChange.role ? t("admin.users.roleChanged") : t("admin.users.statusChanged"));
        setPendingChange(null);
      },
      onError: (error) => {
        const info = parseApiError(error, locale);
        if (info.code === "LAST_ADMIN_REQUIRED") toast.error(t("admin.users.lastAdminBlocked"));
        else if (info.code === "USER_STATE_CONFLICT") toast.error(t("admin.users.stateConflict"));
        else toast.error(t("common.failed"), { description: apiErrorMessage(info, locale) });
      },
    });
  }

  return (
    <>
      <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
        <DropdownMenuTrigger render={<Button size="icon" variant="ghost" aria-label={t("admin.users.edit")} disabled={patchMutation.isPending || runtimeUnknown}>{patchMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <MoreHorizontal aria-hidden="true" />}</Button>} />
        <DropdownMenuContent align="end" className="w-48">
          <DropdownMenuGroup>
            <DropdownMenuLabel>{t("admin.users.edit")}</DropdownMenuLabel>
            <DropdownMenuItem onClick={() => prepare({ enabled: !enabled })}>{enabled ? <ShieldOff aria-hidden="true" /> : <ShieldCheck aria-hidden="true" />}{enabled ? t("admin.users.disable") : t("admin.users.enable")}</DropdownMenuItem>
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuGroup>
            <DropdownMenuLabel>{t("common.role")}</DropdownMenuLabel>
            <DropdownMenuItem disabled={user.role === "admin"} onClick={() => prepare({ role: "admin" })}><UserCog aria-hidden="true" />admin</DropdownMenuItem>
            <DropdownMenuItem disabled={user.role === "analyze"} onClick={() => prepare({ role: "analyze" })}><UserCog aria-hidden="true" />analyze</DropdownMenuItem>
          </DropdownMenuGroup>
          {isSelf ? <><DropdownMenuSeparator /><p className="px-2 py-1 text-xs text-muted-foreground">{t("admin.users.currentUserNote")}</p></> : null}
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={pendingChange !== null} onOpenChange={(open) => !patchMutation.isPending && !open && setPendingChange(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("admin.users.confirmChange")}</DialogTitle>
            <DialogDescription>{isSelf && destructive ? t("admin.users.selfChangeConsequence") : destructive ? t("admin.users.destructiveConsequence") : t("admin.users.changeConsequence")}</DialogDescription>
          </DialogHeader>
          <div className="rounded-md border p-3 font-mono text-sm">{user.username}: {user.role}/{String(enabled)} → {pendingChange?.role ?? user.role}/{String(pendingChange?.enabled ?? enabled)}</div>
          <div className="space-y-2"><Label htmlFor={`user-change-reason-${user.id}`}>{t("admin.users.changeReason")}</Label><Textarea id={`user-change-reason-${user.id}`} value={reason} onChange={(event) => setReason(event.target.value)} /></div>
          <DialogFooter><Button variant="outline" disabled={patchMutation.isPending} onClick={() => setPendingChange(null)}>{t("common.cancel")}</Button><Button variant={destructive ? "destructive" : "default"} disabled={patchMutation.isPending || reason.trim().length < 3 || runtimeUnknown} onClick={submit}>{patchMutation.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}{t("common.save")}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
