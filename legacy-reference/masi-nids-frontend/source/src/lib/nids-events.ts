import type { NidsEvent } from "@/types/api";

export function eventAcceptedByThreshold(event: NidsEvent): boolean {
  return event.raw_event?.accepted_by_threshold === true;
}

export function isKnownAttackAnomaly(event: NidsEvent): boolean {
  return (
    event.decision === "anomaly" &&
    event.nearest_class != null &&
    event.nearest_class !== "" &&
    event.nearest_class !== "normal" &&
    eventAcceptedByThreshold(event)
  );
}
