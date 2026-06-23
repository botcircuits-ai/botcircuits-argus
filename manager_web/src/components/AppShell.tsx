"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { GITHUB_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { cx } from "@/lib/format";
import { Logo } from "./Logo";
import {
  GithubIcon,
  MoonIcon,
  SidebarIcon,
  SignOutIcon,
  SunIcon,
  TraceIcon,
  WorkflowIcon,
} from "./icons";

type NavItem = {
  label: string;
  href: string;
  icon: React.ReactNode;
  soon?: boolean;
};

const NAV: NavItem[] = [
  { label: "Workflows", href: "/workflows", icon: <WorkflowIcon /> },
  { label: "Tracing", href: "/tracing", icon: <TraceIcon /> },
];

const COLLAPSE_KEY = "bc_manager_nav_collapsed";

/** The authenticated app frame: collapsible left nav + top bar + content. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { signOut } = useAuth();
  const { theme, toggle } = useTheme();
  const [collapsed, setCollapsed] = useState(false);

  // Restore the collapsed preference (persists across navigations/sessions).
  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
    } catch {
      /* ignore */
    }
  }, []);

  function toggleCollapsed() {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }

  return (
    <div
      className={cx(
        "h-screen grid max-md:grid-cols-1",
        collapsed ? "grid-cols-[4rem_1fr]" : "grid-cols-[15rem_1fr]",
      )}
    >
      {/* Sidebar */}
      <aside className="border-r border-border bg-surface flex flex-col max-md:hidden h-screen sticky top-0">
        <div
          className={cx(
            "h-16 flex items-center border-b border-border",
            collapsed ? "justify-center px-0" : "px-5",
          )}
        >
          {collapsed ? <Logo compact /> : <Logo />}
        </div>

        <nav className="flex-1 p-3 space-y-1">
          <div
            className={cx(
              "flex items-center py-2",
              collapsed ? "justify-center px-0" : "px-3",
            )}
          >
            {!collapsed && (
              <p className="flex-1 text-xs font-medium uppercase tracking-wider text-muted">
                Platform
              </p>
            )}
            <button
              onClick={toggleCollapsed}
              title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              className="inline-flex items-center justify-center rounded-lg p-1 text-muted hover:text-fg hover:bg-elevated"
            >
              <SidebarIcon className="w-[18px] h-[18px]" />
            </button>
          </div>
          {NAV.map((item) => {
            const active = pathname.startsWith(item.href);
            const content = (
              <span
                title={collapsed ? item.label : undefined}
                className={cx(
                  "flex items-center rounded-xl text-sm font-medium",
                  collapsed ? "justify-center px-0 py-2.5" : "gap-3 px-3 py-2",
                  active
                    ? "bg-brand/15 text-fg ring-1 ring-brand/30"
                    : "text-muted hover:text-fg hover:bg-elevated",
                  item.soon && "opacity-60 cursor-default",
                )}
              >
                <span className={active ? "text-brand-600 dark:text-brand-400" : ""}>
                  {item.icon}
                </span>
                {!collapsed && item.label}
                {!collapsed && item.soon && (
                  <span className="ml-auto text-[10px] uppercase tracking-wide text-muted border border-border rounded-full px-1.5 py-0.5">
                    Soon
                  </span>
                )}
              </span>
            );
            return item.soon ? (
              <div key={item.href}>{content}</div>
            ) : (
              <Link key={item.href} href={item.href}>
                {content}
              </Link>
            );
          })}
        </nav>

        {!collapsed && (
          <div className="p-3 border-t border-border">
            <div className="px-1 text-xs text-muted">
              BotCircuits Argus · v0.1
            </div>
          </div>
        )}
      </aside>

      {/* Main column */}
      <div className="flex flex-col min-w-0 h-screen overflow-hidden">
        <header className="h-16 shrink-0 border-b border-border bg-surface/80 backdrop-blur flex items-center gap-2 px-5 z-10">
          <div className="md:hidden mr-2">
            <Logo compact />
          </div>
          <div className="flex-1" />
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-2 h-9 px-3 rounded-lg text-sm text-muted hover:text-fg hover:bg-elevated"
            title="View on GitHub"
          >
            <GithubIcon className="w-[18px] h-[18px]" />
            <span className="max-sm:hidden">GitHub</span>
          </a>
          <button
            onClick={toggle}
            className="inline-flex items-center justify-center h-9 w-9 rounded-lg text-muted hover:text-fg hover:bg-elevated"
            title={theme === "dark" ? "Switch to light" : "Switch to dark"}
            aria-label="Toggle theme"
          >
            {theme === "dark" ? <SunIcon className="w-[18px] h-[18px]" /> : <MoonIcon className="w-[18px] h-[18px]" />}
          </button>
          <button
            onClick={signOut}
            className="inline-flex items-center gap-2 h-9 px-3 rounded-lg text-sm text-muted hover:text-danger hover:bg-danger/10"
            title="Sign out"
          >
            <SignOutIcon className="w-[18px] h-[18px]" />
            <span className="max-sm:hidden">Sign out</span>
          </button>
        </header>

        <main className="flex-1 min-w-0 overflow-y-auto py-6 px-10 max-w-[100rem] w-full mx-auto">
          {children}
        </main>
      </div>
    </div>
  );
}
