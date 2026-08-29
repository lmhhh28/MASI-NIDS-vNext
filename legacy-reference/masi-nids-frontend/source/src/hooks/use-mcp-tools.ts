import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import type { MCPToolManifestResponse } from "@/types/api";

export function useMcpManifest() {
  return useQuery<MCPToolManifestResponse>({
    queryKey: ["mcp-tools-manifest"],
    queryFn: ({ signal }) => api.get("/mcp/tools", { signal }).then((r) => r.data),
    staleTime: 60_000,
  });
}
