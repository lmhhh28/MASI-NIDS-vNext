"use client";

import { use, useState, useCallback, useMemo, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  ArrowLeft,
  Camera,
  ChevronDown,
  ChevronRight,
  Loader2,
  RotateCcw,
  Send,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";

import api, { apiErrorMessage, parseApiError } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { useDeployments, useSwitch } from "@/hooks/use-p4";
import { useRuntimeSafety } from "@/hooks/use-runtime-safety";
import { useIdempotentOperation } from "@/hooks/use-idempotent-operation";
import { useI18n } from "@/lib/i18n";
import { newIdempotencyKey, useOperationRegistry } from "@/lib/operations";
import { asPositiveInteger } from "@/lib/utils";
import { CreateRestoreRuleWorkflowDialog } from "@/components/p4/create-restore-rule-workflow-dialog";
import { P4OperationRecovery } from "@/components/p4/p4-operation-recovery";
import { ErrorState } from "@/components/async-state";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import type {
  P4Table,
  P4FormSchema,
  P4EntriesResponse,
  P4Deployment,
  P4DeploymentResponse,
  P4ValidationResponse,
  P4Switch,
} from "@/types/api";

/* ── Data hooks ── */
function useTables(switchId: string, enabled: boolean) {
  return useQuery<P4Table[]>({
    queryKey: ["p4-tables", switchId],
    queryFn: ({ signal }) => api.get(`/p4/switches/${switchId}/tables`, { signal }).then((r) => r.data),
    enabled,
  });
}

function useEntries(switchId: string, tableName: string | null, enabled: boolean) {
  return useQuery<P4EntriesResponse>({
    queryKey: ["p4-entries", switchId, tableName],
    queryFn: ({ signal }) =>
      api.get(`/p4/switches/${switchId}/tables/${tableName}/entries`, { signal }).then((r) => r.data),
    enabled: enabled && !!tableName,
    refetchInterval: 15_000,
  });
}

function useFormSchema(switchId: string, tableName: string | null, enabled: boolean) {
  return useQuery<P4FormSchema>({
    queryKey: ["p4-form-schema", switchId, tableName],
    queryFn: ({ signal }) =>
      api.get(`/p4/switches/${switchId}/tables/${tableName}/form-schema`, { signal }).then((r) => r.data),
    enabled: enabled && !!tableName,
  });
}

function stableStringify(value: unknown): string {
  if (value === undefined) return "null";
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== undefined)
    .sort(([left], [right]) => left.localeCompare(right));
  return `{${entries
    .map(([key, item]) => `${JSON.stringify(key)}:${stableStringify(item)}`)
    .join(",")}}`;
}

function deploymentWriterMode(deployment: P4Deployment): string {
  return String(deployment.result?.writer_mode ?? "backend_known_only");
}

function deploymentVerificationScope(deployment: P4Deployment): string {
  return String(deployment.result?.verification_scope ?? "read_rpc_unverified");
}

function DeploymentStatusBadge({ status }: { status: string }) {
  const variant = status === "applied"
    ? "success"
    : status === "unknown" || status === "needs_reconcile"
      ? "warning"
      : "destructive";
  return <Badge variant={variant}>{status}</Badge>;
}

