import {
  useQuery,
  useMutation,
  useQueryClient,
  keepPreviousData,
  type QueryClient,
} from "@tanstack/react-query";
import api from "@/lib/api";
import type {
  EventSourceCreate,
  EventSourceResponse,
  EventStats,
  NidsEvent,
  NidsEventListResponse,
  IngestOnceRequest,
  IngestOnceResponse,
} from "@/types/api";

export const EVENTS_REFRESH_INTERVAL_MS = 5_000;
const DASHBOARD_TREND_LIMIT = 100;

export function invalidateEventQueries(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: ["events"] });
  qc.invalidateQueries({ queryKey: ["events-paginated"] });
  qc.invalidateQueries({ queryKey: ["dashboard-events"] });
}

export function useEvents(params?: {
  decision?: string;
  limit?: number;
  offset?: number;
  online_run_id?: string;
}) {
  return useQuery<NidsEvent[]>({
    queryKey: ["events", params],
    queryFn: ({ signal }) => api.get("/events", { params, signal }).then((r) => r.data),
    refetchInterval: EVENTS_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

/**
 * Pagination-aware events hook backed by the `?include_total=1` envelope.
 *
 * Used by the dashboard event table. Refetches every 5s and uses
 * `placeholderData: keepPreviousData` so page navigation doesn't flash a
 * blank table between requests.
 */
export function useEventsPaginated(params: {
  decision?: string;
  online_run_id?: string;
  page: number;       // 1-based
  pageSize: number;
}) {
  const offset = Math.max(0, (params.page - 1) * params.pageSize);
  return useQuery<NidsEventListResponse>({
    queryKey: ["events-paginated", params],
    queryFn: ({ signal }) =>
      api
        .get("/events", {
          params: {
            include_total: 1,
            limit: params.pageSize,
            offset,
            decision: params.decision || undefined,
            online_run_id: params.online_run_id || undefined,
          },
          signal,
        })
        .then((r) => r.data),
    refetchInterval: EVENTS_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });
}

export function useDashboardEvents(params: {
  decision?: string;
  online_run_id?: string;
  page: number;
  pageSize: number;
}) {
  const offset = Math.max(0, (params.page - 1) * params.pageSize);
  return useQuery<{
    trend: NidsEvent[];
    page: NidsEventListResponse;
  }>({
    queryKey: ["dashboard-events", params],
    queryFn: async ({ signal }) => {
      const commonParams = {
        decision: params.decision || undefined,
        online_run_id: params.online_run_id || undefined,
      };

      if (offset === 0) {
        const limit = Math.max(params.pageSize, DASHBOARD_TREND_LIMIT);
        const response = await api.get<NidsEventListResponse>("/events", {
          params: {
            ...commonParams,
            include_total: 1,
            limit,
            offset,
          },
          signal,
        });
        return {
          trend: response.data.items.slice(0, DASHBOARD_TREND_LIMIT),
          page: {
            ...response.data,
            items: response.data.items.slice(0, params.pageSize),
            limit: params.pageSize,
            offset,
          },
        };
      }

      const [trendResponse, pageResponse] = await Promise.all([
        api.get<NidsEvent[]>("/events", {
          params: {
            ...commonParams,
            limit: DASHBOARD_TREND_LIMIT,
          },
          signal,
        }),
        api.get<NidsEventListResponse>("/events", {
          params: {
            ...commonParams,
            include_total: 1,
            limit: params.pageSize,
            offset,
          },
          signal,
        }),
      ]);

      return {
        trend: trendResponse.data,
        page: pageResponse.data,
      };
    },
    refetchInterval: EVENTS_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });
}

export function useEvent(id: string | null) {
  return useQuery<NidsEvent>({
    queryKey: ["events", id],
    queryFn: ({ signal }) => api.get(`/events/${id}`, { signal }).then((r) => r.data),
    enabled: !!id,
  });
}

export function useEventStats() {
  return useQuery<EventStats>({
    queryKey: ["event-stats"],
    queryFn: ({ signal }) => api.get("/events/stats", { signal }).then((r) => r.data),
    refetchInterval: EVENTS_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

export function useEventSources() {
  return useQuery<EventSourceResponse[]>({
    queryKey: ["event-sources"],
    queryFn: ({ signal }) => api.get("/events/sources", { signal }).then((r) => r.data),
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

export function useCreateEventSource() {
  const qc = useQueryClient();
  return useMutation<EventSourceResponse, Error, EventSourceCreate>({
    mutationFn: (payload) => api.post("/events/sources", payload).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["event-sources"] });
    },
  });
}

export function useIngestOnce() {
  const qc = useQueryClient();
  return useMutation<IngestOnceResponse, Error, IngestOnceRequest>({
    mutationFn: (payload) =>
      api.post("/events/ingest:once", payload).then((r) => r.data),
    onSuccess: () => {
      invalidateEventQueries(qc);
      qc.invalidateQueries({ queryKey: ["event-stats"] });
      qc.invalidateQueries({ queryKey: ["alerts"] });
      qc.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
}
