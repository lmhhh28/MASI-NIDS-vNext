"use client";

import * as React from "react";
import { PanelLeftIcon } from "lucide-react";

import { useIsMobile } from "@/hooks/use-mobile";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const SIDEBAR_COOKIE_NAME = "sidebar_state";
const SIDEBAR_COOKIE_MAX_AGE = 60 * 60 * 24 * 7;
const SIDEBAR_KEYBOARD_SHORTCUT = "b";

interface SidebarContextValue {
  state: "expanded" | "collapsed";
  open: boolean;
  setOpen: (open: boolean) => void;
  openMobile: boolean;
  setOpenMobile: (open: boolean) => void;
  isMobile: boolean;
  toggleSidebar: () => void;
}

const SidebarContext = React.createContext<SidebarContextValue | null>(null);

function useSidebar() {
  const context = React.useContext(SidebarContext);
  if (!context) throw new Error("useSidebar must be used within SidebarProvider");
  return context;
}

function SidebarProvider({
  defaultOpen = true,
  className,
  children,
  ...props
}: React.ComponentProps<"div"> & { defaultOpen?: boolean }) {
  const isMobile = useIsMobile();
  const [open, setOpenState] = React.useState(defaultOpen);
  const [openMobile, setOpenMobile] = React.useState(false);

  const setOpen = React.useCallback((nextOpen: boolean) => {
    setOpenState(nextOpen);
    document.cookie = `${SIDEBAR_COOKIE_NAME}=${nextOpen}; path=/; max-age=${SIDEBAR_COOKIE_MAX_AGE}`;
  }, []);
  const toggleSidebar = React.useCallback(() => {
    if (isMobile) setOpenMobile((current) => !current);
    else setOpen(!open);
  }, [isMobile, open, setOpen]);

  React.useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === SIDEBAR_KEYBOARD_SHORTCUT && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        toggleSidebar();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleSidebar]);

  const value = React.useMemo<SidebarContextValue>(() => ({
    state: open ? "expanded" : "collapsed",
    open,
    setOpen,
    openMobile,
    setOpenMobile,
    isMobile,
    toggleSidebar,
  }), [isMobile, open, openMobile, setOpen, toggleSidebar]);

  return (
    <SidebarContext.Provider value={value}>
      <div
        data-slot="sidebar-wrapper"
        className={cn("group/sidebar-wrapper flex h-dvh min-h-0 w-full overflow-hidden", className)}
        {...props}
      >
        {children}
      </div>
    </SidebarContext.Provider>
  );
}

