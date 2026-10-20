import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { RunCompleteCard } from "../RunCompleteCard";
import { makeMessage } from "@/tests/helpers/factories";

const apiMock = vi.hoisted(() => ({
  getRun: vi.fn(),
  getRunPine: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));

describe("RunCompleteCard", () => {
  beforeEach(() => {
    apiMock.getRun.mockResolvedValue({ status: "success", run_id: "strategy-run" });
    apiMock.getRunPine.mockResolvedValue({ exists: false, content: null });
  });

  it("opens every completed backtest in the research dashboard view", () => {
    render(
      <MemoryRouter>
        <RunCompleteCard msg={makeMessage({ type: "run_complete", runId: "strategy-run" })} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /Full Report/i }))
      .toHaveAttribute("href", "/runs/strategy-run?view=dashboard");
  });

  it("does not render a dashboard link without a run id", () => {
    render(
      <MemoryRouter>
        <RunCompleteCard msg={makeMessage({ type: "run_complete" })} />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("link", { name: /Full Report/i })).not.toBeInTheDocument();
  });
});
