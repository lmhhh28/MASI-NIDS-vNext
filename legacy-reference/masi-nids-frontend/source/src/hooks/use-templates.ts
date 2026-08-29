"use client";

import { useQuery } from "@tanstack/react-query";

import api from "@/lib/api";
import type { TemplatesListResponse } from "@/types/api";

/**
 * Role-filtered listing for the workflow create form.
 *
 * Backend route: `GET /api/templates`. Admin-only template CRUD continues
 * to use `/api/admin/templates`. We keep the cache fresh for 60s since
 * template enable/disable is rare admin activity and the dialog opens
 * frequently from the dashboard.
 */
export function useTemplates() {
  return useQuery<TemplatesListResponse>({
    queryKey: ["templates"],
    queryFn: ({ signal }) => api.get("/templates", { signal }).then((r) => r.data),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}
