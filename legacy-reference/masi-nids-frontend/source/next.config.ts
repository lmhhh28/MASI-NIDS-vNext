import type { NextConfig } from "next";

const backendInternalUrl = (
  process.env.NIDS_BACKEND_INTERNAL_URL ?? "http://127.0.0.1:8090"
).replace(/\/+$/, "");

const nextConfig: NextConfig = {
  allowedDevOrigins: ["127.0.0.1"],
  turbopack: {
    root: process.cwd(),
  },
  env: {
    NEXT_PUBLIC_NIDS_DEMO_SOURCE_URL:
      process.env.NEXT_PUBLIC_NIDS_DEMO_SOURCE_URL ?? "http://127.0.0.1:8088",
    NEXT_PUBLIC_NIDS_DEMO_SOURCE_LOOPBACK_ONLY:
      process.env.NEXT_PUBLIC_NIDS_DEMO_SOURCE_LOOPBACK_ONLY ?? "true",
  },
};

// Route handlers use this server-only value. Keeping it out of `env` prevents
// Next.js from inlining the internal backend address into browser bundles.
process.env.NIDS_BACKEND_INTERNAL_URL = backendInternalUrl;

export default nextConfig;
