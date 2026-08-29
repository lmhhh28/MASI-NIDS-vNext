"use client";

import type { MouseEvent, ReactNode } from "react";

export function SkipLink({ children }: { children: ReactNode }) {
  const focusMain = (event: MouseEvent<HTMLAnchorElement>) => {
    const main = document.getElementById("main-content");
    if (!main) return;
    event.preventDefault();
    main.scrollIntoView({ block: "start" });
    main.focus({ preventScroll: true });
  };

  return (
    <a
      href="#main-content"
      onClick={focusMain}
      className="fixed left-3 top-3 z-[1000] -translate-y-20 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow focus:translate-y-0"
    >
      {children}
    </a>
  );
}
