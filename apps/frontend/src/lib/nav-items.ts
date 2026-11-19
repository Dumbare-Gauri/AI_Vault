import {
  Archive,
  BrainCircuit,
  Building2,
  Cloud,
  FolderOpen,
  HardDrive,
  Layers,
  LayoutDashboard,
  Lightbulb,
  MessageCircle,
  ScanLine,
  Search,
  Trash2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface NavItem {
  label: string;
  to: string;
  icon: LucideIcon;
}

export interface NavSection {
  label: string | null;
  items: NavItem[];
}

/** Central nav config — the sidebar, the mobile drawer, and the command
 * palette's "Go to…" group all read from this one list so a new page never
 * needs to be wired into three places by hand. */
export const NAV_SECTIONS: NavSection[] = [
  {
    label: null,
    items: [
      { label: "Dashboard", to: "/dashboard", icon: LayoutDashboard },
      { label: "Files", to: "/files", icon: FolderOpen },
      { label: "Search", to: "/search", icon: Search },
      { label: "Ask Vault", to: "/chat", icon: MessageCircle },
      { label: "Recommendations", to: "/recommendations", icon: Lightbulb },
    ],
  },
  {
    label: "Storage",
    items: [
      { label: "Connections", to: "/storage-connections", icon: Cloud },
      { label: "Scans", to: "/scans", icon: ScanLine },
      { label: "Storage Intelligence", to: "/storage-intelligence", icon: HardDrive },
      { label: "Archives", to: "/archives", icon: Archive },
      { label: "Trash", to: "/trash", icon: Trash2 },
    ],
  },
  {
    label: "Organization",
    items: [
      { label: "Organization", to: "/organization", icon: Building2 },
    ],
  },
];

export const AI_ACCENT_ICON: LucideIcon = BrainCircuit;
export const LAYERS_ICON: LucideIcon = Layers;
