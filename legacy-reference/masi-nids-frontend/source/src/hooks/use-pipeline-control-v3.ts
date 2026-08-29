"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import api from "@/lib/api";
import type {
  PipelineActivationRequestV3,
  PipelineActivationResponseV3,
  PipelineBundleV3,
  PipelineTargetRegistrationRequestV3,
  PipelineTargetV3,
} from "@/types/api";

const PIPELINE_CONTROL_REFRESH_INTERVAL_MS = 10_000;

export function usePipelineBundlesV3() {
  return useQuery<PipelineBundleV3[]>({
    queryKey: ["pipeline-control-v3", "bundles"],
    queryFn: ({ signal }) =>
      api
        .get<PipelineBundleV3[]>("/p4/control-v3/pipeline-bundles", { signal })
        .then((response) => response.data),
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

export function usePipelineTargetsV3() {
  return useQuery<PipelineTargetV3[]>({
    queryKey: ["pipeline-control-v3", "targets"],
    queryFn: ({ signal }) =>
      api
        .get<PipelineTargetV3[]>("/p4/control-v3/targets", { signal })
        .then((response) => response.data),
    refetchInterval: PIPELINE_CONTROL_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  });
}

export function usePipelineActivationsV3(targetUuid: string | null) {
  return useQuery<PipelineActivationResponseV3[]>({
    queryKey: ["pipeline-control-v3", "targets", targetUuid, "activations"],
    queryFn: ({ signal }) =>
      api
        .get<PipelineActivationResponseV3[]>(
          `/p4/control-v3/targets/${targetUuid}/activations`,
          { signal },
        )
        .then((response) => response.data),
    enabled: targetUuid !== null,
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
  });
}

function useInvalidatePipelineControl() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: ["pipeline-control-v3"] });
}

export function useImportPipelineBundleV3() {
  const invalidate = useInvalidatePipelineControl();
  return useMutation<PipelineBundleV3, Error, string>({
    mutationFn: (sourceRelativePath) =>
      api
        .post<PipelineBundleV3>("/p4/control-v3/pipeline-bundles/import", {
          source_relative_path: sourceRelativePath,
        }, { timeout: 120_000 })
        .then((response) => response.data),
    onSuccess: invalidate,
  });
}

export function useCreatePipelineTargetV3() {
  const invalidate = useInvalidatePipelineControl();
  return useMutation<PipelineTargetV3, Error, PipelineTargetRegistrationRequestV3>({
    mutationFn: (registration) =>
      api
        .post<PipelineTargetV3>("/p4/control-v3/targets", registration)
        .then((response) => response.data),
    onSuccess: invalidate,
  });
}

export function useReplacePipelineTargetV3() {
  const invalidate = useInvalidatePipelineControl();
  return useMutation<
    PipelineTargetV3,
    Error,
    { registration: PipelineTargetRegistrationRequestV3; expectedVersion: number }
  >({
    mutationFn: ({ registration, expectedVersion }) =>
      api
        .put<PipelineTargetV3>(
          `/p4/control-v3/targets/${registration.target_uuid}`,
          registration,
          { headers: { "If-Match": String(expectedVersion) } },
        )
        .then((response) => response.data),
    onSuccess: invalidate,
  });
}

export function useActivatePipelineV3() {
  const invalidate = useInvalidatePipelineControl();
  return useMutation<
    PipelineActivationResponseV3,
    Error,
    { targetUuid: string; payload: PipelineActivationRequestV3; idempotencyKey: string }
  >({
    mutationFn: ({ targetUuid, payload, idempotencyKey }) =>
      api
        .post<PipelineActivationResponseV3>(
          `/p4/control-v3/targets/${targetUuid}/activations`,
          payload,
          { headers: { "Idempotency-Key": idempotencyKey } },
        )
        .then((response) => response.data),
    onSuccess: invalidate,
  });
}
