"use client";

import { Copy } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import type { AuditLog } from "@/types/api";

export function AuditDetailSheet({
  log,
  onClose,
}: {
  log: AuditLog;
  onClose: () => void;
}) {
  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{log.action}</SheetTitle>
          <SheetDescription>{log.ts}</SheetDescription>
        </SheetHeader>
        <div className="space-y-4 overflow-y-auto px-4 pb-6">
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">request ID</dt>
              <dd className="mt-1 flex items-center gap-1 break-all font-mono text-xs">
                {log.request_id ?? "—"}
                {log.request_id ? (
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label="Copy request ID"
                    onClick={() => navigator.clipboard.writeText(log.request_id!)}
                  >
                    <Copy aria-hidden="true" />
                  </Button>
                ) : null}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">resource</dt>
              <dd className="mt-1 break-all font-mono text-xs">
                {log.resource_type}/{log.resource_id}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">actor</dt>
              <dd className="mt-1 font-mono text-xs">
                {log.actor_user_id ?? "system"} · {log.actor_role}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">channel/status</dt>
              <dd className="mt-1 font-mono text-xs">
                {log.channel} · {log.status}
              </dd>
            </div>
          </dl>
          <div>
            <p className="mb-2 text-sm font-medium">details JSON</p>
            <pre className="overflow-auto rounded-md border bg-muted/30 p-3 text-xs">
              {JSON.stringify(log.details, null, 2)}
            </pre>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
