import { beforeEach, describe, expect, it } from "vitest";

import {
  claimDynamicImportReload,
  clearDynamicImportReloadMarker,
  isDynamicImportFailure,
} from "./dynamicImportRecovery";

describe("dynamic import recovery", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("recognizes browser chunk and module loading failures", () => {
    expect(isDynamicImportFailure(new TypeError("Failed to fetch dynamically imported module: /assets/Page-old.js"))).toBe(true);
    expect(isDynamicImportFailure(new Error("Loading chunk 12 failed"))).toBe(true);
    expect(isDynamicImportFailure(new Error("Ordinary render error"))).toBe(false);
  });

  it("permits only one automatic reload inside the guard window", () => {
    const error = new TypeError("Failed to fetch dynamically imported module: /assets/Page-old.js");
    expect(claimDynamicImportReload(error, 10_000)).toBe(true);
    expect(claimDynamicImportReload(error, 20_000)).toBe(false);
    expect(claimDynamicImportReload(error, 80_001)).toBe(true);
  });

  it("clears the guard after a current module loads successfully", () => {
    const error = new TypeError("Failed to fetch dynamically imported module: /assets/Page-old.js");
    expect(claimDynamicImportReload(error, 10_000)).toBe(true);
    clearDynamicImportReloadMarker();
    expect(claimDynamicImportReload(error, 10_001)).toBe(true);
  });
});
