import type { ApiResponse } from "@/lib/api";

export function apiResponse<T>(
  data: T,
  status = 200,
  headers = new Headers(),
): ApiResponse<T> {
  return { data, status, headers };
}
