import { useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { LLMConfig, LLMConfigPatch, LLMTestResult } from "@/types/api";

export function usePatchLLMConfig(configId: string) {
  const qc = useQueryClient();
  return useMutation<LLMConfig, Error, { payload: LLMConfigPatch; version: number }>({
    retry: false,
    mutationFn: ({ payload, version }) =>
      api.patch(`/admin/llm-config/${configId}`, payload, { headers: { "If-Match": String(version) } }).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-llm"] });
    },
  });
}

export function useDeleteLLMConfig() {
  const qc = useQueryClient();
  return useMutation<{ deleted: boolean; id: string }, Error, { configId: string; version: number }>({
    retry: false,
    mutationFn: ({ configId, version }) =>
      api.delete(`/admin/llm-config/${configId}`, { headers: { "If-Match": String(version) } }).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-llm"] });
    },
  });
}

export function useTestLLMConfig() {
  return useMutation<LLMTestResult, Error, string>({
    mutationFn: (configId) =>
      api.post(`/admin/llm-config/${configId}/test`).then((r) => r.data),
  });
}

export function useProbeLLMConfig() {
  return useMutation<LLMTestResult, Error, string>({
    retry: false,
    mutationFn: (configId) => api.post(`/admin/llm-config/${configId}/probe`).then((response) => response.data),
  });
}
