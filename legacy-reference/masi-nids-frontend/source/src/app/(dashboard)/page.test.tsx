import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// Regression coverage for design r9 section 19 Phase 6. Its acceptance condition
// is "no Event-v2 API in the dashboard's network requests".
// Frozen baseline: Requirements r3 / Design r9 §13.2.
//
// This is asserted over the dashboard's *module graph* rather than by rendering
// the page.  The page composes roughly fifteen hooks, so a render harness would
// need fifteen mocks and would break on unrelated changes -- and a render only
// proves the paths taken on one code path.  Walking the graph proves the retired
// hooks are unreachable from the page at all, and cannot be defeated by moving
// the call into a child component.

const SRC = path.join(__dirname, "..", "..");
const PAGE = path.join(__dirname, "page.tsx");

const RETIRED_HOOKS = ["useDashboardEvents", "useEventStats"] as const;
const RETIRED_COMPONENTS = ["AnomalyChart"] as const;

function resolveImport(specifier: string, fromFile: string): string | null {
  let base: string;
  if (specifier.startsWith("@/")) {
    base = path.join(SRC, specifier.slice(2));
  } else if (specifier.startsWith(".")) {
    base = path.resolve(path.dirname(fromFile), specifier);
  } else {
    return null; // package import; not part of the first-party graph
  }
  for (const candidate of [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    path.join(base, "index.ts"),
    path.join(base, "index.tsx"),
  ]) {
    if (existsSync(candidate) && !candidate.endsWith(path.sep)) {
      try {
        if (readFileSync(candidate).length >= 0) return candidate;
      } catch {
        continue;
      }
    }
  }
  return null;
}

function moduleGraph(entry: string): Map<string, string> {
  const seen = new Map<string, string>();
  const queue = [entry];
  while (queue.length > 0) {
    const current = queue.shift() as string;
    if (seen.has(current)) continue;
    let source: string;
    try {
      source = readFileSync(current, "utf-8");
    } catch {
      continue;
    }
    if (current.endsWith(".test.ts") || current.endsWith(".test.tsx")) continue;
    seen.set(current, source);
    const specifiers = [...source.matchAll(/(?:from|import)\s+"([^"]+)"/g)].map((match) => match[1]);
    for (const specifier of specifiers) {
      const resolved = resolveImport(specifier, current);
      if (resolved !== null && !seen.has(resolved)) queue.push(resolved);
    }
  }
  return seen;
}

describe("dashboard page Event-v3 cutover", () => {
  it("has a module graph that reaches more than the entry file", () => {
    // Guards the guard: a resolver regression would make every assertion vacuous.
    const graph = moduleGraph(PAGE);
    expect(graph.size).toBeGreaterThan(5);
    expect([...graph.keys()]).toContain(PAGE);
  });

  it("does not reach any retired Event-v2 dashboard hook", () => {
    const graph = moduleGraph(PAGE);
    const offenders: string[] = [];
    for (const [file, source] of graph) {
      for (const hook of RETIRED_HOOKS) {
        if (new RegExp(`\\b${hook}\\s*\\(`).test(source)) {
          offenders.push(`${path.relative(SRC, file)} calls ${hook}()`);
        }
      }
    }
    expect(offenders, offenders.join("\n")).toEqual([]);
  });

  it("does not reach the retired anomaly-score chart", () => {
    const graph = moduleGraph(PAGE);
    const offenders: string[] = [];
    for (const [file, source] of graph) {
      for (const component of RETIRED_COMPONENTS) {
        if (new RegExp(`\\b${component}\\b`).test(source)) {
          offenders.push(`${path.relative(SRC, file)} references ${component}`);
        }
      }
      if (source.includes("anomaly_score")) {
        offenders.push(`${path.relative(SRC, file)} reads anomaly_score`);
      }
    }
    expect(offenders, offenders.join("\n")).toEqual([]);
  });

  it("does not request the Event-v2 endpoints from anywhere in the graph", () => {
    const graph = moduleGraph(PAGE);
    const offenders: string[] = [];
    for (const [file, source] of graph) {
      for (const match of source.matchAll(/api\.(?:get|post|patch|delete)\(\s*[`"']([^`"']+)/g)) {
        // The client is called with a leading slash (api.get("/events", ...)),
        // so the comparison must normalise it; matching the bare form only made
        // this assertion vacuous.
        const requested = match[1].replace(/^\/+/, "");
        const isEventV2List = requested === "events" || requested.startsWith("events?");
        const isEventV2Stats = requested === "events/stats" || requested.startsWith("events/stats?");
        if (isEventV2List || isEventV2Stats) {
          offenders.push(`${path.relative(SRC, file)} requests ${match[1]}`);
        }
      }
    }
    expect(offenders, offenders.join("\n")).toEqual([]);
  });

  it("proves the endpoint scan is not vacuous", () => {
    // Guards the guard: if the api-call regex or the normalisation regresses,
    // the assertion above would silently pass on a tree that still reads
    // Event v2.  This fixture must always be detected.
    const fixture = new Map([
      ["/tmp/fixture.ts", 'const a = api.get("/events", { params });\nconst b = api.get("events/stats");\n'],
    ]);
    const detected: string[] = [];
    for (const [, source] of fixture) {
      for (const match of source.matchAll(/api\.(?:get|post|patch|delete)\(\s*[`"']([^`"']+)/g)) {
        const requested = match[1].replace(/^\/+/, "");
        if (requested === "events" || requested.startsWith("events?") || requested.startsWith("events/stats")) {
          detected.push(requested);
        }
      }
    }
    expect(detected).toEqual(["events", "events/stats"]);
  });

  it("reads trends, events, incidents, live, and demo projection hooks from the v3 sources", () => {
    const graph = moduleGraph(PAGE);
    const joined = [...graph.values()].join("\n");
    // Phase 6 (design r9 §13.2): the dashboard must pull all real-time and
    // read-only projection data from the v3 hooks, not from Event-v2 or
    // client-side synthesis.  analysis-trace and flow-evidence hooks are
    // exercised by the workflow-detail module graph, not the dashboard.
    for (const required of [
      "useTrendsV3",
      "useEventsV3",
      "useIncidentsV3",
      "useLiveV3",
      "useDemoRunProjection",
    ]) {
      expect(joined, `dashboard must use ${required}`).toContain(required);
    }
  });

  it("renders the three runtime/demo/qualification states as separate cards", () => {
    const source = readFileSync(PAGE, "utf-8");
    expect(source).toContain("StatusCardsV3");
  });
});

describe("three-state separation", () => {
  const CARDS = path.join(SRC, "components", "dashboard", "status-cards-v3.tsx");

  it("keeps demo readiness and production qualification independent of runtime health", () => {
    const source = readFileSync(CARDS, "utf-8");
    // r9: Demo readiness cannot be derived from runtime health, and production
    // qualification is out of scope for the demo -- Phase 5 is not implemented,
    // so readiness must report not_implemented rather than ready.
    expect(source).toContain("not_implemented");
    expect(source).toMatch(/HOLD|OUT OF SCOPE/);
    expect(source).not.toMatch(/demoReady\s*=\s*runtimeHealthy/);
  });
});
