import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RouteFocus } from "./route-focus";

const navigation = vi.hoisted(() => ({ pathname: "/" }));

vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
}));

describe("RouteFocus", () => {
  beforeEach(() => {
    navigation.pathname = "/";
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  });

  it("resets persistent dashboard scroll before focusing a new route", () => {
    const view = render(
      <>
        <RouteFocus />
        <main id="main-content" tabIndex={-1} />
      </>,
    );
    const main = document.getElementById("main-content") as HTMLElement;
    const focus = vi.spyOn(main, "focus");
    main.scrollTop = 640;
    main.scrollLeft = 32;

    act(() => {
      navigation.pathname = "/sources";
      view.rerender(
        <>
          <RouteFocus />
          <main id="main-content" tabIndex={-1} />
        </>,
      );
    });

    expect(main.scrollTop).toBe(0);
    expect(main.scrollLeft).toBe(0);
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 0, left: 0, behavior: "auto" });
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
  });
});
