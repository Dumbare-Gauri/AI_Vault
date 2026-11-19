import { Link, useCanGoBack, useRouter } from "@tanstack/react-router";
import { ChevronLeft } from "lucide-react";
import type { ReactNode } from "react";

/** "Back" that behaves like the browser's Back button: it returns to the
 * screen the user came from — with its folder, filters and page intact —
 * and only falls back to `to` when there is nothing in the app to go back
 * to (a link opened directly). */
export function BackLink({ to, children }: { to: string; children: ReactNode }) {
  const router = useRouter();
  const canGoBack = useCanGoBack();
  return (
    <Link
      to={to}
      onClick={(event) => {
        if (!canGoBack) return;
        event.preventDefault();
        router.history.back();
      }}
      className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
    >
      <ChevronLeft className="size-4" /> {children}
    </Link>
  );
}
