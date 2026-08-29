import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { AdminUser, AdminUserPatch } from "@/types/api";

export function useAdminUsers() {
  return useQuery<AdminUser[]>({
    queryKey: ["admin-users"],
    queryFn: ({ signal }) => api.get("/admin/users", { signal }).then((r) => r.data),
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

export function usePatchAdminUser(userId: string) {
  const qc = useQueryClient();
  return useMutation<AdminUser, Error, AdminUserPatch>({
    mutationFn: (payload) =>
      api.patch(`/admin/users/${userId}`, payload).then((r) => r.data),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });
}
