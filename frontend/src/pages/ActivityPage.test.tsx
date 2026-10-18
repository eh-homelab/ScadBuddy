import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { useLocation, useNavigate } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { server } from "../mocks/server";
import { fakeRealtime } from "../lib/realtime.fake";
import { getRealtime } from "../lib/realtime";
import { seedActivity, setActivityFacets, setActivityListCursor } from "../mocks/features/activity";
import { resetMockState } from "../mocks/handlers";
import { renderPage } from "../test/utils";
import type { Activity } from "../api/types";
import { CHIPS_SHOWN, principalLabel, sinceTime } from "../components/activity/labels";
import { ActivityPage } from "./ActivityPage";

function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">{`${location.pathname}${location.search}`}</output>
  );
}

function Back() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => void navigate(-1)}>
      Go back
    </button>
  );
}

// A role query walks every row; with 250 of them it alone outlasts a loaded runner.
const button = (name: string) => screen.getByText(name, { selector: "button" });

function render(route = "/activity") {
  return renderPage(
    <>
      <ActivityPage />
      <Location />
      <Back />
    </>,
    { route },
  );
}

function activity(
  id: string,
  at: string,
  extra: Partial<Activity> = {},
): Activity {
  return {
    id,
    kind: "render",
    status: "running",
    title: `Row ${id}`,
    principal: "browser",
    session: null,
    via_tool: null,
    subject: "",
    parent_id: null,
    detail: {},
    created_at: at,
    updated_at: at,
    finished_at: null,
    ...extra,
  };
}

/** `count` rows, newest first, one minute apart. */
function many(count: number): Activity[] {
  return Array.from({ length: count }, (_, i) =>
    activity(
      `r${String(i).padStart(3, "0")}`,
      new Date(Date.UTC(2026, 9, 9, 12) - i * 60_000).toISOString(),
    ),
  );
}

describe("labels", () => {
  it("names the principals", () => {
    expect(principalLabel("browser")).toBe("You");
    expect(principalLabel("system")).toBe("System");
    expect(principalLabel(null)).toBe("Unknown");
    expect(principalLabel("token:t1")).toBe("token:t1");
    expect(principalLabel("Zo%C3%AB")).toBe("Zoë");
    expect(principalLabel("100%")).toBe("100%");
  });

  it("turns a since token into a time, and an unknown one into none", () => {
    const now = Date.parse("2026-10-09T12:00:00Z");
    expect(sinceTime("24h", now)).toBe("2026-10-08T12:00:00.000Z");
    expect(sinceTime("7d", now)).toBe("2026-10-02T12:00:00.000Z");
    expect(sinceTime("bogus", now)).toBe("");
    expect(sinceTime("", now)).toBe("");
  });
});

