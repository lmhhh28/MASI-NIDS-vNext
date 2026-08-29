"use client";

import { useRef } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import api from "@/lib/api";
import { isHttpErrorLike } from "@/lib/http-error";
import type {
  EventV3Detail,
  EventV3ListResponse,
  IncidentV3,
  IncidentV3Status,
  ModelRoleV3,
  NidsDecision,
  WorkflowResponse,
} from "@/types/api";

export const EVENTS_V3_REFRESH_INTERVAL_MS = 5_000;

export interface EventV3Filters {
  cursor?: string | null;
  decision?: NidsDecision | "all";
  targetUuid?: string;
  modelRole?: ModelRoleV3 | "all";
  limit?: number;
}

export function useEventsV3(filters: EventV3Filters) {
  return useQuery<EventV3ListResponse>({
    queryKey: ["events-v3", filters],
    queryFn: ({ signal }) =>
      api
        .get<EventV3ListResponse>("/events/v3", {
          params: {
            cursor: filters.cursor || undefined,
            decision: filters.decision === "all" ? undefined : filters.decision,
            target_uuid: filters.targetUuid?.trim() || undefined,
            model_role: filters.modelRole === "all" ? undefined : filters.modelRole,
            limit: filters.limit ?? 50,
          },
          signal,
        })
        .then((response) => response.data),
    refetchInterval: EVENTS_V3_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });
}

export function useEventV3(eventId: string) {
  return useQuery<EventV3Detail>({
    queryKey: ["events-v3", "detail", eventId],
    queryFn: ({ signal }) =>
      api.get<EventV3Detail>(`/events/v3/${eventId}`, { signal }).then((response) => response.data),
    enabled: Boolean(eventId),
    refetchOnWindowFocus: false,
  });
}

export function useAdmitEventV4Workflow(eventId: string) {
  const queryClient = useQueryClient();
  const idempotencyKey = useRef<string | null>(null);
  const queryOnly = useRef(false);

  return useMutation<WorkflowResponse, Error, { requestedIntent?: string; locale?: string }>({
    retry: false,
    mutationFn: async ({ requestedIntent, locale }) => {
      const key = idempotencyKey.current ?? crypto.randomUUID();
      idempotencyKey.current = key;
      const path = `/workflows/event-v4/${eventId}`;
      const headers = {
        "Idempotency-Key": key,
        ...(locale ? { "Accept-Language": locale } : {}),
      };
      if (queryOnly.current) {
        return api.get<WorkflowResponse>(path, { headers }).then((response) => response.data);
      }
      try {
        const response = await api.post<WorkflowResponse>(
          path,
          { requested_intent: requestedIntent?.trim() || null, locale: locale ?? null },
          { headers },
        );
        queryOnly.current = true;
        return response.data;
      } catch (error) {
        const status = isHttpErrorLike(error) ? error.response?.status : undefined;
        if (status !== undefined && status < 500) {
          idempotencyKey.current = null;
          throw error;
        }
        // A timeout/5xx may have happened after durable commit. From this point
        // onward this hook is permanently query-only for the original key.
        queryOnly.current = true;
        try {
          return await api
            .get<WorkflowResponse>(path, { headers })
            .then((response) => response.data);
        } catch {
          throw new Error("WORKFLOW_ADMISSION_OUTCOME_UNKNOWN");
        }
      }
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflows"] }),
        queryClient.invalidateQueries({ queryKey: ["events-v3", "detail", eventId] }),
      ]);
    },
  });
}

export function useIncidentsV3(filters: {
  status?: IncidentV3Status | "all";
  targetUuid?: string;
  limit?: number;
}) {
  return useQuery<IncidentV3[]>({
    queryKey: ["incidents-v3", filters],
    queryFn: ({ signal }) =>
      api
        .get<IncidentV3[]>("/incidents/v3", {
          params: {
            status: filters.status === "all" ? undefined : filters.status,
            target_uuid: filters.targetUuid?.trim() || undefined,
            limit: filters.limit ?? 100,
          },
          signal,
        })
        .then((response) => response.data),
    refetchInterval: EVENTS_V3_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });
}
