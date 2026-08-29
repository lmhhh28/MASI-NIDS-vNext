import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-4 p-6 text-center">
      <p className="font-mono text-sm text-muted-foreground">404</p>
      <h1 className="text-2xl font-semibold">页面不存在</h1>
      <p className="text-sm text-muted-foreground">该地址可能已失效，或当前账号无权访问。</p>
      <Link href="/" className={buttonVariants()}>返回仪表盘</Link>
    </main>
  );
}
