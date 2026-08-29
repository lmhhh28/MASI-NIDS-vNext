"use client";

import { Camera, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useCreateFlowCapture, useFlowCapture } from "@/hooks/use-flow-capture";
import { useI18n } from "@/lib/i18n";
import type { EventV3Detail, FlowEvidenceV3Summary } from "@/types/api";

interface FlowCapturePanelProps {
  event: EventV3Detail;
  evidence: FlowEvidenceV3Summary | null;
}

export function FlowCapturePanel({ event, evidence }: FlowCapturePanelProps) {
  const { formatDateTime } = useI18n();
  const capture = useCreateFlowCapture();
  const status = useFlowCapture(capture.data?.operation_id);
  const latest = status.data ?? capture.data;
  const switchId = evidence?.switch_id ?? null;
  const disabled = capture.isPending || !switchId;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Camera className="h-4 w-4" aria-hidden="true" />
          Flow capture
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <Fact label="target" value={event.target_uuid} />
          <Fact label="switch" value={switchId ?? "unresolved"} />
          <Fact label="duration" value="3000 ms" />
          <Fact label="sample cap" value="256" />
        </dl>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            className="min-h-10"
            disabled={disabled}
            onClick={() => {
              if (!switchId) return;
              capture.mutate({
                event_id: event.event_id,
                target_uuid: event.target_uuid,
                switch_id: switchId,
                duration_ms: 3000,
                sample_cap: 256,
              });
            }}
          >
            {capture.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Camera aria-hidden="true" />}
            Start capture
          </Button>
          {latest ? <Badge variant={latest.status === "applied" ? "success" : latest.status === "failed" ? "destructive" : "secondary"}>{latest.status}</Badge> : null}
          {latest?.operation_id ? <span className="break-all font-mono text-xs text-muted-foreground">{latest.operation_id}</span> : null}
        </div>
        {!switchId ? <p className="text-xs text-muted-foreground">Capture requires a resolved FlowEvidence switch binding.</p> : null}
        {capture.isError ? <p className="text-xs text-destructive">{capture.error.message}</p> : null}
        {latest?.updated_at ? <p className="text-xs text-muted-foreground">Updated {formatDateTime(latest.updated_at)}</p> : null}
      </CardContent>
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-all font-mono text-xs">{value == null || value === "" ? "-" : String(value)}</dd>
    </div>
  );
}
