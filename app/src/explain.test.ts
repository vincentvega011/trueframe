import { describe, expect, it } from "vitest";
import { explainCheck, explainFrame } from "./explain";

const bad = (name: string, detail: string, metrics?: Record<string, unknown>) => ({ name, passed: false, detail, metrics });

describe("plain-language sentences", () => {
  it("reports the share of differing pixels for the re-render check", () => {
    expect(explainCheck(bad("seg_rerender", "x", { global_agreement: 0.8728 }))).toBe("Labels don't match the scene: 13% of pixels differ from the verifier's re-render.");
    expect(explainCheck(bad("seg_rerender", "x", { global_agreement: 0.9999 }))).toContain("under 0.1%");
  });
  it("names the class and the overshoot for a box that leaves its projection", () => {
    expect(explainCheck(bad("annotation_plausibility", "object 1 (rack): mask box [1] leaves the projected box [2] by 69.3px (tolerance 1.0px)"))).toBe("Boxes don't match the scene: the rack box sticks out 69 px beyond where the scene puts it.");
    expect(explainCheck(bad("annotation_plausibility", "object 1 (rack): bbox [254, -22, 401, 480] is empty or outside the 640x480 image"))).toContain("outside the image");
  });
  it("says when a box was drawn for an object that is not in view", () => {
    expect(explainCheck(bad("annotation_plausibility", "object 1 (rack) claims a visible box [441, 120, 470, 150] but its projection is behind_camera"))).toBe("Boxes don't match the scene: the node drew a box for a rack that is not in view.");
  });
  it("leads with the most telling failure and counts the rest", () => {
    const f = { index: 3, status: "checked" as const, passed: false, checks: [bad("segmentation_consistency", "y"), bad("annotation_plausibility", "object 2 (box): by 5.0px")] };
    expect(explainFrame(f)).toBe("Boxes don't match the scene: the box box sticks out 5 px beyond where the scene puts it. 1 more check failed too.");
  });
  it("has nothing to say about a passing frame and explains an unserved one", () => {
    expect(explainFrame({ index: 0, status: "checked", passed: true, checks: [] })).toBeNull();
    expect(explainFrame({ index: 0, status: "reveal_unavailable", passed: false, checks: [] })).toBe("The node did not serve this frame.");
  });
});
