"use client";

import { useEffect } from "react";
import { ErrorState } from "@/components/async-state";

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <main className="mx-auto w-full max-w-3xl p-6" tabIndex={-1}>
      <ErrorState error={error} title="页面发生错误" onRetry={reset} />
    </main>
  );
}
