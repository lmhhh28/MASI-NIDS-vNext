"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { Eye, EyeOff, Shield, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { AppearanceControls } from "@/components/layout/appearance-controls";
import { useAuthHydrated, useAuthStore } from "@/lib/auth";
import { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type LoginForm = {
  username: string;
  password: string;
};

export default function LoginPage() {
  const router = useRouter();
  const login = useAuthStore((s) => s.login);
  const sessionStatus = useAuthStore((s) => s.sessionStatus);
  const hydrated = useAuthHydrated();
  const [loading, setLoading] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<Partial<Record<keyof LoginForm, string>>>({});
  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const { t, locale } = useI18n();

  useEffect(() => {
    if (hydrated && sessionStatus === "authenticated") {
      router.replace("/");
    }
  }, [hydrated, router, sessionStatus]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextErrors: Partial<Record<keyof LoginForm, string>> = {};
    if (!username.trim()) nextErrors.username = t("validation.usernameRequired");
    if (!password) nextErrors.password = t("validation.passwordRequired");
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      window.requestAnimationFrame(() => {
        (nextErrors.username ? usernameRef : passwordRef).current?.focus();
      });
      return;
    }

    setLoading(true);
    try {
      const result = await login(username.trim(), password, locale);
      if (result.ok) {
        toast.success(t("login.success"));
        router.replace("/");
      } else {
        toast.error(t("login.failed"), {
          description: apiErrorMessage(result.error, locale),
        });
      }
    } finally {
      setLoading(false);
    }
  }

  if (!hydrated || sessionStatus === "authenticated") {
    return (
      <main
        id="main-content"
        tabIndex={-1}
        className="flex min-h-dvh items-center justify-center focus:outline-none"
        role="status"
      >
        <Loader2 className="h-6 w-6 animate-spin text-primary motion-reduce:animate-none" />
        <span className="sr-only">{t("common.loadingSources")}</span>
      </main>
    );
  }

  return (
    <main id="main-content" tabIndex={-1} className="relative flex min-h-dvh items-center justify-center p-4 focus:outline-none">
      <div className="absolute right-4 top-4">
        <AppearanceControls />
      </div>
      <Card className="w-full max-w-md border-border bg-card">
        <CardHeader className="text-center space-y-3">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-xl bg-primary/10 ring-1 ring-primary/20">
            <Shield className="h-7 w-7 text-primary" />
          </div>
          <CardTitle className="text-2xl font-semibold">
            MASI-NIDS Console
          </CardTitle>
          <CardDescription className="text-muted-foreground">
            {t("login.subtitle")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} noValidate className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">{t("login.username")}</Label>
              <Input
                id="username"
                ref={usernameRef}
                value={username}
                onChange={(event) => {
                  setUsername(event.currentTarget.value);
                  if (errors.username) setErrors((current) => ({ ...current, username: undefined }));
                }}
                placeholder="admin"
                autoComplete="username"
                aria-invalid={Boolean(errors.username)}
                aria-describedby={errors.username ? "username-error" : undefined}
              />
              {errors.username && (
                <p id="username-error" role="alert" className="text-sm font-medium text-destructive">
                  {errors.username}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">{t("login.password")}</Label>
              <div className="relative">
                <Input
                  id="password"
                  ref={passwordRef}
                  value={password}
                  onChange={(event) => {
                    setPassword(event.currentTarget.value);
                    if (errors.password) setErrors((current) => ({ ...current, password: undefined }));
                  }}
                  type={showPassword ? "text" : "password"}
                  placeholder="••••••••"
                  autoComplete="current-password"
                  aria-invalid={Boolean(errors.password)}
                  aria-describedby={errors.password ? "password-error" : undefined}
                  className="pr-12"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="absolute right-0 top-0 min-h-11 min-w-11"
                  onClick={() => setShowPassword((value) => !value)}
                  aria-label={showPassword ? t("login.hidePassword") : t("login.showPassword")}
                  aria-pressed={showPassword}
                >
                  {showPassword ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
                </Button>
              </div>
              {errors.password && (
                <p id="password-error" role="alert" className="text-sm font-medium text-destructive">
                  {errors.password}
                </p>
              )}
            </div>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" />
              )}
              {t("login.signIn")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