describe("ActivityPage", () => {
  beforeEach(() => {
    resetMockState();
    window.localStorage.clear();
  });
  afterEach(() => vi.useRealTimers());

  it("builds its filter chips from the facets, including a kind it has no code for", async () => {
    render();
    expect(
      await screen.findByRole("button", { name: "Install a font" }),
    ).toBeInTheDocument();
    // Started by takes one value: its chips are radios.
    expect(screen.getByRole("radio", { name: /^You/ })).toBeInTheDocument();
  });

  it("is its own scroll container, so rows below the filters can be reached (#3747)", async () => {
    render();
    await screen.findAllByTestId(/^activity-row-/);
    expect(screen.getByTestId("activity-page")).toHaveClass("h-full", "overflow-y-auto");
  });

  it("shows the most frequent subjects, keeps a chosen one, and shows the rest on asking (#3747)", async () => {
    const subjects = Array.from({ length: CHIPS_SHOWN + 5 }, (_, i) => ({
      value: `subject-${String(i).padStart(2, "0")}`,
      count: 100 - i,
    }));
    const facets = await (await fetch("/api/v1/activity/facets")).json();
    setActivityFacets({ ...facets, subjects });
    const { user } = render(`/activity?subject=subject-${CHIPS_SHOWN + 3}`);
    const group = await screen.findByLabelText("Subject");
    await waitFor(() => expect(within(group).getAllByRole("radio")).toHaveLength(CHIPS_SHOWN + 1));
    expect(within(group).getByRole("radio", { name: /^subject-00/ })).toBeInTheDocument();
    expect(within(group).getByRole("radio", { name: /^subject-13/, checked: true })).toBeInTheDocument();
    expect(within(group).queryByRole("radio", { name: /^subject-14/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Subject: show 4 more" }));
    expect(within(group).getAllByRole("radio")).toHaveLength(CHIPS_SHOWN + 5);
    await user.click(screen.getByRole("button", { name: "Subject: show fewer" }));
    expect(within(group).getAllByRole("radio")).toHaveLength(CHIPS_SHOWN + 1);
  });

  it("names a subject chip, and filters by its reference (#3856)", async () => {
    const facets = await (await fetch("/api/v1/activity/facets")).json();
    setActivityFacets({
      ...facets,
      subjects: [
        { value: "keychain", count: 4, label: "Name Keychain" },
        { value: "library:89", count: 3 },
        { value: "project:7", count: 2 },
        { value: `output:${"a".repeat(32)}`, count: 1 },
      ],
    });
    const { user } = render();
    const group = await screen.findByLabelText("Subject");
    const names = await waitFor(() => {
      const radios = within(group).getAllByRole("radio");
      expect(radios).toHaveLength(4);
      return radios.map((r) => r.textContent);
    });
    expect(names).toEqual(["Name Keychain4", "Library file 893", "Project 72", "Output aaaaaaaa1"]);
    await user.click(within(group).getByRole("radio", { name: /^Library file 89/ }));
    expect(screen.getByTestId("location").textContent).toContain("subject=library%3A89");
  });

  it("filters by status and keeps it in the URL", async () => {
    const { user } = render();
    await user.click(await screen.findByRole("button", { name: "Failed" }));
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(1),
    );
    expect(screen.getByTestId("location").textContent).toContain(
      "status=failed",
    );
  });

  it("shows a kind’s own facets once the kind is selected", async () => {
    const { user } = render();
    expect(screen.queryByLabelText("Template")).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Render" }));
    expect(await screen.findByLabelText("Template")).toBeInTheDocument();
  });

  it("updates live when an activity changes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const realtime = fakeRealtime();
    render();
    await screen.findAllByTestId(/^activity-row-/);
    seedActivity([
      {
        id: "render:new",
        kind: "render",
        status: "running",
        title: "Render lid",
        principal: "browser",
        session: null,
        via_tool: null,
        subject: "",
        parent_id: null,
        detail: {},
        created_at: "2026-10-09T11:00:00Z",
        updated_at: "2026-10-09T11:00:00Z",
        finished_at: null,
      },
    ]);
    await realtime.signal("activity", "activity.changed", {
      activity_id: "render:new",
    });
    expect(await screen.findByText("Render lid")).toBeInTheDocument();
  });

  it("loads more, and a live insert loses no row", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const realtime = fakeRealtime();
    seedActivity(many(60));
    const { user } = render();
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50),
    );
    await user.click(button("Load more"));
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(60),
    );
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    seedActivity([activity("new", "2026-10-09T13:00:00Z"), ...many(60)]);
    await realtime.signal("activity", "activity.changed", {
      activity_id: "new",
    });
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(61),
    );
    expect(screen.getByTestId("activity-row-r059")).toBeInTheDocument();
  });

  it("restores chips and a facet input from a deep link", async () => {
    render("/activity?kind=render&facet.slug=box");
    expect(
      await screen.findByRole(
        "button",
        { name: "Render", pressed: true },
        { timeout: 5000 },
      ),
    ).toBeInTheDocument();
    expect(
      await screen.findByLabelText("Template", {}, { timeout: 5000 }),
    ).toHaveValue("box");
  });

  it("shows an applied value the facets do not list, and clears it", async () => {
    const { user } = render("/activity?principal=ghost");
    expect(
      await screen.findByRole("radio", { name: "ghost", checked: true }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/activity"),
    );
  });

  it("nests a child under its parent when both are loaded", async () => {
    seedActivity([
      activity("child", "2026-10-09T12:05:00Z", { parent_id: "parent" }),
      activity("other", "2026-10-09T12:03:00Z"),
      activity("parent", "2026-10-09T12:00:00Z"),
    ]);
    render();
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(3),
    );
    const order = screen
      .getAllByTestId(/^activity-row-/)
      .map((r) => r.getAttribute("data-testid"));
    expect(order).toEqual([
      "activity-row-other",
      "activity-row-parent",
      "activity-row-child",
    ]);
    expect(
      within(screen.getByTestId("activity-row-child"))
        .getByText("Row child")
        .closest("td"),
    ).toHaveClass("pl-8");
  });

  it("keeps the loaded rows when a Load more fails, and says to try again", async () => {
    seedActivity(many(60));
    const { user } = render();
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50),
    );
    server.use(
      http.get("/api/v1/activity", () =>
        HttpResponse.json({ detail: "down" }, { status: 500 }),
      ),
    );
    await user.click(button("Load more"));
    expect(
      await screen.findByText("Could not load more. Try again."),
    ).toBeInTheDocument();
    expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50);
    expect(button("Load more")).toBeEnabled();
  });

  it("pauses live updates past the cap, and Show latest brings them back", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const realtime = fakeRealtime();
    seedActivity(many(260));
    const { user } = render();
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50),
    );
    for (const n of [100, 150, 200]) {
      await user.click(button("Load more"));
      await waitFor(() =>
        expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(n),
      );
    }
    expect(screen.queryByText(/Live updates paused/)).toBeNull();
    await user.click(button("Load more"));
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(250),
    );
    expect(
      screen.getByText(/Live updates paused, showing older activity/),
    ).toBeInTheDocument();
    seedActivity([activity("new", "2026-10-09T13:00:00Z"), ...many(260)]);
    await realtime.signal("activity", "activity.changed", {
      activity_id: "new",
    });
    expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(250);
    expect(screen.queryByTestId("activity-row-new")).toBeNull();
    await user.click(button("Show latest"));
    expect(await screen.findByTestId("activity-row-new")).toBeInTheDocument();
    expect(screen.queryByText(/Live updates paused/)).toBeNull();
  }, 60_000);

  it("reads the list once for a burst of signals", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const realtime = fakeRealtime();
    const list = vi.spyOn(api, "listActivity");
    render();
    await screen.findAllByTestId(/^activity-row-/);
    await vi.advanceTimersByTimeAsync(2000);
    const before = list.mock.calls.length;
    for (let i = 0; i < 5; i++)
      await realtime.signal("activity", "activity.changed");
    expect(list.mock.calls.length).toBe(before);
    await vi.advanceTimersByTimeAsync(700);
    expect(list.mock.calls.length).toBe(before + 1);
  });

  it("re-reads the facets at most once per throttle window", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const realtime = fakeRealtime();
    const facets = vi.spyOn(api, "activityFacets");
    render();
    await screen.findAllByTestId(/^activity-row-/);
    await vi.advanceTimersByTimeAsync(7000);
    const before = facets.mock.calls.length;
    for (let i = 0; i < 3; i++)
      await realtime.signal("activity", "activity.changed");
    expect(facets.mock.calls.length).toBe(before + 1);
    await vi.advanceTimersByTimeAsync(3500);
    expect(facets.mock.calls.length).toBe(before + 2);
  });

  it("drops the held pages when the query changes, and Back shows a fresh first page", async () => {
    seedActivity(many(260));
    const { user } = render();
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50),
    );
    for (const n of [100, 150, 200, 250]) {
      await user.click(button("Load more"));
      await waitFor(() =>
        expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(n),
      );
    }
    expect(screen.getByText(/Live updates paused/)).toBeInTheDocument();
    await user.click(
      within(screen.getByLabelText("Activity filters")).getByRole("button", {
        name: "Running",
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toContain(
        "status=running",
      ),
    );
    await user.click(screen.getByRole("button", { name: "Go back" }));
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/activity"),
    );
    await waitFor(() =>
      expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50),
    );
    expect(screen.queryByText(/Live updates paused/)).toBeNull();
  }, 60_000);
});

