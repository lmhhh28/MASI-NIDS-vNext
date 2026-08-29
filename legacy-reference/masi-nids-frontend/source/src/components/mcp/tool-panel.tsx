"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ArrowRight, Loader2, Play } from "lucide-react";
import { toast } from "sonner";

import {
  initialValueFromSchema,
  JsonSchemaFields,
  validateSchemaValue,
  type JsonSchema,
} from "@/components/mcp/json-schema-form";
import { Button, buttonVariants } from "@/components/ui/button";
import api, { apiErrorMessage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { MCPToolManifestEntry } from "@/types/api";

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function ToolPanel({
  tool,
  roleAllowed,
}: {
  tool: MCPToolManifestEntry;
  roleAllowed: boolean;
}) {
  const { t, locale } = useI18n();
  const [argumentsValue, setArgumentsValue] = useState<Record<string, unknown>>(
    () => objectValue(initialValueFromSchema(tool.input_schema as JsonSchema)),
  );
  const [result, setResult] = useState<unknown>(null);
  const [schemaErrors, setSchemaErrors] = useState<string[]>([]);
  const directExecution = tool.side_effect === "read" || tool.side_effect === "validate";
  const destination = tool.side_effect === "p4_write" ? "/p4" : "/workflows";

  const callMutation = useMutation({
    retry: false,
    mutationFn: (args: Record<string, unknown>) =>
      api
        .post("/mcp/console/tools/call", { tool_name: tool.name, arguments: args })
        .then((response) => response.data),
    onSuccess: (data) => {
      setResult(data);
      toast.success(t("mcp.toolExecuted"));
    },
    onError: (error) =>
      toast.error(t("mcp.toolFailed"), {
        description: apiErrorMessage(error, locale),
      }),
  });

  function execute() {
    const errors = validateSchemaValue(tool.input_schema as JsonSchema, argumentsValue);
    setSchemaErrors(errors);
    if (errors.length === 0) callMutation.mutate(argumentsValue);
  }

  return (
    <div className="mt-1 space-y-4 rounded-lg border bg-card p-4">
      <JsonSchemaFields
        schema={tool.input_schema as JsonSchema}
        value={argumentsValue}
        onChange={(next) => {
          setArgumentsValue(next);
          setSchemaErrors([]);
        }}
      />

      {schemaErrors.length > 0 ? (
        <div
          className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
          role="alert"
        >
          <p className="font-medium">{t("mcp.schemaErrors")}</p>
          <ul className="mt-1 list-disc pl-5 font-mono text-xs">
            {schemaErrors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {directExecution ? (
        <Button size="sm" disabled={!roleAllowed || callMutation.isPending} onClick={execute}>
          {callMutation.isPending ? (
            <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <Play aria-hidden="true" />
          )}
          {t("mcp.execute")}
        </Button>
      ) : (
        <div className="flex flex-col gap-3 rounded-md border border-warning/40 bg-warning/5 p-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm font-medium">{t("mcp.managedWriteNotice")}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("mcp.roleRequired", { role: tool.role_required })}
            </p>
          </div>
          {roleAllowed ? (
            <Link href={destination} className={buttonVariants()}>
              {tool.side_effect === "p4_write"
                ? t("mcp.openP4Flow")
                : t("mcp.openWorkflowFlow")}
              <ArrowRight aria-hidden="true" />
            </Link>
          ) : (
            <Button disabled>
              {tool.side_effect === "p4_write"
                ? t("mcp.openP4Flow")
                : t("mcp.openWorkflowFlow")}
              <ArrowRight aria-hidden="true" />
            </Button>
          )}
        </div>
      )}

      {result !== null ? (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border bg-muted/30 p-3 font-mono text-xs">
          {typeof result === "string" ? result : JSON.stringify(result, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}
