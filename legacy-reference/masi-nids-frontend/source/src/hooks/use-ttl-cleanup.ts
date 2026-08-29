import { useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { TTLCleanupRunResponse } from "@/types/api";

export function useRunTTLCleanup() {
  const qc = useQueryClient();
  return useMutation<TTLCleanupRunResponse, Error, { dryRun?: boolean }>({
    mutationFn: ({ dryRun }) =>
      api
        .post("/p4/ttl-cleanup:run", { dry_run: !!dryRun })
        .then((r) => r.data),
    onSuccess: (_, vars) => {
      if (!vars.dryRun) {
        qc.invalidateQueries({ queryKey: ["p4-entries"] });
        qc.invalidateQueries({ queryKey: ["p4-deployments"] });
      }
    },
  });
}
