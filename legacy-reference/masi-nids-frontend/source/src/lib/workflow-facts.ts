import type { Locale, TranslationKey } from "@/lib/messages";

type Translate = (key: TranslationKey) => string;

const FACT_LABEL_KEYS: Record<string, TranslationKey> = {
  decision: "workflow.fact.decision",
  nearest_class: "workflow.fact.nearestClass",
  flow_id: "workflow.fact.flowId",
  target_deployment: "workflow.fact.targetDeployment",
  target_table: "workflow.fact.targetTable",
  target_status: "workflow.fact.targetStatus",
};

const DECISION_VALUE_KEYS: Record<string, TranslationKey> = {
  anomaly: "workflow.fact.decision.anomaly",
  normal: "workflow.fact.decision.normal",
  unknown: "workflow.fact.decision.unknown",
};

const FIXED_REVIEW_TEXT_KEYS: Record<string, TranslationKey> = {
  "No single NIDS event was bound to this workflow.": "workflow.reviewText.noSingleEvent",
  "Triage skipped: restore_rule has no anomaly event context.": "workflow.reviewText.restoreNoEvent",
};

const PROTOCOL_LABELS: Record<string, string> = {
  "1": "ICMP",
  "6": "TCP",
  "17": "UDP",
  icmp: "ICMP",
  tcp: "TCP",
  udp: "UDP",
};

function protocolLabel(protocol: string): string {
  const normalized = protocol.trim().toLowerCase();
  return PROTOCOL_LABELS[normalized] ?? protocol.trim().toUpperCase();
}

function formatFlowId(value: string): string {
  const current = value.match(/^([^:\s]+):(\d+)-([^:\s]+):(\d+)-([A-Za-z0-9]+)$/);
  const legacy = value.match(/^([^:\s]+):(\d+)->([^:\s]+):(\d+):([A-Za-z0-9]+)$/);
  const match = current ?? legacy;
  if (!match) {
    return value;
  }
  const [, srcIp, srcPort, dstIp, dstPort, protocol] = match;
  return `${srcIp}:${srcPort} \u2192 ${dstIp}:${dstPort} (${protocolLabel(protocol)})`;
}

function formatFactValue(key: string, value: string, locale: Locale, t: Translate): string {
  if (key === "decision" && locale === "zh-CN") {
    const valueKey = DECISION_VALUE_KEYS[value.toLowerCase()];
    if (valueKey) {
      return `${value} (${t(valueKey)})`;
    }
  }
  if (key === "flow_id") {
    return formatFlowId(value);
  }
  return value;
}

export function formatEvidenceFact(fact: string, locale: Locale, t: Translate): string {
  const separator = fact.indexOf("=");
  if (separator < 0) {
    return fact;
  }

  const key = fact.slice(0, separator).trim();
  if (!key) {
    return fact;
  }

  const labelKey = FACT_LABEL_KEYS[key];
  if (!labelKey) {
    return fact;
  }

  const value = fact.slice(separator + 1).trim();
  return `${t(labelKey)}: ${formatFactValue(key, value, locale, t)}`;
}

export function formatReviewFreeText(text: string, t: Translate): string {
  const key = FIXED_REVIEW_TEXT_KEYS[text];
  return key ? t(key) : text;
}
