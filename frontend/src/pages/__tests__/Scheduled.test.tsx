import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Scheduled } from "@/pages/Scheduled";
import {
  ApiError,
  api,
  type ScheduledRun,
  type ScheduledRunOutcome,
  type VerdictRecord,
} from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listScheduledRuns: vi.fn(),
      createScheduledRun: vi.fn(),
      deleteScheduledRun: vi.fn(),
    },
  };
});

const mocked = api as unknown as {
  listScheduledRuns: ReturnType<typeof vi.fn>;
  createScheduledRun: ReturnType<typeof vi.fn>;
  deleteScheduledRun: ReturnType<typeof vi.fn>;
};

function run(overrides: Partial<ScheduledRun> = {}): ScheduledRun {
  return {
    id: "auckland-scan",
    prompt: "pre-open scan of NZX names",
    schedule: "30 23 * * 1-5",
    next_run_at: 1_790_000_000_000,
    status: "pending",
    created_at: 1_780_000_000_000,
    last_run_at: null,
    consecutive_failures: 0,
    last_error: null,
    failure_kind: null,
    config: {},
    timezone: "Pacific/Auckland",
    delivery_channel: null,
    delivery_target: null,
    delivery_status: "none",
    delivery_error: null,
    delivery_updated_at: null,
    last_verdict: null,
    last_run: null,
    last_run_session_id: null,
    ...overrides,
  };
}

function outcome(overrides: Partial<ScheduledRunOutcome> = {}): ScheduledRunOutcome {
  return {
    session_id: "sess-1",
    outcome: "completed",
    degraded: false,
    excerpt: "Closed: 600519.SH 1,710.00.",
    recorded_at: 1_789_000_000_000,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <Scheduled />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocked.listScheduledRuns.mockResolvedValue([]);
});

describe("Scheduled page", () => {
  it("renders the stored local cadence and timezone without UTC conversion", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run()]);
    renderPage();

    expect(await screen.findByText("Mon–Fri at 23:30")).toBeInTheDocument();
    const row = screen.getByRole("listitem");
    expect(within(row).getByText("Pacific/Auckland")).toBeInTheDocument();
    expect(within(row).getByText("pre-open scan of NZX names")).toBeInTheDocument();
  });

  it("shows legacy timezone-less jobs as UTC", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run({ timezone: null, schedule: "0 9 * * *" })]);
    renderPage();

    expect(await screen.findByText("Daily at 09:00")).toBeInTheDocument();
    expect(within(screen.getByRole("listitem")).getByText("UTC")).toBeInTheDocument();
  });

  it("creates a job from the wall-clock composer", async () => {
    mocked.createScheduledRun.mockResolvedValue(run());
    renderPage();
    await screen.findByText(/No scheduled runs yet/);

    fireEvent.change(screen.getByLabelText("Research prompt"), {
      target: { value: "scan the pre-open movers" },
    });
    fireEvent.change(screen.getByLabelText("Local time"), { target: { value: "23:30" } });
    fireEvent.submit(screen.getByRole("button", { name: /Schedule run/ }));

    await waitFor(() =>
      expect(mocked.createScheduledRun).toHaveBeenCalledWith(
        expect.objectContaining({
          prompt: "scan the pre-open movers",
          schedule: "30 23 * * 1-5",
          timezone: expect.any(String),
        }),
      ),
    );
  });

  it("surfaces a validation error from the backend", async () => {
    mocked.createScheduledRun.mockRejectedValue(
      new ApiError("timezone 'Not/AZone' is not a recognized IANA timezone key", 422),
    );
    renderPage();
    await screen.findByText(/No scheduled runs yet/);

    fireEvent.change(screen.getByLabelText("Research prompt"), {
      target: { value: "scan" },
    });
    fireEvent.submit(screen.getByRole("button", { name: /Schedule run/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "not a recognized IANA timezone",
    );
  });

  it("deletes only after an explicit confirm click, and cancel disarms", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run()]);
    mocked.deleteScheduledRun.mockResolvedValue(undefined);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Delete scheduled run/ }));
    expect(mocked.deleteScheduledRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: /Confirm deleting/ })).not.toBeInTheDocument();
    expect(mocked.deleteScheduledRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Delete scheduled run/ }));
    fireEvent.click(screen.getByRole("button", { name: /Confirm deleting/ }));
    await waitFor(() =>
      expect(mocked.deleteScheduledRun).toHaveBeenCalledWith("auckland-scan"),
    );
  });

  it("blocks a whitespace-only prompt client-side", async () => {
    renderPage();
    await screen.findByText(/No scheduled runs yet/);

    fireEvent.change(screen.getByLabelText("Research prompt"), {
      target: { value: "   " },
    });
    fireEvent.submit(screen.getByRole("button", { name: /Schedule run/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Enter a research prompt",
    );
    expect(mocked.createScheduledRun).not.toHaveBeenCalled();
  });
});


