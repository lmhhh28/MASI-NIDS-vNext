"use client";

import { useNotices } from "@/hooks/use-notices";
import { useI18n } from "@/lib/i18n";
import { NoticeBanner } from "./notice-banner";

export function NoticeBanners() {
  const { locale, t } = useI18n();
  const { data, isLoading } = useNotices(locale);

  if (isLoading || !data?.items.length) return null;

  return (
    <div className="space-y-2 px-4 md:px-6 pt-2">
      {data.items.map((notice) => (
        <NoticeBanner
          key={notice.id}
          notice={notice}
          title={t(
            notice.category === "server_issues"
              ? "notices.serverIssuesTitle"
              : "notices.operationGuideTitle",
          )}
          confirmLabel={t("notices.confirm")}
          autoOpen={notice.category === "server_issues"}
        />
      ))}
    </div>
  );
}