describe("ActivityPage's first read and the subscription's confirmation (#1039)", () => {
  beforeEach(() => {
    resetMockState();
    window.localStorage.clear();
    setActivityListCursor("e5");
  });
  let started: (({ request }: { request: Request }) => void) | undefined;
  afterEach(() => {
    vi.useRealTimers();
    if (started) server.events.removeListener("request:start", started);
    started = undefined;
  });

  /** How many times the list itself is read. */
  function countLists() {
    let reads = 0;
    started = ({ request }) => {
      if (new URL(request.url).pathname === "/api/v1/activity") reads += 1;
    };
    server.events.on("request:start", started);
    return () => reads;
  }

  async function settle() {
    await screen.findAllByTestId(/^activity-row-/);
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
  }

  it("reads the list once when its first read saw the server's last event", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fakeRealtime({ confirmation: { last: "e5" } });
    const check = vi.spyOn(getRealtime(), "check");
    const reads = countLists();
    render();
    await settle();
    expect(reads()).toBe(1);
    expect(check).not.toHaveBeenCalled();
  });

  it("asks the server otherwise, and reads again only when it names the topic", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fakeRealtime({ confirmation: { last: "e9" } });
    const check = vi.spyOn(getRealtime(), "check").mockResolvedValue([]);
    const reads = countLists();
    render();
    await settle();
    expect(check).toHaveBeenCalledWith(["e5"], ["activity"]);
    expect(reads()).toBe(1);
  });

  it("reads again when the server names the topic", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fakeRealtime({ confirmation: { last: "e9" } });
    vi.spyOn(getRealtime(), "check").mockResolvedValue(["activity"]);
    const reads = countLists();
    render();
    await settle();
    expect(reads()).toBe(2);
  });
});

