import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { GlyphCoverage } from "../api/types";
import {
  GLYPH_ANSWERS_KEPT,
  GLYPH_ASKS_IN_FLIGHT,
  askMissingGlyphs,
  forgetGlyphAnswers,
} from "./useMissingGlyphs";

describe("askMissingGlyphs", () => {
  beforeEach(() => forgetGlyphAnswers());
  afterEach(() => vi.restoreAllMocks());

  it("keeps a bounded number of answers, forgetting the least recently asked", async () => {
    const asked = vi
      .spyOn(api, "getGlyphs")
      .mockImplementation(async (font) => ({ font, missing: [] }));
    await askMissingGlyphs("Lobster Two", "kept");
    for (let n = 0; n < GLYPH_ANSWERS_KEPT; n += 1) {
      await askMissingGlyphs("Lobster Two", `text ${n}`);
      // Asked again on the way: the most recent, so never the one forgotten.
      if (n === 0) await askMissingGlyphs("Lobster Two", "kept");
    }
    const before = asked.mock.calls.length;
    await askMissingGlyphs("Lobster Two", "kept");
    await askMissingGlyphs("Lobster Two", `text ${GLYPH_ANSWERS_KEPT - 1}`);
    expect(asked.mock.calls.length).toBe(before);
    // One past the bound pushed out the oldest: it is asked again.
    await askMissingGlyphs("Lobster Two", "text 0");
    expect(asked.mock.calls.length).toBe(before + 1);
  });

  it("sends only a few asks at once, the rest as those answer", async () => {
    const pending: ((coverage: GlyphCoverage) => void)[] = [];
    const asked = vi
      .spyOn(api, "getGlyphs")
      .mockImplementation(
        () => new Promise<GlyphCoverage>((resolve) => pending.push(resolve)),
      );
    const answers = Array.from({ length: GLYPH_ASKS_IN_FLIGHT + 3 }, (_, n) =>
      askMissingGlyphs(`Family ${n}`, "Reagan"),
    );
    await vi.waitFor(() =>
      expect(asked).toHaveBeenCalledTimes(GLYPH_ASKS_IN_FLIGHT),
    );
    pending.shift()?.({ font: "Family 0", missing: ["😀"] });
    await vi.waitFor(() =>
      expect(asked).toHaveBeenCalledTimes(GLYPH_ASKS_IN_FLIGHT + 1),
    );
    expect(await answers[0]).toEqual(["😀"]);
    while (asked.mock.calls.length < answers.length || pending.length > 0) {
      await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
      pending.shift()?.({ font: "", missing: [] });
    }
    await Promise.all(answers);
    expect(asked).toHaveBeenCalledTimes(answers.length);
  });
});