describe("briefing delivery", () => {
  it("keeps delivery off unless a channel is typed", async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText(/prompt/i), {
      target: { value: "pre-open scan" },
    });
    fireEvent.click(screen.getByRole("button", { name: /schedule|create/i }));

    await waitFor(() => expect(mocked.createScheduledRun).toHaveBeenCalled());
    const body = mocked.createScheduledRun.mock.calls[0][0];
    expect(body.delivery_channel).toBeNull();
    expect(body.delivery_target).toBeNull();
  });

  it("refuses a channel with no target instead of sending nowhere", async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText(/prompt/i), {
      target: { value: "pre-open scan" },
    });
    fireEvent.change(screen.getByLabelText(/deliver to channel/i), {
      target: { value: "telegram" },
    });
    fireEvent.click(screen.getByRole("button", { name: /schedule|create/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(mocked.createScheduledRun).not.toHaveBeenCalled();
  });

  it("sends the channel and target it was given", async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText(/prompt/i), {
      target: { value: "pre-open scan" },
    });
    fireEvent.change(screen.getByLabelText(/deliver to channel/i), {
      target: { value: " telegram " },
    });
    fireEvent.change(screen.getByLabelText(/channel target/i), {
      target: { value: " chat-9 " },
    });
    fireEvent.click(screen.getByRole("button", { name: /schedule|create/i }));

    await waitFor(() => expect(mocked.createScheduledRun).toHaveBeenCalled());
    const body = mocked.createScheduledRun.mock.calls[0][0];
    expect(body.delivery_channel).toBe("telegram");
    expect(body.delivery_target).toBe("chat-9");
  });

  it("shows a monitor's delivery state, and shows nothing when it has none", async () => {
    mocked.listScheduledRuns.mockResolvedValue([
      run({ id: "with", delivery_channel: "telegram", delivery_status: "sent" }),
      run({ id: "without" }),
    ]);
    renderPage();

    expect(await screen.findByText(/delivered to telegram/i)).toBeInTheDocument();
    expect(screen.queryAllByText(/delivered to/i)).toHaveLength(1);
  });

  it("surfaces why a delivery failed rather than only that it did", async () => {
    mocked.listScheduledRuns.mockResolvedValue([
      run({
        delivery_channel: "telegram",
        delivery_status: "failed",
        delivery_error: "channel unreachable",
      }),
    ]);
    renderPage();

    expect(await screen.findByText(/channel unreachable/i)).toBeInTheDocument();
  });
});

function verdict(overrides: Partial<VerdictRecord> = {}): VerdictRecord {
  return {
    session_id: "sess-1",
    recorded_at: 1_789_000_000_000,
    parse: "ok",
    outcome: "FLAT",
    items: [{ symbol: "600519.SH", state: "FLAT", reason: "band held" }],
    previous: null,
    ...overrides,
  };
}

