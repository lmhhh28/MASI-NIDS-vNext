"use client";

import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, UserPlus } from "lucide-react";
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
import api, { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";

type UserRole = "admin" | "analyze";

export function CreateUserDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const { t, locale } = useI18n();
  const runtimeSafety = useRuntimeSafety("admin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("analyze");
  const [formError, setFormError] = useState<string | null>(null);
  const canCreate = username.trim().length > 0 && password.length >= 8;

  const createMutation = useMutation({
    mutationFn: () =>
      api.post("/admin/users", {
        username: username.trim(),
        password,
        role,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
      toast.success(t("admin.users.userCreated"));
      onClose();
    },
    onError: (error) =>
      toast.error(t("common.failed"), {
        description: apiErrorMessage(error, locale),
      }),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!runtimeSafety.guard()) return;
    if (!username.trim()) {
      setFormError(t("validation.usernameRequired"));
      return;
    }
    if (password.length < 8) {
      setFormError(t("admin.users.passwordMinLength"));
      return;
    }
    setFormError(null);
    createMutation.mutate();
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !createMutation.isPending && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("admin.users.createUser")}</DialogTitle>
          <DialogDescription>{t("admin.users.createUserDescription")}</DialogDescription>
        </DialogHeader>
        <form className="mt-2 space-y-4" onSubmit={submit} noValidate>
          <div className="space-y-2">
            <Label htmlFor="admin-user-username">{t("login.username")}</Label>
            <Input
              id="admin-user-username"
              value={username}
              onChange={(event) => {
                setFormError(null);
                setUsername(event.currentTarget.value);
              }}
              placeholder="analyst01"
              autoComplete="username"
              aria-invalid={Boolean(formError && !username.trim())}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="admin-user-password">{t("login.password")}</Label>
            <Input
              id="admin-user-password"
              type="password"
              value={password}
              onChange={(event) => {
                setFormError(null);
                setPassword(event.currentTarget.value);
              }}
              autoComplete="new-password"
              aria-describedby="admin-user-password-hint admin-user-form-error"
              aria-invalid={Boolean(formError && password.length < 8)}
            />
            <p id="admin-user-password-hint" className="text-xs text-muted-foreground">
              {t("admin.users.passwordMinLength")}
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="admin-user-role">{t("common.role")}</Label>
            <select
              id="admin-user-role"
              value={role}
              onChange={(event) => setRole(event.currentTarget.value as UserRole)}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="analyze">{t("audit.roleAnalyze")}</option>
              <option value="admin">{t("audit.roleAdmin")}</option>
            </select>
          </div>
          {formError ? (
            <p id="admin-user-form-error" className="text-xs text-destructive" role="alert">
              {formError}
            </p>
          ) : null}
          <Button type="submit" className="w-full" disabled={!runtimeSafety.allowed || createMutation.isPending || !canCreate}>
            {createMutation.isPending ? (
              <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <UserPlus aria-hidden="true" />
            )}
            {t("common.create")}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
