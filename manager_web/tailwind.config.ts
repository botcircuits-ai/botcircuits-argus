import type { Config } from "tailwindcss";

/**
 * BotCircuits Manager theme.
 *
 * Brand: lime/chartreuse green accent on a clean light surface, with a
 * near-black dark mode (matching the marketing refs). Colors are exposed as
 * CSS variables (see globals.css) so the same class names work in both themes;
 * dark mode is toggled by a `dark` class on <html>.
 */
const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Brand lime — the BotCircuits accent.
        brand: {
          DEFAULT: "#C4F542",
          50: "#f7fde6",
          100: "#edfbc4",
          200: "#def88c",
          300: "#cdf357",
          400: "#c4f542",
          500: "#a6dd1f",
          600: "#82b015",
          700: "#638515",
          800: "#506916",
          900: "#445917",
        },
        // Theme-aware surfaces/text via CSS vars (set per theme in globals.css).
        bg: "rgb(var(--bg) / <alpha-value>)",
        surface: "rgb(var(--surface) / <alpha-value>)",
        elevated: "rgb(var(--elevated) / <alpha-value>)",
        border: "rgb(var(--border) / <alpha-value>)",
        fg: "rgb(var(--fg) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        // Status colors for trace events.
        ok: "#22c55e",
        warn: "#f59e0b",
        danger: "#ef4444",
        info: "#3b82f6",
      },
      borderRadius: {
        xl: "0.875rem",
        "2xl": "1.125rem",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
