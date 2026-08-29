import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EmptyState, ErrorState, LoadingState, StaleState } from "@/components/async-state";

describe("shared async states", () => {
  it("keeps loading, empty, error, and stale semantics distinct", () => {
    const retry = vi.fn();
    const { rerender } = render(<LoadingState label="Loading workflows" rows={2} />);
    expect(screen.getByRole("status", { name: "Loading workflows" })).toBeInTheDocument();

    rerender(<EmptyState title="No workflows" description="Create one to begin." />);
    expect(screen.getByRole("heading", { name: "No workflows" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    rerender(<ErrorState title="Workflow query failed" error={new Error("upstream unavailable")} onRetry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Workflow query failed");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(retry).toHaveBeenCalledTimes(1);

    rerender(<StaleState>Revision changed; draft retained.</StaleState>);
    expect(screen.getByText("Revision changed; draft retained.")).toBeInTheDocument();
  });
});
