"use client";

// Runtime health / demo readiness / production qualification status cards.
// Frozen baseline: Requirements r3 / Design r9 §13.2.
// Contract coverage: src/app/(dashboard)/page.test.tsx
//
// Design r9 §13.2: the three states are deliberately independent.  Demo
// readiness must never be derived from runtime health, and production
// qualification stays out of scope (HOLD) for the demo.

export type RuntimeHealthState = "healthy" | "degraded" | "unknown";
export type DemoReadinessState = "ready" | "degraded" | "not_implemented" | "unknown";
export type QualificationState = "hold" | "out_of_scope";

export interface StatusCardsV3Props {
  runtimeHealth?: RuntimeHealthState;
  demoReadiness?: DemoReadinessState;
  qualification?: QualificationState;
  lastObservedAt?: string | null;
}

const RUNTIME_LABEL: Record<RuntimeHealthState, string> = {
  healthy: "运行正常",
  degraded: "运行降级",
  unknown: "状态未知",
};

const DEMO_LABEL: Record<DemoReadinessState, string> = {
  ready: "演示就绪",
  degraded: "演示降级",
  not_implemented: "未实现",
  unknown: "演示未知",
};

const QUALIFICATION_LABEL: Record<QualificationState, string> = {
  hold: "生产验证 HOLD",
  out_of_scope: "生产验证不在范围",
};

function StateDot({ state }: { state: "ok" | "warn" | "bad" | "muted" }) {
  const className =
    state === "ok"
      ? "bg-emerald-500"
      : state === "warn"
        ? "bg-amber-500"
        : state === "bad"
          ? "bg-destructive"
          : "bg-muted-foreground";
  return <span aria-hidden="true" className={`inline-block h-2 w-2 rounded-full ${className}`} />;
}

export function StatusCardsV3({
  runtimeHealth = "unknown",
  demoReadiness = "not_implemented",
  qualification = "out_of_scope",
  lastObservedAt = null,
}: StatusCardsV3Props) {
  const runtimeState =
    runtimeHealth === "healthy" ? "ok" : runtimeHealth === "degraded" ? "warn" : "bad";
  const demoState =
    demoReadiness === "ready" ? "ok" : demoReadiness === "degraded" ? "warn" : "muted";
  const qualificationState = "muted";

  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <section className="rounded-md border border-border bg-card p-3" aria-label="运行状态">
        <div className="flex items-center gap-2 text-sm font-medium">
          <StateDot state={runtimeState} />
          <span>运行状态</span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">{RUNTIME_LABEL[runtimeHealth]}</p>
        {lastObservedAt ? (
          <p className="mt-1 text-xs text-muted-foreground">最后观测 {lastObservedAt}</p>
        ) : null}
      </section>

      <section className="rounded-md border border-border bg-card p-3" aria-label="演示就绪">
        <div className="flex items-center gap-2 text-sm font-medium">
          <StateDot state={demoState} />
          <span>演示就绪</span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">{DEMO_LABEL[demoReadiness]}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          仅表示演示链可用，不代表生产资格
        </p>
      </section>

      <section className="rounded-md border border-border bg-card p-3" aria-label="生产验证">
        <div className="flex items-center gap-2 text-sm font-medium">
          <StateDot state={qualificationState} />
          <span>生产验证</span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">{QUALIFICATION_LABEL[qualification]}</p>
        <p className="mt-1 text-xs text-muted-foreground">HOLD / 不在本次范围</p>
      </section>
    </div>
  );
}
