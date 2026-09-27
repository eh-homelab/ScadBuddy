import { act, renderHook } from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type MockInstance,
} from "vitest";
import { api } from "../api/client";
import type { Job } from "../api/types";
import { useRenderJob } from "./useRenderJob";

const JOB_A = "a".repeat(32);
const JOB_B = "b".repeat(32);
const JOB_C = "c".repeat(32);

function job(id: string, status: Job["status"]): Job {
  return {
    id,
    slug: "demo",
    status,
    created_at: "2026-09-27T00:00:00Z",
    params: {},
    log_tail: [],
  } as Job;
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe("useRenderJob", () => {
  let submit: MockInstance<typeof api.render>;
  let read: MockInstance<typeof api.getJob>;
  const ids = [JOB_A, JOB_B, JOB_C];

  beforeEach(() => {
    vi.useFakeTimers();
    let next = 0;
    submit = vi.spyOn(api, "render").mockImplementation(async () => {
      const id = ids[next++]!;
      return { job_id: id, status_url: `/api/v1/jobs/${id}` };
    });
    read = vi.spyOn(api, "getJob");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("tells the server which unfinished render a new one replaces", async () => {
    read.mockImplementation(async (id) => job(id, "pending"));
    const { rerender } = renderHook(
      ({ params }) => useRenderJob("demo", params),
      {
        initialProps: { params: { n: 1 } },
      },
    );
    await settle();
    rerender({ params: { n: 2 } });
    await settle();

    expect(submit).toHaveBeenNthCalledWith(
      1,
      "demo",
      { n: 1 },
      undefined,
      undefined,
    );
    expect(submit).toHaveBeenNthCalledWith(
      2,
      "demo",
      { n: 2 },
      undefined,
      JOB_A,
    );
  });

  it("supersedes nothing once the last render has settled", async () => {
    read.mockImplementation(async (id) => job(id, "done"));
    const { rerender } = renderHook(
      ({ params }) => useRenderJob("demo", params),
      {
        initialProps: { params: { n: 1 } },
      },
    );
    await settle();
    rerender({ params: { n: 2 } });
    await settle();

    expect(submit).toHaveBeenNthCalledWith(
      2,
      "demo",
      { n: 2 },
      undefined,
      undefined,
    );
  });
});
