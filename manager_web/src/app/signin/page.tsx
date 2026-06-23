"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { GITHUB_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { Logo } from "@/components/Logo";
import { GithubIcon, MoonIcon, SunIcon } from "@/components/icons";

export default function SignInPage() {
  const { token, ready, signIn } = useAuth();
  const { theme, toggle } = useTheme();
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Already signed in → go to the app.
  useEffect(() => {
    if (ready && token) router.replace("/tracing");
  }, [ready, token, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await signIn(username, password);
      router.replace("/tracing");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen relative grid place-items-center px-4">
      {/* top-right controls */}
      <div className="absolute top-5 right-5 flex items-center gap-1">
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center justify-center h-9 w-9 rounded-lg text-muted hover:text-fg hover:bg-elevated"
          title="GitHub"
        >
          <GithubIcon className="w-[18px] h-[18px]" />
        </a>
        <button
          onClick={toggle}
          className="inline-flex items-center justify-center h-9 w-9 rounded-lg text-muted hover:text-fg hover:bg-elevated"
          aria-label="Toggle theme"
        >
          {theme === "dark" ? <SunIcon className="w-[18px] h-[18px]" /> : <MoonIcon className="w-[18px] h-[18px]" />}
        </button>
      </div>

      {/* subtle brand glow */}
      <div className="pointer-events-none absolute inset-x-0 top-0 h-64 bg-gradient-to-b from-brand/10 to-transparent" />

      <div className="relative w-full max-w-sm">
        <div className="flex justify-center mb-6">
          <Logo />
        </div>
        <div className="rounded-2xl border border-border bg-surface shadow-sm p-6">
          <h1 className="text-lg font-semibold text-fg">Sign in</h1>
          <p className="text-sm text-muted mt-1">
            Use your manager admin credentials.
          </p>

          <form onSubmit={onSubmit} className="mt-5 space-y-4">
            <Field
              label="Username"
              value={username}
              onChange={setUsername}
              autoComplete="username"
              autoFocus
            />
            <Field
              label="Password"
              type="password"
              value={password}
              onChange={setPassword}
              autoComplete="current-password"
            />

            {error && (
              <div className="text-sm rounded-lg border border-danger/30 bg-danger/10 text-danger px-3 py-2">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={busy || !username || !password}
              className="w-full h-10 rounded-lg bg-brand text-zinc-900 font-semibold hover:bg-brand-300 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
        <p className="text-center text-xs text-muted mt-4">
          Credentials come from{" "}
          <code className="font-mono">BOTCIRCUITS_MANAGER_ADMIN_*</code>.
        </p>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  autoComplete,
  autoFocus,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  autoComplete?: string;
  autoFocus?: boolean;
}) {
  return (
    <label className="block">
      <span className="text-sm font-medium text-fg">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        className="mt-1 w-full h-10 rounded-lg border border-border bg-bg px-3 text-sm text-fg outline-none focus:ring-2 focus:ring-brand/40 focus:border-brand/50"
      />
    </label>
  );
}
