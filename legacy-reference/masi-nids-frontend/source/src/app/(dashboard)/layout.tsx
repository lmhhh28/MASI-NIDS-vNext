"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/layout/app-sidebar";
import { AppHeader } from "@/components/layout/app-header";
import { NoticeBanners } from "@/components/dashboard/notice-banners";
import { MaintenanceBanners } from "@/components/dashboard/maintenance-banners";
import { OperationRegistryGuard } from "@/components/dashboard/operation-registry-guard";
import { QueryProvider } from "@/lib/query-provider";
import { useAuthHydrated, useAuthStore } from "@/lib/auth";
import { RouteFocus } from "@/components/layout/route-focus";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const hydrated = useAuthHydrated();
  const sessionStatus = useAuthStore((s) => s.sessionStatus);

  // Auth guard — redirect to login when no token
  useEffect(() => {
    if (hydrated && sessionStatus === "unauthenticated") {
      router.replace("/login");
    }
  }, [hydrated, router, sessionStatus]);

  // Don't render dashboard until authenticated
  if (!hydrated || sessionStatus !== "authenticated") {
    return (
      <div className="flex min-h-dvh items-center justify-center" role="status">
        <div className="h-8 w-8 animate-spin motion-reduce:animate-none rounded-full border-2 border-primary border-t-transparent" />
        <span className="sr-only">正在验证会话…</span>
      </div>
    );
  }

  return (
    <QueryProvider>
      <SidebarProvider>
        <RouteFocus />
        <AppSidebar />
        <SidebarInset>
          <AppHeader />
          <MaintenanceBanners />
          <OperationRegistryGuard />
          <NoticeBanners />
          <main
            id="main-content"
            tabIndex={-1}
            className="min-h-0 min-w-0 flex-1 overflow-auto overscroll-contain p-4 focus:outline-none md:p-6"
          >
            {children}
          </main>
        </SidebarInset>
      </SidebarProvider>
    </QueryProvider>
  );
}
