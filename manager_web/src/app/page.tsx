"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";

/** Entry: route to the default section (Tracing) or sign-in. */
export default function Home() {
  const { token, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!ready) return;
    router.replace(token ? "/tracing" : "/signin");
  }, [ready, token, router]);

  return (
    <div className="min-h-screen grid place-items-center text-muted text-sm">
      Loading…
    </div>
  );
}
