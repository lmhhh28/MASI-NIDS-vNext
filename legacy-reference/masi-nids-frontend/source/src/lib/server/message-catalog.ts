import "server-only";

import type { Locale, MessageCatalog } from "@/lib/messages";

export async function loadMessageCatalog(locale: Locale): Promise<MessageCatalog> {
  if (locale === "en-US") {
    return (await import("@/lib/messages.en")).en;
  }
  return (await import("@/lib/messages.zh")).zh;
}