function MobileSidebar({
  children,
  dir,
  side,
}: {
  children: React.ReactNode;
  dir?: string;
  side: "left" | "right";
}) {
  const { openMobile, setOpenMobile } = useSidebar();
  const { t } = useI18n();
  const dialogRef = React.useRef<HTMLDialogElement>(null);

  React.useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (openMobile && !dialog.open) dialog.showModal();
    if (!openMobile && dialog.open) dialog.close();
  }, [openMobile]);

  function closeFromBackdrop(event: React.MouseEvent<HTMLDialogElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (
      event.clientX < bounds.left ||
      event.clientX > bounds.right ||
      event.clientY < bounds.top ||
      event.clientY > bounds.bottom
    ) {
      setOpenMobile(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      dir={dir}
      aria-label={t("common.sidebar")}
      onCancel={(event) => {
        event.preventDefault();
        setOpenMobile(false);
      }}
      onClose={() => openMobile && setOpenMobile(false)}
      onClick={closeFromBackdrop}
      className={`fixed inset-y-0 m-0 h-dvh max-h-none w-72 max-w-[85vw] border-0 bg-sidebar p-0 text-sidebar-foreground shadow-xl backdrop:bg-black/50 ${
        side === "left" ? "left-0" : "right-0 left-auto"
      }`}
    >
      <button
        type="button"
        aria-label={t("common.close")}
        onClick={() => setOpenMobile(false)}
        className="absolute right-2 top-2 z-10 inline-flex min-h-11 min-w-11 items-center justify-center rounded-md text-xl text-muted-foreground hover:bg-sidebar-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span aria-hidden="true">×</span>
      </button>
      <div className="flex h-full w-full flex-col">{children}</div>
    </dialog>
  );
}

function Sidebar({
  side = "left",
  className,
  children,
  dir,
}: React.ComponentProps<"div"> & {
  side?: "left" | "right";
  variant?: "sidebar" | "floating" | "inset";
  collapsible?: "offcanvas" | "icon" | "none";
}) {
  const { isMobile, state } = useSidebar();
  if (isMobile) return <MobileSidebar dir={dir} side={side}>{children}</MobileSidebar>;

  return (
    <aside
      data-slot="sidebar"
      data-state={state}
      data-collapsible={state === "collapsed" ? "icon" : ""}
      data-side={side}
      className={cn(
        "group peer sticky top-0 hidden h-dvh shrink-0 flex-col overflow-hidden border-sidebar-border bg-sidebar text-sidebar-foreground transition-[width] duration-200 md:flex",
        side === "left" ? "border-r" : "border-l",
        state === "collapsed" ? "w-12" : "w-64",
        className,
      )}
    >
      {children}
    </aside>
  );
}

function SidebarInset({ className, ...props }: React.ComponentProps<"main">) {
  return (
    <main
      data-slot="sidebar-inset"
      className={cn(
        "relative flex h-dvh min-h-0 min-w-0 w-full flex-1 flex-col overflow-hidden bg-background",
        className,
      )}
      {...props}
    />
  );
}

function SidebarTrigger({ className, ...props }: React.ComponentProps<"button">) {
  const { toggleSidebar } = useSidebar();
  return (
    <button
      type="button"
      data-slot="sidebar-trigger"
      aria-label="Toggle Sidebar"
      title="Toggle Sidebar"
      onClick={toggleSidebar}
      className={cn(
        "inline-flex min-h-9 min-w-9 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11",
        className,
      )}
      {...props}
    >
      <PanelLeftIcon className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

function SidebarHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="sidebar-header" className={cn("flex flex-col gap-2 p-2", className)} {...props} />;
}

function SidebarFooter({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="sidebar-footer" className={cn("flex flex-col gap-2 p-2", className)} {...props} />;
}

function SidebarContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-content"
      className={cn(
        "flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto",
        className,
      )}
      {...props}
    />
  );
}

function SidebarSeparator({ className, ...props }: React.ComponentProps<"div">) {
  return <div role="separator" className={cn("mx-2 h-px bg-sidebar-border", className)} {...props} />;
}

function SidebarGroup({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "relative flex w-full min-w-0 flex-col p-2 group-data-[collapsible=icon]:items-center group-data-[collapsible=icon]:px-1",
        className,
      )}
      {...props}
    />
  );
}

function SidebarGroupLabel({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex h-8 shrink-0 items-center rounded-md px-2 text-xs font-medium text-sidebar-foreground/70 group-data-[collapsible=icon]:pointer-events-none group-data-[collapsible=icon]:-mt-8 group-data-[collapsible=icon]:h-0 group-data-[collapsible=icon]:overflow-hidden group-data-[collapsible=icon]:opacity-0 group-data-[collapsible=icon]:px-0",
        className,
      )}
      {...props}
    />
  );
}

function SidebarGroupContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("w-full text-sm", className)} {...props} />;
}

function SidebarMenu({ className, ...props }: React.ComponentProps<"ul">) {
  return <ul className={cn("flex w-full min-w-0 flex-col", className)} {...props} />;
}

function SidebarMenuItem({ className, ...props }: React.ComponentProps<"li">) {
  return <li className={cn("relative", className)} {...props} />;
}

function SidebarMenuButton({
  render,
  isActive = false,
  tooltip,
  className,
  children,
}: {
  render?: React.ReactElement<Record<string, unknown>>;
  isActive?: boolean;
  tooltip?: string;
  className?: string;
  children?: React.ReactNode;
}) {
  const classes = cn(
    "flex h-9 w-full min-w-0 items-center gap-2 overflow-hidden rounded-md px-2 text-left text-sm outline-none transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring group-data-[collapsible=icon]:mx-auto group-data-[collapsible=icon]:size-8! group-data-[collapsible=icon]:min-h-8 group-data-[collapsible=icon]:min-w-8 group-data-[collapsible=icon]:max-w-8 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:gap-0 group-data-[collapsible=icon]:px-0! group-data-[collapsible=icon]:py-0 [&_svg]:size-4 [&_svg]:shrink-0 group-data-[collapsible=icon]:[&>span]:sr-only [&>span]:truncate",
    isActive && "bg-sidebar-accent font-medium text-sidebar-accent-foreground",
    className,
  );
  if (render) {
    return React.cloneElement(render, {
      className: cn(render.props.className as string | undefined, classes),
      title: tooltip,
      children,
      "data-active": isActive || undefined,
    });
  }
  return <button type="button" className={classes} title={tooltip}>{children}</button>;
}

export {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarSeparator,
  SidebarTrigger,
  useSidebar,
};