describe("Scheduled page verdict cell", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.listScheduledRuns.mockResolvedValue([]);
  });

  it("renders the latest verdict with its delta and the recorded time", async () => {
    mocked.listScheduledRuns.mockResolvedValue([
      run({
        last_verdict: verdict({
          outcome: "DRIFT",
          items: [{ symbol: "600519.SH", state: "DRIFT", reason: "band crossed" }],
          previous: verdict({ session_id: "sess-0", outcome: "FLAT", recorded_at: 1_788_000_000_000 }),
        }),
      }),
    ]);
    renderPage();

    expect(await screen.findByText(/600519.SH DRIFT/)).toBeInTheDocument();
    expect(screen.getByText(/FLAT → DRIFT/)).toBeInTheDocument();
    expect(screen.getByText(/as of/)).toBeInTheDocument();
  });

  it("shows an explicit empty state when no run has recorded one", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run({ last_verdict: null })]);
    renderPage();

    expect(await screen.findByText("No verdict yet")).toBeInTheDocument();
  });

  it("renders nothing for ad-hoc monitors permanently at no_verdict_section", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run({ last_verdict: verdict({ parse: "no_verdict_section", items: [] }) })]);
    renderPage();

    await screen.findByRole("listitem");
    expect(screen.queryByText(/No verdict yet/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("verdict-line-auckland-scan")).toBeNull();
  });

  it("never shows a wrong verdict: a malformed section reads as unreadable", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run({ last_verdict: verdict({ parse: "contract_violation", items: [] }) })]);
    renderPage();

    expect(await screen.findByText("Latest verdict unreadable")).toBeInTheDocument();
  });

  it("reads an empty item list as a real 'no calls' answer", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run({ last_verdict: verdict({ items: [] }) })]);
    renderPage();

    expect(await screen.findByText(/No calls/)).toBeInTheDocument();
  });
});

describe("Scheduled page last run", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.listScheduledRuns.mockResolvedValue([]);
  });

  it("shows the run's own outcome, excerpt and a way into its session", async () => {
    // The wire status is "completed" after every healthy dispatch, so a row
    // that shows only that reads as done even when the run produced nothing
    // the reader can use.
    mocked.listScheduledRuns.mockResolvedValue([
      run({
        status: "completed",
        last_run: outcome(),
        last_run_session_id: "sess-1",
      }),
    ]);
    renderPage();

    expect(await screen.findByText(/Result produced/)).toBeInTheDocument();
    expect(screen.queryByText("Completed")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-excerpt-auckland-scan")).toHaveTextContent(
      "Closed: 600519.SH 1,710.00.",
    );
    expect(screen.getByTestId("run-open-auckland-scan")).toHaveAttribute(
      "href",
      "/?session=sess-1",
    );
  });

  it("marks a deterministic fallback as degraded, not as a produced result", async () => {
    mocked.listScheduledRuns.mockResolvedValue([
      run({
        status: "completed",
        last_run: outcome({ degraded: true }),
        last_run_session_id: "sess-1",
      }),
    ]);
    renderPage();

    expect(await screen.findByText(/Fallback output/)).toBeInTheDocument();
    expect(screen.queryByText(/Result produced/)).not.toBeInTheDocument();
  });

  it("shows a failed run as failed while the job itself stays schedulable", async () => {
    mocked.listScheduledRuns.mockResolvedValue([
      run({
        status: "completed",
        last_run: outcome({ outcome: "failed", excerpt: null }),
        last_run_session_id: "sess-1",
      }),
    ]);
    renderPage();

    expect(await screen.findByText(/Run failed/)).toBeInTheDocument();
    expect(screen.queryByTestId("run-excerpt-auckland-scan")).toBeNull();
    // The link still reaches the session the failure happened in.
    expect(screen.getByTestId("run-open-auckland-scan")).toBeInTheDocument();
  });

  it("says the run is in flight once dispatched but before it lands", async () => {
    mocked.listScheduledRuns.mockResolvedValue([
      run({ status: "completed", last_run: null, last_run_session_id: "sess-9" }),
    ]);
    renderPage();

    expect(await screen.findByText("Producing the result…")).toBeInTheDocument();
    expect(screen.queryByTestId("run-result-auckland-scan")).toBeNull();
  });

  it("adds nothing before the first dispatch", async () => {
    mocked.listScheduledRuns.mockResolvedValue([run()]);
    renderPage();

    expect(await screen.findByText("No verdict yet")).toBeInTheDocument();
    expect(screen.queryByTestId("run-result-auckland-scan")).toBeNull();
    expect(screen.queryByTestId("run-result-pending-auckland-scan")).toBeNull();
  });
});
