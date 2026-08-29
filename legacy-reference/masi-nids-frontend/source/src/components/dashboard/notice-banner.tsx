"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import { AlertTriangle, BookOpen } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import type { NoticeItem } from "@/types/api";

const NoticeMarkdown = dynamic(
  () => import("./notice-markdown").then((module) => module.NoticeMarkdown),
  {
    loading: () => (
      <div
        className="h-40 animate-pulse rounded-md bg-muted/40 motion-reduce:animate-none"
        role="status"
        aria-label="Loading notice"
      />
    ),
  },
);

interface NoticeBannerProps {
  notice: NoticeItem;
  title: string;
  confirmLabel: string;
  autoOpen?: boolean;
}

export function NoticeBanner({ notice, title, confirmLabel, autoOpen }: NoticeBannerProps) {
  const [open, setOpen] = useState(autoOpen ?? false);
  const isWarning = notice.category === "server_issues";
  const Icon = isWarning ? AlertTriangle : BookOpen;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "flex w-full items-center gap-2 rounded-md border-l-4 bg-card px-3 py-2 shadow-sm transition-colors hover:bg-card/80 text-left",
          isWarning ? "border-l-warning" : "border-l-success",
        )}
      >
        <Icon
          className={cn(
            "h-4 w-4 shrink-0",
            isWarning ? "text-warning" : "text-success",
          )}
        />
        <span className="text-sm font-medium flex-1">{title}</span>
      </button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <div className="flex items-center gap-2">
              <Icon
                className={cn(
                  "h-5 w-5 shrink-0",
                  isWarning ? "text-warning" : "text-success",
                )}
              />
              <DialogTitle>{title}</DialogTitle>
            </div>
          </DialogHeader>
          <ScrollArea className="max-h-[70vh]">
            <div className="prose prose-sm dark:prose-invert max-w-none py-2">
              {open ? <NoticeMarkdown content={notice.content} /> : null}
            </div>
          </ScrollArea>
          <div className="flex justify-end pt-2">
            <Button onClick={() => setOpen(false)}>{confirmLabel}</Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
