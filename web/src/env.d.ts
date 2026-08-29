/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BUILD_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

interface Window {
  readonly __masiRuntimeReadback: () => {
    query_cache_entries: number
    query_cache_bytes: number
    chart_instances: number
    application_timers: number
  }
}
