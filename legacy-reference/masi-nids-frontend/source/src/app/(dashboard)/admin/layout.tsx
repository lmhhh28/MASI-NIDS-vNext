"use client";

import Link from "next/link";
import { ShieldAlert, Loader2 } from "lucide-react";

import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuthHydrated, useAuthStore } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const hydrated = useAuthHydrated();
  const sessionStatus = useAuthStore((s) => s.sessionStatus);
  const user = useAuthStore((s) => s.user);
  const { t } = useI18n();

  if (!hydrated || sessionStatus !== "authenticated") {
    return (
      <div className="flex min-h-[calc(100vh-3rem)] items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin motion-reduce:animate-none text-primary" />
      </div>
    );
  }

  if (user?.role !== "admin") {
    return (
      <div className="flex min-h-[calc(100vh-3rem)] items-center justify-center p-4">
        <Card className="w-full max-w-lg border-border bg-card">
          <CardHeader className="space-y-3 text-center">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-destructive/10 ring-1 ring-destructive/20">
              <ShieldAlert className="h-6 w-6 text-destructive" />
            </div>
            <CardTitle className="text-xl font-semibold">
              {t("common.accessDenied")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 text-center">
            <p className="text-sm text-muted-foreground">
              {t("common.accessDeniedHint")}
            </p>
            <Link href="/" className={buttonVariants({ variant: "outline", className: "w-full" })}>
              {t("common.home")}
            </Link>
          </CardContent>
        </Card>
      </div>
    );
  }

  return children;
}