/* ── Table schema viewer ── */
function TableSchemaCard({ table }: { table: P4Table }) {
  const [open, setOpen] = useState(false);
  const { t } = useI18n();
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="flex w-full items-center justify-between rounded-lg border border-border bg-secondary/20 px-4 py-3 text-left hover:bg-secondary/40 transition-colors">
        <div className="flex items-center gap-3">
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          <span className="text-sm font-mono font-medium">{table.name}</span>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="text-xs">
            {t("common.countFields", { count: table.match_fields?.length ?? 0 })}
          </Badge>
          <Badge variant="outline" className="text-xs">
            {t("common.countActions", { count: table.actions?.length ?? 0 })}
          </Badge>
        </div>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1 rounded-lg border border-border bg-card p-4 space-y-3">
        <div>
          <p className="text-xs font-semibold text-muted-foreground mb-2">{t("p4.matchFields")}</p>
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-transparent">
                  <TableHead className="text-xs">{t("common.name")}</TableHead>
                  <TableHead className="text-xs">{t("p4.fieldType")}</TableHead>
                  <TableHead className="text-xs text-right">{t("p4.bitwidth")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(table.match_fields ?? []).map((f) => (
                  <TableRow key={f.id} className="border-border">
                    <TableCell className="text-xs font-mono">{f.name}</TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="text-xs">
                        {f.match_type}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-right font-mono">{f.bitwidth}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
        <div>
          <p className="text-xs font-semibold text-muted-foreground mb-2">{t("common.actions")}</p>
          {(table.actions ?? []).map((a) => (
            <div key={a.id} className="flex items-center gap-2 py-1">
              <span className="text-xs font-mono text-primary">{a.name}</span>
              {a.params && a.params.length > 0 && (
                <span className="text-xs text-muted-foreground">
                  ({a.params.map((p) => `${p.name}:${p.bitwidth}`).join(", ")})
                </span>
              )}
            </div>
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/* ── Dynamic Entry Form ── */
function EntryForm({
  switchId,
  tableName,
  schema,
  switchState,
}: {
  switchId: string;
  tableName: string;
  schema: P4FormSchema;
  switchState: P4Switch;
}) {
  const qc = useQueryClient();
  const isAdmin = useAuthStore((s) => s.isAdmin)();
  const writesEnabled = switchState.writes_enabled;
  const { t, locale } = useI18n();
  const runtimeSafety = useRuntimeSafety("p4");
  const p4Maintenance = !runtimeSafety.allowed;

  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [actionName, setActionName] = useState(schema.actions[0]?.name ?? "");
  const [operation, setOperation] = useState("insert");
  const [ttl, setTtl] = useState("");
  const [actionParams, setActionParams] = useState<Record<string, string>>({});
  const [validated, setValidated] = useState<P4ValidationResponse | null>(null);
  const [confirmApply, setConfirmApply] = useState(false);
  const pendingApplyKey = useRef<{ signature: string; key: string } | null>(null);
  const unresolvedApply = useOperationRegistry((state) =>
    Object.values(state.operations).some(
      (item) => item.kind === "p4.apply"
        && item.target === `${switchId}:${tableName}`
        && ["prepared", "submitting", "outcome_unknown"].includes(item.status),
    ),
  );
  const clearApplyKey = useCallback(() => {
    const key = pendingApplyKey.current?.key;
    const operationState = key ? useOperationRegistry.getState().operations[key]?.status : undefined;
    if (operationState && ["prepared", "submitting", "outcome_unknown"].includes(operationState)) return;
    pendingApplyKey.current = null;
  }, []);
  const ttlDraft = ttl.trim();
  const ttlSeconds = ttlDraft === "" ? null : asPositiveInteger(ttlDraft);
  const ttlInvalid = ttlDraft !== "" && ttlSeconds == null;

  const validateMut = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      api
        .post(`/p4/switches/${switchId}/tables/${tableName}/entries:validate`, payload)
        .then((r) => r.data as P4ValidationResponse),
  });

  const operationOptions = useMemo(() => ({
    kind: "p4.apply",
    target: () => `${switchId}:${tableName}`,
    execute: (payload: Record<string, unknown>, key: string) =>
      api
        .post<P4DeploymentResponse>(`/p4/switches/${switchId}/tables/${tableName}/entries`, payload, {
          headers: { "Idempotency-Key": key },
        })
        .then((response) => response.data),
    resultStatus: (result: P4DeploymentResponse) => result.status,
  }), [switchId, tableName]);
  const applyOperation = useIdempotentOperation(operationOptions);

  const applyMut = useMutation({
    retry: false,
    mutationFn: async (payload: Record<string, unknown>) => {
      const signature = stableStringify({ switchId, tableName, payload });
      if (!pendingApplyKey.current || pendingApplyKey.current.signature !== signature) {
        pendingApplyKey.current = { signature, key: newIdempotencyKey("p4-apply") };
      }
      return applyOperation.run(payload, pendingApplyKey.current.key);
    },
    onSuccess: ({ result }) => {
      setConfirmApply(false);
      qc.invalidateQueries({ queryKey: ["p4-entries", switchId, tableName] });
      qc.invalidateQueries({ queryKey: ["p4-deployments"] });
      if (["unknown", "needs_reconcile"].includes(result.status)) {
        toast.warning(t("p4.operationOutcomeUnknown"));
        return;
      }
      toast.success(t("p4.entryApplied"));
      clearApplyKey();
      setValidated(null);
      setFormValues({});
    },
    onError: (error) => {
      setConfirmApply(false);
      const key = pendingApplyKey.current?.key;
      const outcome = key ? useOperationRegistry.getState().operations[key]?.status : undefined;
      if (outcome === "outcome_unknown") {
        toast.warning(t("p4.operationOutcomeUnknown"), { description: key });
        return;
      }
      clearApplyKey();
      toast.error(t("p4.applyFailed"), { description: apiErrorMessage(error, locale) });
    },
  });

  const buildPayload = useCallback(() => {
    const matchFields: Record<string, unknown> = {};
    for (const field of schema.match_fields) {
      if (formValues[field.name]) matchFields[field.name] = formValues[field.name];
    }
    const payload: Record<string, unknown> = {
      operation,
      match_fields: matchFields,
      action_name: actionName,
      action_params: Object.fromEntries(
        Object.entries(actionParams).filter(([, value]) => value !== "")
      ),
    };
    if (ttlSeconds != null) payload.ttl_seconds = ttlSeconds;
    return payload;
  }, [formValues, actionName, actionParams, operation, ttlSeconds, schema]);

  function handleValidate() {
    if (ttlInvalid) {
      setValidated(null);
      toast.error(t("p4.ttlInvalid"));
      return;
    }
    const payload = buildPayload();
    validateMut.mutate(payload, {
      onSuccess: (data) => {
        setValidated(data);
        if (data.valid) {
          toast.success(t("p4.validationPassed"));
        } else {
          toast.error(t("p4.validationFailed"), {
            description: data.errors?.join("; "),
          });
        }
      },
      onError: (e) =>
        toast.error(t("p4.validationError"), {
          description: apiErrorMessage(e, locale),
        }),
    });
  }

  function handleApply() {
    if (!runtimeSafety.guard()) return;
    if (ttlInvalid) {
      setValidated(null);
      toast.error(t("p4.ttlInvalid"));
      return;
    }
    setConfirmApply(true);
  }

  const selectedAction = useMemo(
    () => schema.actions.find((a) => a.name === actionName),
    [schema.actions, actionName]
  );

  return (
    <Card className="border-border bg-card">
      <CardHeader>
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {t("p4.entryForm", { table: tableName })}
        </CardTitle>
        <p className="font-mono text-xs text-muted-foreground mt-1">
          {t("p4.directManualEntrySubtitle")}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-2">
            <Label htmlFor="p4-entry-operation" className="text-xs">{t("p4.operation")}</Label>
            <Select value={operation} onValueChange={(v) => { if (v) { setOperation(v); setValidated(null); clearApplyKey(); } }}>
              <SelectTrigger id="p4-entry-operation" className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="insert">{t("p4.operationInsert")}</SelectItem>
                <SelectItem value="modify">{t("p4.operationModify")}</SelectItem>
                <SelectItem value="delete">{t("p4.operationDelete")}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="p4-entry-action" className="text-xs">{t("p4.action")}</Label>
            <Select
              value={actionName}
              onValueChange={(v) => {
                if (v) {
                  setActionName(v);
                  setActionParams({});
                  setValidated(null);
                  clearApplyKey();
                }
              }}
            >
              <SelectTrigger id="p4-entry-action" className="h-8 text-xs font-mono">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {schema.actions.map((a) => (
                  <SelectItem key={a.name} value={a.name} className="font-mono text-xs">
                    {a.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div>
          <p className="text-xs font-semibold text-muted-foreground mb-2">{t("p4.matchFields")}</p>
          <div className="grid grid-cols-2 gap-3">
            {schema.match_fields.map((f) => (
              <div key={f.name} className="space-y-1">
                <Label htmlFor={`p4-match-${f.name}`} className="text-xs font-mono">
                  {f.name}
                  <Badge variant="outline" className="ml-1.5 text-xs">
                    {f.widget}
                  </Badge>
                </Label>
                <Input
                  id={`p4-match-${f.name}`}
                  className="h-8 text-xs font-mono"
                  placeholder={
                    f.widget === "ipv4"
                      ? "10.0.0.1"
                      : f.widget === "port"
                        ? "80"
                        : f.widget === "mac"
                          ? "aa:bb:cc:dd:ee:ff"
                          : "0"
                  }
                  value={formValues[f.name] ?? ""}
                  onChange={(e) => {
                    setFormValues((prev) => ({ ...prev, [f.name]: e.target.value }));
                    setValidated(null);
                    clearApplyKey();
                  }}
                />
              </div>
            ))}
          </div>
        </div>

        {selectedAction && selectedAction.params && selectedAction.params.length > 0 && (
          <div>
            <p className="text-xs font-semibold text-muted-foreground mb-2">{t("p4.actionParams")}</p>
            <div className="grid grid-cols-2 gap-3">
              {selectedAction.params.map((p) => (
                <div key={p.name} className="space-y-1">
                  <Label htmlFor={`p4-param-${p.name}`} className="text-xs font-mono">{p.name}</Label>
                  <Input
                    id={`p4-param-${p.name}`}
                    className="h-8 text-xs font-mono"
                    placeholder="0"
                    value={actionParams[p.name] ?? ""}
                    onChange={(e) => {
                      setActionParams((prev) => ({ ...prev, [p.name]: e.target.value }));
                      setValidated(null);
                      clearApplyKey();
                    }}
                  />
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="space-y-2">
          <Label className="text-xs" htmlFor="p4-entry-ttl">{t("p4.ttl")}</Label>
          <Input
            id="p4-entry-ttl"
            className="h-8 text-xs font-mono w-32"
            type="number"
            inputMode="numeric"
            placeholder="300"
            value={ttl}
            aria-invalid={ttlInvalid}
            aria-describedby="p4-entry-ttl-error"
            onChange={(e) => { setTtl(e.target.value); setValidated(null); clearApplyKey(); }}
          />
          {ttlInvalid && (
            <p id="p4-entry-ttl-error" className="text-xs text-destructive" role="alert" aria-live="polite">
              {t("p4.ttlInvalid")}
            </p>
          )}
        </div>

        {validated && (
          <div
            className={`rounded-lg border p-3 text-xs font-mono ${
              validated.valid
                ? "border-primary/30 bg-primary/5 text-primary"
                : "border-destructive/30 bg-destructive/5 text-destructive"
            }`}
          >
            {validated.valid
              ? `✓ ${t("p4.validationPassed")}`
              : `✗ ${validated.errors?.join("; ") ?? t("p4.validationFailed")}`}
          </div>
        )}

        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-8 text-xs"
            onClick={handleValidate}
            disabled={validateMut.isPending || ttlInvalid}
          >
            {validateMut.isPending ? (
              <Loader2 className="mr-1.5 h-3 w-3 animate-spin motion-reduce:animate-none" />
            ) : (
              <ShieldCheck className="mr-1.5 h-3 w-3" />
            )}
            {t("p4.validate")}
          </Button>
          <Button
            size="sm"
            className="h-8 text-xs"
            onClick={handleApply}
            disabled={p4Maintenance || unresolvedApply || !isAdmin || !writesEnabled || !validated?.valid || applyMut.isPending || ttlInvalid}
          >
            {applyMut.isPending ? (
              <Loader2 className="mr-1.5 h-3 w-3 animate-spin motion-reduce:animate-none" />
            ) : (
              <Send className="mr-1.5 h-3 w-3" />
            )}
            {t("p4.apply")}
          </Button>
          {!isAdmin && (
            <span className="text-xs text-muted-foreground">{t("common.adminOnly")}</span>
          )}
          {isAdmin && !writesEnabled && (
            <span className="text-xs text-warning">{t("common.writesDisabled")}</span>
          )}
          {unresolvedApply ? (
            <span className="text-xs text-warning">{t("p4.operationOutcomeUnknown")}</span>
          ) : null}
        </div>
        <Dialog open={confirmApply} onOpenChange={(open) => !applyMut.isPending && setConfirmApply(open)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("p4.apply")}</DialogTitle>
              <DialogDescription>{t("p4.directManualEntrySubtitle")}</DialogDescription>
            </DialogHeader>
            <div className="grid gap-2 text-sm sm:grid-cols-2">
              <p><span className="text-muted-foreground">switch</span><br /><span className="font-mono">{switchId}</span></p>
              <p><span className="text-muted-foreground">table</span><br /><span className="break-all font-mono">{tableName}</span></p>
              <p><span className="text-muted-foreground">operation</span><br /><span className="font-mono">{operation}</span></p>
              <p><span className="text-muted-foreground">TTL</span><br /><span className="font-mono">{ttlSeconds ?? "—"}</span></p>
            </div>
            <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs">{JSON.stringify(validated?.normalized_entry ?? buildPayload(), null, 2)}</pre>
            <p className="break-all font-mono text-xs text-muted-foreground">expected hash: {validated?.expected_post_state_hash ?? "—"}</p>
            <DialogFooter>
              <Button variant="outline" disabled={applyMut.isPending} onClick={() => setConfirmApply(false)}>{t("common.cancel")}</Button>
              <Button disabled={applyMut.isPending || p4Maintenance || unresolvedApply} onClick={() => { if (runtimeSafety.guard()) applyMut.mutate(buildPayload()); }}>
                {applyMut.isPending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Send aria-hidden="true" />}
                {t("p4.apply")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}

/* ── Main Switch Detail Page ── */
export default function SwitchDetailPage({
  params,
}: {
  params: Promise<{ switchId: string }>;
}) {
  const { switchId } = use(params);
  const switchQ = useSwitch(switchId);
  const switchFound = Boolean(switchQ.data);
  const tablesQ = useTables(switchId, switchFound);
  const [deploymentPage, setDeploymentPage] = useState(1);
  const [deploymentStatus, setDeploymentStatus] = useState("");
  const deploymentLimit = 25;
  const deploymentsQ = useDeployments({
    switchId,
    status: deploymentStatus,
    limit: deploymentLimit,
    offset: (deploymentPage - 1) * deploymentLimit,
  });
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const entriesQ = useEntries(switchId, selectedTable, switchFound);
  const formSchemaQ = useFormSchema(switchId, selectedTable, switchFound);
  const isAdmin = useAuthStore((s) => s.isAdmin)();
  const [rollbackTarget, setRollbackTarget] = useState<P4Deployment | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<P4Deployment | null>(null);
  const rollbackKeys = useRef<Record<string, string>>({});
  const operationRecords = useOperationRegistry((state) => state.operations);
  const qc = useQueryClient();
  const router = useRouter();
  const { t, locale, formatDateTime } = useI18n();
  const runtimeSafety = useRuntimeSafety("p4");
  const workflowSafety = useRuntimeSafety("workflow");
  const p4Maintenance = !runtimeSafety.allowed;
  const switchDeployments = deploymentsQ.data?.items ?? [];
  const deploymentPages = Math.max(1, Math.ceil((deploymentsQ.data?.total ?? 0) / deploymentLimit));
  const hasUnresolvedRollback = useCallback((deploymentId: string) =>
    Object.values(operationRecords).some(
      (item) => item.kind === "p4.rollback"
        && item.target === `${switchId}:${deploymentId}`
        && ["prepared", "submitting", "outcome_unknown"].includes(item.status),
    ), [operationRecords, switchId]);

  const snapshotMut = useMutation({
    mutationFn: () => api.post(`/p4/switches/${switchId}/snapshots`),
    onSuccess: () => toast.success(t("p4.snapshotCreated")),
    onError: (e) =>
      toast.error(t("p4.snapshotFailed"), {
        description: apiErrorMessage(e, locale),
      }),
  });

  const rollbackOptions = useMemo(() => ({
    kind: "p4.rollback",
    target: (payload: { deploymentId: string }) => `${switchId}:${payload.deploymentId}`,
    execute: (payload: { deploymentId: string }, key: string) =>
      api
        .post<P4DeploymentResponse>(`/p4/deployments/${payload.deploymentId}/rollback`, null, {
          headers: { "Idempotency-Key": key },
        })
        .then((response) => response.data),
    resultStatus: (result: P4DeploymentResponse) => result.status,
  }), [switchId]);
  const rollbackOperation = useIdempotentOperation(rollbackOptions);
  const rollbackMut = useMutation({
    retry: false,
    mutationFn: (deployId: string) => {
      rollbackKeys.current[deployId] = rollbackKeys.current[deployId] ?? newIdempotencyKey("p4-rollback");
      return rollbackOperation.run({ deploymentId: deployId }, rollbackKeys.current[deployId]);
    },
    onSuccess: ({ result }, deployId) => {
      qc.invalidateQueries({ queryKey: ["p4-deployments"] });
      setRollbackTarget(null);
      if (["unknown", "needs_reconcile"].includes(result.status)) {
        toast.warning(t("p4.operationOutcomeUnknown"), { description: rollbackKeys.current[deployId] });
        return;
      }
      delete rollbackKeys.current[deployId];
      toast.success(t("p4.rollbackCompleted"));
    },
    onError: (error, deployId) => {
      const key = rollbackKeys.current[deployId];
      const outcome = key ? useOperationRegistry.getState().operations[key]?.status : undefined;
      if (outcome === "outcome_unknown") {
        setRollbackTarget(null);
        toast.warning(t("p4.operationOutcomeUnknown"), { description: key });
        return;
      }
      delete rollbackKeys.current[deployId];
      const info = parseApiError(error, locale);
      toast.error(t("p4.rollbackFailed"), { description: apiErrorMessage(info, locale) });
    },
  });

  function takeSnapshot() {
    if (runtimeSafety.guard()) snapshotMut.mutate();
  }

  function confirmRollback() {
    if (!runtimeSafety.guard() || !rollbackTarget) return;
    rollbackMut.mutate(rollbackTarget.id);
  }

  if (switchQ.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    );
  }

  const sw = switchQ.data;

  if (switchQ.isError) {
    return (
      <div className="space-y-4">
        <Link href="/p4" className={buttonVariants({ variant: "ghost", size: "sm", className: "h-8" })}>
          <ArrowLeft className="mr-1 h-3.5 w-3.5" />
          {t("common.back")}
        </Link>
        <Card className="border-border bg-card">
          <CardHeader>
            <CardTitle className="text-sm">{t("p4.switchLoadFailed")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="text-sm text-muted-foreground">{t("p4.switchLoadFailedHint")}</p>
            <p className="text-xs font-mono text-destructive">
              {apiErrorMessage(switchQ.error, locale)}
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!sw) {
    return (
      <div className="space-y-4">
        <Link href="/p4" className={buttonVariants({ variant: "ghost", size: "sm", className: "h-8" })}>
          <ArrowLeft className="mr-1 h-3.5 w-3.5" />
          {t("common.back")}
        </Link>
        <Card className="border-border bg-card">
          <CardHeader>
            <CardTitle className="text-sm">{t("p4.switchNotFound")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="text-sm text-muted-foreground">{t("p4.switchNotFoundHint")}</p>
            <p className="text-xs font-mono text-muted-foreground">{switchId}</p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link href="/p4" className={buttonVariants({ variant: "ghost", size: "sm", className: "h-8" })}>
          <ArrowLeft className="mr-1 h-3.5 w-3.5" />
          {t("common.back")}
        </Link>
        <div>
          <h1 className="text-xl font-semibold">
            {sw?.name ?? switchId}
          </h1>
          <p className="text-xs text-muted-foreground font-mono">
            {sw?.grpc_addr} · {sw?.pipeline_owner}
          </p>
        </div>
      </div>

      <P4OperationRecovery switchId={switchId} />

      <Tabs defaultValue="tables" className="space-y-4">
        <TabsList className="bg-secondary/30">
          <TabsTrigger value="tables" className="text-xs">{t("p4.tables")}</TabsTrigger>
          <TabsTrigger value="entries" className="text-xs">{t("p4.entries")}</TabsTrigger>
          <TabsTrigger value="deployments" className="text-xs">{t("p4.deployments")}</TabsTrigger>
        </TabsList>

        {/* Tables Tab */}
        <TabsContent value="tables" className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">{t("p4.schemaFromP4Info")}</p>
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs"
              onClick={takeSnapshot}
              disabled={p4Maintenance || snapshotMut.isPending}
            >
              {snapshotMut.isPending ? (
                <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" />
              ) : (
                <Camera className="mr-1 h-3 w-3" />
              )}
              {t("p4.snapshot")}
            </Button>
          </div>
          {tablesQ.isLoading ? (
            <div className="space-y-2">
              {[1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-12 rounded-lg" />
              ))}
            </div>
          ) : tablesQ.isError ? (
            <ErrorState
              error={tablesQ.error}
              title={t("p4.switchLoadFailed")}
              onRetry={() => void tablesQ.refetch()}
            />
          ) : (
            (tablesQ.data ?? []).map((t) => (
              <TableSchemaCard key={t.name ?? t.id} table={t} />
            ))
          )}
        </TabsContent>

        {/* Entries Tab */}
        <TabsContent value="entries" className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="p4-selected-table" className="text-xs">{t("p4.selectTable")}</Label>
            <Select
              value={selectedTable ?? ""}
              onValueChange={(v) => setSelectedTable(v || null)}
            >
              <SelectTrigger id="p4-selected-table" className="w-64 h-8 text-xs font-mono">
                <SelectValue placeholder={t("p4.chooseTable")} />
              </SelectTrigger>
              <SelectContent>
                {(tablesQ.data ?? []).map((t) => (
                  <SelectItem key={t.name ?? t.id} value={t.name} className="font-mono text-xs">
                    {t.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {selectedTable && formSchemaQ.data && (
            <EntryForm
              key={`${selectedTable}:${formSchemaQ.data.schema_hash}`}
              switchId={switchId}
              tableName={selectedTable}
              schema={formSchemaQ.data}
              switchState={sw}
            />
          )}

          {selectedTable && formSchemaQ.isError && (
            <ErrorState
              error={formSchemaQ.error}
              title={t("p4.schemaUnavailable")}
              onRetry={() => void formSchemaQ.refetch()}
            />
          )}

          {selectedTable && entriesQ.isLoading && (
            <Skeleton className="h-40 rounded-xl" />
          )}

          {selectedTable && entriesQ.isError && (
            <ErrorState
              error={entriesQ.error}
              title={t("p4.entriesLoadFailed")}
              onRetry={() => void entriesQ.refetch()}
            />
          )}

          {selectedTable && entriesQ.data && (
            <Card className="border-border bg-card">
              <CardHeader>
                <CardTitle className="flex flex-wrap items-center gap-1.5 text-sm font-medium text-muted-foreground">
                  {t("p4.currentEntries", { table: selectedTable })}
                  <Badge variant="outline" className="text-xs">
                    {entriesQ.data.state_source}
                  </Badge>
                  <Badge variant="outline" className="text-xs">
                    {entriesQ.data.verification}
                  </Badge>
                </CardTitle>
              </CardHeader>
              <CardContent>
                {entriesQ.data.entries === null ? (
                  <p role="alert" className="py-6 text-center text-sm text-muted-foreground">
                    {entriesQ.data.reason ?? t("p4.entriesLoadFailed")}
                  </p>
                ) : entriesQ.data.entries.length === 0 ? (
                  <p className="text-sm text-muted-foreground text-center py-6">
                    {t("p4.noEntries")}
                  </p>
                ) : (
                  <>
                    <div className="space-y-3 md:hidden">
                      {entriesQ.data.entries.map((entry, idx) => (
                        <article
                          key={entry.entry_id ?? idx}
                          className="min-w-0 space-y-3 rounded-lg border border-border bg-muted/20 p-4"
                        >
                          <div>
                            <p className="text-xs font-medium text-muted-foreground">
                              {t("p4.matchFieldsColumn")}
                            </p>
                            <p className="mt-1 break-all font-mono text-xs">
                              {JSON.stringify(entry.match_fields)}
                            </p>
                          </div>
                          <div>
                            <p className="text-xs font-medium text-muted-foreground">
                              {t("p4.action")}
                            </p>
                            <p className="mt-1 break-all font-mono text-xs text-primary">
                              {entry.action_name}
                            </p>
                          </div>
                          <div>
                            <p className="text-xs font-medium text-muted-foreground">
                              {t("p4.paramsColumn")}
                            </p>
                            <p className="mt-1 break-all font-mono text-xs">
                              {JSON.stringify(entry.action_params)}
                            </p>
                          </div>
                        </article>
                      ))}
                    </div>
                    <div className="hidden overflow-x-auto md:block">
                      <Table>
                      <TableHeader>
                        <TableRow className="border-border hover:bg-transparent">
                          <TableHead className="text-xs">{t("p4.matchFieldsColumn")}</TableHead>
                          <TableHead className="text-xs">{t("p4.action")}</TableHead>
                          <TableHead className="text-xs">{t("p4.paramsColumn")}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {entriesQ.data.entries.map((entry, idx) => (
                          <TableRow key={entry.entry_id ?? idx} className="border-border">
                            <TableCell className="text-xs font-mono max-w-[200px] truncate">
                              {JSON.stringify(entry.match_fields)}
                            </TableCell>
                            <TableCell className="text-xs font-mono text-primary">
                              {entry.action_name}
                            </TableCell>
                            <TableCell className="max-w-[200px] truncate text-xs font-mono" title={JSON.stringify(entry.action_params)}>
                              {JSON.stringify(entry.action_params)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                      </Table>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {/* Deployments Tab */}
        <TabsContent value="deployments" className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Select
              value={deploymentStatus || "all"}
              onValueChange={(value) => {
                setDeploymentStatus(value === "all" || value === null ? "" : value);
                setDeploymentPage(1);
              }}
            >
              <SelectTrigger className="w-48"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t("common.all")}</SelectItem>
                <SelectItem value="applied">applied</SelectItem>
                <SelectItem value="unknown">unknown</SelectItem>
                <SelectItem value="p4_write_failed">p4_write_failed</SelectItem>
                <SelectItem value="rejected">rejected</SelectItem>
              </SelectContent>
            </Select>
            <span className="text-sm text-muted-foreground">{deploymentsQ.data?.total ?? 0} · {deploymentPage}/{deploymentPages}</span>
          </div>
          {deploymentsQ.isLoading ? (
            <div className="space-y-2">
              {[1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-12 rounded-lg" />
              ))}
            </div>
          ) : deploymentsQ.isError ? (
            <Card className="border-border bg-card">
              <CardContent className="p-6 text-sm text-destructive">
                {apiErrorMessage(deploymentsQ.error, locale)}
              </CardContent>
            </Card>
          ) : switchDeployments.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              {t("p4.noDeploymentHistory")}
            </p>
          ) : (
            <>
              <div className="space-y-3 md:hidden">
                {switchDeployments.map((d) => (
                  <article key={d.id} className="min-w-0 space-y-3 rounded-lg border border-border bg-card p-4">
                    <div className="flex min-w-0 items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="break-all font-mono text-sm font-medium">{d.table_name}</p>
                        <p className="mt-1 font-mono text-xs text-muted-foreground">
                          {formatDateTime(d.created_at, {
                            month: "short",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                          })}
                        </p>
                      </div>
                      <DeploymentStatusBadge status={d.status} />
                    </div>
                    <dl className="grid grid-cols-2 gap-3 text-xs">
                      <div>
                        <dt className="text-muted-foreground">{t("p4.operation")}</dt>
                        <dd className="mt-1 font-mono">{d.operation}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">{t("p4.writerMode")}</dt>
                        <dd className="mt-1 break-all font-mono">{deploymentWriterMode(d)}</dd>
                      </div>
                      <div className="col-span-2">
                        <dt className="text-muted-foreground">{t("workflow.deployment.verificationScope")}</dt>
                        <dd className="mt-1 break-all font-mono">{deploymentVerificationScope(d)}</dd>
                      </div>
                      {d.rollback_state ? (
                        <div className="col-span-2">
                          <dt className="text-muted-foreground">rollback_state</dt>
                          <dd className="mt-1 break-all font-mono">{d.rollback_state}</dd>
                        </div>
                      ) : null}
                    </dl>
                    <div className="flex flex-wrap gap-2 border-t border-border pt-3">
                      {d.status === "applied" && d.rollback_state !== "rolled_back" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="border-warning/40 text-warning hover:bg-warning/10"
                          onClick={() => { if (workflowSafety.guard()) setRestoreTarget(d); }}
                          disabled={!workflowSafety.allowed}
                        >
                          <ShieldCheck className="mr-1 h-3.5 w-3.5" />
                          {t("p4.workflowRollback")}
                        </Button>
                      ) : null}
                      {isAdmin && d.status === "applied" && d.rollback_state !== "rolled_back" ? (
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => { if (runtimeSafety.guard()) setRollbackTarget(d); }}
                          disabled={p4Maintenance || rollbackMut.isPending || hasUnresolvedRollback(d.id)}
                        >
                          <RotateCcw className="mr-1 h-3.5 w-3.5" />
                          {t("p4.rollback")}
                        </Button>
                      ) : null}
                    </div>
                  </article>
                ))}
              </div>
              <div className="hidden overflow-x-auto md:block">
                <Table>
                <TableHeader>
                  <TableRow className="border-border hover:bg-transparent">
                    <TableHead className="text-xs">{t("common.time")}</TableHead>
                    <TableHead className="text-xs">{t("p4.tableName")}</TableHead>
                    <TableHead className="text-xs">{t("p4.operation")}</TableHead>
                    <TableHead className="text-xs">{t("p4.writerMode")}</TableHead>
                    <TableHead className="text-xs">{t("common.status")}</TableHead>
                    <TableHead className="text-xs text-right">{t("common.actions")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {switchDeployments.map((d) => (
                    <TableRow key={d.id} className="border-border">
                      <TableCell className="text-xs font-mono text-muted-foreground whitespace-nowrap">
                        {formatDateTime(d.created_at, {
                          month: "short",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </TableCell>
                      <TableCell className="text-xs font-mono">{d.table_name}</TableCell>
                      <TableCell className="text-xs">
                        <Badge variant="outline" className="text-xs">
                          {d.operation}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs">
                        <div className="flex flex-col gap-1">
                          <Badge
                            variant={deploymentWriterMode(d) === "demo_fake" ? "outline" : "secondary"}
                            className="w-fit text-xs font-mono"
                          >
                            {deploymentWriterMode(d)}
                          </Badge>
                          <span className="max-w-[15rem] truncate text-xs font-mono text-muted-foreground">
                            {deploymentVerificationScope(d)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <DeploymentStatusBadge status={d.status} />
                        {d.rollback_state ? <p className="mt-1 font-mono text-xs text-muted-foreground">{d.rollback_state}</p> : null}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex flex-wrap items-center justify-end gap-1.5">
                          {d.status === "applied" && d.rollback_state !== "rolled_back" && (
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-8 flex-col items-start gap-0 py-1 text-xs leading-tight border-warning/40 text-warning hover:bg-warning/10"
                              onClick={() => { if (workflowSafety.guard()) setRestoreTarget(d); }}
                              disabled={!workflowSafety.allowed}
                              title={t("p4.workflowRollbackTooltip")}
                            >
                              <span className="flex items-center gap-1">
                                <ShieldCheck className="h-3 w-3" />
                                <span>{t("p4.workflowRollback")}</span>
                              </span>
                              <span className="font-mono text-xs text-muted-foreground/80">
                                · restore_rule
                              </span>
                            </Button>
                          )}
                          {isAdmin && d.status === "applied" && d.rollback_state !== "rolled_back" && (
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-8 text-xs text-destructive hover:text-destructive hover:bg-destructive/10"
                              onClick={() => { if (runtimeSafety.guard()) setRollbackTarget(d); }}
                              disabled={p4Maintenance || rollbackMut.isPending || hasUnresolvedRollback(d.id)}
                            >
                              {rollbackMut.isPending ? (
                                <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" />
                              ) : (
                                <RotateCcw className="mr-1 h-3 w-3" />
                              )}
                              {t("p4.rollback")}
                            </Button>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
                </Table>
              </div>
            </>
          )}
          {deploymentsQ.data && deploymentsQ.data.total > deploymentLimit ? (
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" disabled={deploymentPage <= 1} onClick={() => setDeploymentPage((page) => Math.max(1, page - 1))}>{t("common.back")}</Button>
              <Button variant="outline" size="sm" disabled={deploymentPage >= deploymentPages} onClick={() => setDeploymentPage((page) => Math.min(deploymentPages, page + 1))}>{t("common.next")}</Button>
            </div>
          ) : null}
        </TabsContent>
      </Tabs>
      <Dialog
        open={!!rollbackTarget}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && !rollbackMut.isPending) {
            if (rollbackTarget) {
              const key = rollbackKeys.current[rollbackTarget.id];
              const state = key ? useOperationRegistry.getState().operations[key]?.status : undefined;
              if (!state || !["prepared", "submitting", "outcome_unknown"].includes(state)) {
                delete rollbackKeys.current[rollbackTarget.id];
              }
            }
            setRollbackTarget(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("p4.rollbackConfirmTitle")}</DialogTitle>
            <DialogDescription>{t("p4.rollbackConfirmDescription")}</DialogDescription>
          </DialogHeader>
          {rollbackTarget && (
            <div className="space-y-2 rounded-lg border border-border bg-secondary/20 p-3 text-xs">
              <div className="grid grid-cols-[6rem_1fr] gap-2">
                <span className="text-muted-foreground">{t("p4.deploymentId")}</span>
                <span className="break-all font-mono">{rollbackTarget.id}</span>
              </div>
              <div className="grid grid-cols-[6rem_1fr] gap-2">
                <span className="text-muted-foreground">{t("p4.tableName")}</span>
                <span className="break-all font-mono">{rollbackTarget.table_name}</span>
              </div>
              <div className="grid grid-cols-[6rem_1fr] gap-2">
                <span className="text-muted-foreground">{t("p4.operation")}</span>
                <span className="font-mono">{rollbackTarget.operation}</span>
              </div>
            </div>
          )}
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={rollbackMut.isPending} />}>
              {t("common.cancel")}
            </DialogClose>
            <Button
              type="button"
              variant="destructive"
              disabled={p4Maintenance || !rollbackTarget || rollbackMut.isPending || (rollbackTarget ? hasUnresolvedRollback(rollbackTarget.id) : false)}
              onClick={confirmRollback}
            >
              {rollbackMut.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
              ) : (
                <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
              )}
              {t("p4.rollback")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <CreateRestoreRuleWorkflowDialog
        deployment={restoreTarget}
        open={!!restoreTarget}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) {
            setRestoreTarget(null);
          }
        }}
        onCreated={() => {
          qc.invalidateQueries({ queryKey: ["workflows"] });
          qc.invalidateQueries({ queryKey: ["p4-deployments"] });
          router.push("/workflows");
        }}
      />
    </div>
  );
}
