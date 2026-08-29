"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";

export function RouteFocus() {
  const pathname = usePathname();

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const main = document.getElementById("main-content");

      // The dashboard layout persists across App Router navigation, so its
      // nested scroll container also persists. Reset both scroll owners before
      // moving focus or a short page can open below its content as a blank view.
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      if (main) {
        main.scrollTop = 0;
        main.scrollLeft = 0;
        main.focus({ preventScroll: true });
      }
    });

    return () => cancelAnimationFrame(frame);
  }, [pathname]);

  return null;
}
