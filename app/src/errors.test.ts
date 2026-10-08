import { describe, expect, it } from "vitest";
import { errorName, explainError, programErrorFrom } from "./errors";

describe("program errors come from the IDL", () => {
  it("maps codes through the loaded IDL table", () => {
    expect(errorName(6017)).toBe("DeadlineNotReached");
    expect(errorName(6012)).toBe("InvalidStatus");
  });
  it("prefers Anchor's own log line", () => {
    const e = explainError(new Error("boom"), ["Program log: AnchorError thrown. Error Code: InvalidStatus. Error Number: 6012. Error Message: x"]);
    expect(e.programError).toBe("InvalidStatus");
    expect(e.message).toBe("Transaction failed: InvalidStatus (6012)");
  });
  it("falls back to the raw custom code", () => {
    expect(programErrorFrom("Transaction simulation failed: custom program error: 0x1781")).toEqual({ name: "DeadlineNotReached", code: 6017 });
  });
  it("names an unknown code honestly", () => {
    expect(explainError(new Error("custom program error: 0x270f")).message).toBe("Transaction failed: UnknownProgramError (9999)");
  });
  it("explains a cancelled signature and an unreachable RPC in plain words", () => {
    expect(explainError(new Error("User rejected the request.")).message).toMatch(/cancelled/);
    expect(explainError(new TypeError("Failed to fetch")).message).toMatch(/Can't reach the RPC/);
  });
});
