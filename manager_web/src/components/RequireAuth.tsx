"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";
import { AppShell } from "./AppShell";

/** Gate protected pages: until the token is hydrated we show nothing; with no
 *  token we bounce to /signin; otherwise render inside the app shell. */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const { token, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (ready && !token) router.replace("/signin");
  }, [ready, token, router]);

  if (!ready || !token) {
    return (
      <div className="min-h-screen grid place-items-center text-muted text-sm">
        Loading…
      </div>
    );
  }
  return <AppShell>{children}</AppShell>;
}
