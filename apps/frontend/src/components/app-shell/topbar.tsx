import { Link, useNavigate } from "@tanstack/react-router";
import { LogOut, Menu, Search, Settings, Sparkles, User } from "lucide-react";

import { WorkspaceSwitcher } from "@/components/storage-intelligence/storage-scope-switcher";
import { useAuthStore } from "@/stores/auth-store";
import { useCopilotStore } from "@/stores/copilot-store";
import { useLayoutStore } from "@/stores/layout-store";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  const letters = parts.length > 1 ? [parts[0]?.[0], parts.at(-1)?.[0]] : [parts[0]?.[0]];
  return letters.filter(Boolean).join("").toUpperCase() || "?";
}

export function Topbar({ title }: { title?: string }) {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const setMobileNavOpen = useLayoutStore((s) => s.setMobileNavOpen);
  const setCommandPaletteOpen = useLayoutStore((s) => s.setCommandPaletteOpen);
  const setCopilotOpen = useCopilotStore((s) => s.setOpen);
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    void navigate({ to: "/login" });
  }

  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-border bg-background/85 px-4 backdrop-blur-sm md:px-6">
      <button
        type="button"
        onClick={() => setMobileNavOpen(true)}
        className="rounded-md p-1.5 text-foreground hover:bg-secondary md:hidden"
        aria-label="Open navigation"
      >
        <Menu className="size-5" />
      </button>

      {title ? (
        <h1 className="hidden truncate text-sm font-semibold md:block">{title}</h1>
      ) : null}

      <button
        type="button"
        onClick={() => setCommandPaletteOpen(true)}
        className="ml-auto flex h-9 w-full max-w-sm items-center gap-2 rounded-lg border border-input bg-card px-3 text-sm text-muted-foreground shadow-clay-sm transition-colors hover:border-ring/40 md:ml-0 md:mr-auto md:max-w-md"
      >
        <Search className="size-4 shrink-0" aria-hidden="true" />
        <span className="truncate">Search files, ask Vault…</span>
        <kbd className="ml-auto hidden shrink-0 items-center gap-0.5 rounded border border-border bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground sm:flex">
          ⌘K
        </kbd>
      </button>

      <WorkspaceSwitcher />

      <Button variant="ai" size="sm" className="hidden sm:inline-flex" onClick={() => setCopilotOpen(true)}>
        <Sparkles className="size-4" />
        Ask Vault
      </Button>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            aria-label="Account menu"
          >
            <Avatar>
              <AvatarFallback>{user ? initials(user.name) : <User className="size-4" />}</AvatarFallback>
            </Avatar>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuLabel className="flex flex-col gap-0.5">
            <span className="truncate text-sm font-medium text-foreground">{user?.name}</span>
            <span className="truncate text-xs font-normal text-muted-foreground">{user?.email}</span>
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <Link to="/profile">
              <User className="size-4" /> Profile
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/organization">
              <Settings className="size-4" /> Organization
            </Link>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => void handleLogout()}>
            <LogOut className="size-4" /> Log out
            <DropdownMenuShortcut>⇧⌘Q</DropdownMenuShortcut>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </header>
  );
}
