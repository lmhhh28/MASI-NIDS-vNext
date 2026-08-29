"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { Plus, Shield, ShieldCheck, ShieldOff } from "lucide-react";

import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAdminUsers } from "@/hooks/use-admin-users";
import type { AdminUser } from "@/types/api";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

const CreateUserDialog = dynamic(() =>
  import("@/components/admin/create-user-dialog").then((module) => module.CreateUserDialog),
);
const UserActions = dynamic(() =>
  import("@/components/admin/user-actions").then((module) => module.UserActions),
);

function isEnabled(user: AdminUser) {
  return user.enabled === true || user.enabled === 1;
}

function RoleBadge({ role }: { role: string }) {
  return (
    <Badge variant={role === "admin" ? "default" : "secondary"} className="text-xs">
      {role}
    </Badge>
  );
}

function StatusBadge({ enabled }: { enabled: boolean }) {
  const { t } = useI18n();
  return (
    <Badge
      variant={enabled ? "outline" : "secondary"}
      className={`text-xs ${enabled ? "border-primary/30 text-primary" : "text-muted-foreground"}`}
    >
      {enabled ? (
        <ShieldCheck className="mr-1 h-3 w-3" aria-hidden />
      ) : (
        <ShieldOff className="mr-1 h-3 w-3" aria-hidden />
      )}
      {enabled ? t("common.active") : t("common.disabled")}
    </Badge>
  );
}

export default function AdminUsersPage() {
  const usersQ = useAdminUsers();
  const [dialogOpen, setDialogOpen] = useState(false);
  const { t, locale, formatDate } = useI18n();
  const runtimeSafety = useRuntimeSafety("admin");

  if (usersQ.isError) {
    return (
      <div className="rounded-xl border border-destructive/30 bg-card p-6 text-sm text-destructive">
        {apiErrorMessage(usersQ.error, locale)}
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-heading font-semibold">{t("admin.users.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("admin.users.subtitle")}</p>
        </div>
        <Button size="sm" className="w-full sm:w-auto" disabled={!runtimeSafety.allowed} onClick={() => { if (runtimeSafety.guard()) setDialogOpen(true); }}>
          <Plus aria-hidden="true" />
          {t("admin.users.addUser")}
        </Button>
      </div>
      {dialogOpen ? <CreateUserDialog onClose={() => setDialogOpen(false)} /> : null}
      {usersQ.isLoading ? (
        <div className="space-y-2">{[1,2,3].map(i=><Skeleton key={i} className="h-12 w-full" />)}</div>
      ) : !usersQ.data || usersQ.data.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center">
          <Shield className="h-10 w-10 text-muted-foreground/40 mx-auto mb-3" />
          <p className="text-sm text-muted-foreground">{t("admin.users.noUsers")}</p>
        </div>
      ) : (
        <>
          <div className="grid gap-3 md:hidden">
            {usersQ.data.map((user) => {
              const enabled = isEnabled(user);
              return (
                <div
                  key={user.id}
                  className={`rounded-xl border border-border bg-card p-4 ${enabled ? "" : "opacity-70"}`}
                >
                  <div className="flex min-w-0 items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="break-all text-sm font-medium">{user.username}</p>
                      <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
                        {user.id}
                      </p>
                    </div>
                    <UserActions user={user} />
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <RoleBadge role={user.role} />
                    <StatusBadge enabled={enabled} />
                  </div>
                  <p className="mt-3 text-xs text-muted-foreground">
                    {t("common.created")}: <span className="font-mono">{formatDate(user.created_at)}</span>
                  </p>
                </div>
              );
            })}
          </div>

          <div className="hidden overflow-hidden rounded-xl border border-border bg-card md:block">
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="text-xs">{t("login.username")}</TableHead>
                  <TableHead className="text-xs">{t("common.role")}</TableHead>
                  <TableHead className="text-xs">{t("common.status")}</TableHead>
                  <TableHead className="text-xs">{t("common.created")}</TableHead>
                  <TableHead className="w-12 text-xs"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {usersQ.data.map((user) => {
                  const enabled = isEnabled(user);
                  return (
                    <TableRow
                      key={user.id}
                      className={`border-border hover:bg-secondary/20 ${enabled ? "" : "opacity-70"}`}
                    >
                      <TableCell className="max-w-[20rem]">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium" title={user.username}>
                            {user.username}
                          </p>
                          <p className="truncate font-mono text-xs text-muted-foreground" title={user.id}>
                            {user.id}
                          </p>
                        </div>
                      </TableCell>
                      <TableCell><RoleBadge role={user.role} /></TableCell>
                      <TableCell><StatusBadge enabled={enabled} /></TableCell>
                      <TableCell className="text-xs text-muted-foreground font-mono">{formatDate(user.created_at)}</TableCell>
                      <TableCell className="text-right"><UserActions user={user} /></TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </>
      )}
    </div>
  );
}
