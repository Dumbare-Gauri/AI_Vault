import type { ReactNode } from "react";

import { Sidebar } from "@/components/app-shell/sidebar";
import { Topbar } from "@/components/app-shell/topbar";
import { MobileNav } from "@/components/app-shell/mobile-nav";
import { CommandPalette } from "@/components/app-shell/command-palette";
import { CopilotPanel } from "@/components/copilot/copilot-panel";

/** The authenticated application shell (redesign brief §6) — every
 * authenticated route wraps its content with this rather than the routing
 * tree itself owning a layout, so each route file's existing
 * `beforeLoad`/data-loading logic is untouched. The Ask Vault copilot lives
 * here too, so it is on every page, in the current storage workspace. */
export function AppShell({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar />
      <MobileNav />
      <CommandPalette />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar title={title} />
        <main className="flex-1 px-4 py-6 md:px-8 md:py-8">{children}</main>
      </div>
      <CopilotPanel />
    </div>
  );
}
