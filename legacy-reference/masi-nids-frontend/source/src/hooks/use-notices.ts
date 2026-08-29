import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import type { NoticesResponse } from "@/types/api";

export function useNotices(locale: string) {
  return useQuery<NoticesResponse>({
    queryKey: ["notices", locale],
    queryFn: ({ signal }) =>
      api.get("/notices", { params: { locale }, signal }).then((r) => r.data),
    staleTime: 60_000,
  });
}
