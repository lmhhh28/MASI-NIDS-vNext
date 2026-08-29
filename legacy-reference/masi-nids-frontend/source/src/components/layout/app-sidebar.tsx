"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  GitBranch,
  CheckCircle,
  FileText,
  Network,
  Terminal,
  Settings,
  Shield,
  LogOut,
  Database,
  Radar,
  Users,
  Bot,
  ShieldAlert,
  Crosshair,
  Workflow,
  LayoutTemplate,
  PlayCircle,
} from "lucide-react";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
} from "@/components/ui/sidebar";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { useAuthStore } from "@/lib/auth";
import { useI18n, type TranslationKey } from "@/lib/i18n";

const mainNav = [
  { titleKey: "common.dashboard", url: "/", icon: LayoutDashboard },
  { titleKey: "common.detectionsV3", url: "/detections", icon: Radar },
  { titleKey: "sources.managePage", url: "/sources", icon: Database },
  { titleKey: "common.workflows", url: "/workflows", icon: GitBranch },
  { titleKey: "common.reviews", url: "/reviews", icon: CheckCircle },
  { titleKey: "common.auditLog", url: "/audit", icon: FileText },
  { titleKey: "common.p4Operations", url: "/p4", icon: Network },
  { titleKey: "common.mcpTools", url: "/mcp", icon: Terminal },
] satisfies { titleKey: TranslationKey; url: string; icon: typeof LayoutDashboard }[];

const adminNav = [
  { titleKey: "common.users", url: "/admin/users", icon: Users },
  { titleKey: "common.llmConfig", url: "/admin/llm", icon: Bot },
  { titleKey: "common.riskPolicy", url: "/admin/risk", icon: ShieldAlert },
  { titleKey: "common.protectedTargets", url: "/admin/targets", icon: Crosshair },
  { titleKey: "common.pipelineControlV3", url: "/admin/pipeline", icon: Workflow },
  { titleKey: "common.templates", url: "/admin/templates", icon: LayoutTemplate },
  { titleKey: "common.demoTraffic", url: "/admin/demo", icon: PlayCircle },
] satisfies { titleKey: TranslationKey; url: string; icon: typeof Users }[];

export function AppSidebar() {
  const pathname = usePathname();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const isAdmin = user?.role === "admin";
  const { t } = useI18n();

  return (
    <Sidebar variant="sidebar" collapsible="icon">
      <SidebarHeader className="px-4 pt-4 pb-2 group-data-[collapsible=icon]:items-center group-data-[collapsible=icon]:px-1">
        <Link
          href="/"
          prefetch={false}
          title="MASI-NIDS"
          className="flex items-center gap-2.5 overflow-hidden group-data-[collapsible=icon]:justify-center"
        >
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/15 ring-1 ring-primary/25">
            <Shield className="h-4 w-4 text-primary" />
          </div>
          <div className="grid min-w-0 leading-tight group-data-[collapsible=icon]:hidden">
            <span className="truncate text-sm font-semibold">
              MASI-NIDS
            </span>
            <span className="truncate text-xs text-muted-foreground">
              {t("common.console")}
            </span>
          </div>
        </Link>
      </SidebarHeader>

      <SidebarSeparator />

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>{t("common.navigation")}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {mainNav.map((item) => {
                const title = t(item.titleKey);
                const isActive =
                  item.url === "/"
                    ? pathname === "/"
                    : pathname.startsWith(item.url);
                return (
                  <SidebarMenuItem key={item.url}>
                    <SidebarMenuButton
                      isActive={isActive}
                      tooltip={title}
                      render={<Link href={item.url} prefetch={false} aria-current={isActive ? "page" : undefined} />}
                    >
                      <item.icon className="h-4 w-4" />
                      <span>{title}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {isAdmin && (
          <>
            <SidebarSeparator />
            <SidebarGroup>
              <SidebarGroupLabel>
                <Settings className="mr-1.5 h-3.5 w-3.5" />
                {t("common.admin")}
              </SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {adminNav.map((item) => {
                    const title = t(item.titleKey);
                    const isActive = pathname.startsWith(item.url);
                    return (
                      <SidebarMenuItem key={item.url}>
                        <SidebarMenuButton
                          isActive={isActive}
                          tooltip={title}
                          render={<Link href={item.url} prefetch={false} aria-current={isActive ? "page" : undefined} />}
                        >
                          <item.icon className="h-4 w-4" />
                          <span>{title}</span>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    );
                  })}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </>
        )}
      </SidebarContent>

      <SidebarFooter className="p-3 group-data-[collapsible=icon]:items-center group-data-[collapsible=icon]:p-1">
        <div
          className="flex w-full items-center gap-2.5 overflow-hidden rounded-md p-2 text-sm group-data-[collapsible=icon]:hidden"
          title={t("common.signedInAs", { username: user?.username ?? "—" })}
        >
          <Avatar className="h-7 w-7 shrink-0">
            <AvatarFallback className="bg-primary/15 text-xs font-semibold text-primary">
              {user?.username?.charAt(0).toUpperCase() ?? "?"}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1 leading-tight text-left">
            <span className="block truncate text-sm font-medium">
              {user?.username ?? "—"}
            </span>
            <Badge
              variant={isAdmin ? "default" : "secondary"}
              className="h-5 w-fit px-1.5 text-xs"
            >
              {user?.role ?? "—"}
            </Badge>
          </div>
          <button
            type="button"
            aria-label={t("common.signOut")}
            title={t("common.signOut")}
            onClick={async () => {
              await logout();
              window.location.href = "/login";
            }}
            className="ml-auto inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11"
          >
            <LogOut className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <button
          type="button"
          aria-label={t("common.signOut")}
          title={`${t("common.signOut")} · ${user?.username ?? "—"}`}
          onClick={async () => {
            await logout();
            window.location.href = "/login";
          }}
          className="hidden size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring group-data-[collapsible=icon]:inline-flex"
        >
          <LogOut className="h-4 w-4" aria-hidden="true" />
        </button>
      </SidebarFooter>
    </Sidebar>
  );
}
