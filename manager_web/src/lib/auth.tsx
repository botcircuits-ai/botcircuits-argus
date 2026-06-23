"use client";

/**
 * Auth context: holds the bearer token (persisted in localStorage), exposes
 * sign-in / sign-out, and gates the app. The token is the only auth state —
 * the backend is stateless, so there is no profile to fetch beyond the
 * username embedded in the token.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "./api";

const TOKEN_KEY = "bc_manager_token";

type AuthState = {
  token: string | null;
  ready: boolean; // hydrated from storage yet?
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    try {
      setToken(localStorage.getItem(TOKEN_KEY));
    } catch {
      /* storage blocked */
    }
    setReady(true);
  }, []);

  const signIn = useCallback(async (username: string, password: string) => {
    const { token } = await api.login(username, password);
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch {
      /* storage blocked — session-only token still works */
    }
    setToken(token);
  }, []);

  const signOut = useCallback(() => {
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* ignore */
    }
    setToken(null);
  }, []);

  const value = useMemo(
    () => ({ token, ready, signIn, signOut }),
    [token, ready, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
