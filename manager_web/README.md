# BotCircuits Manager — Web

The web UI for the BotCircuits Manager. Today it surfaces **workflow execution
tracing** (per-session trace + memory-flow graph)

## Prerequisites

The manager **backend** must be running — it serves auth and the session
traces this UI reads:

```bash
# from the repo root
export BOTCIRCUITS_MANAGER_ADMIN_USERNAME=admin
export BOTCIRCUITS_MANAGER_ADMIN_PASSWORD=change-me
botcircuits manager start --backend-only   # serves http://127.0.0.1:8700
```

Workflow runs write their traces to `.botcircuits/sessions/*-session.json`; the
backend reads from there, so run a workflow (`botcircuits workflow run …`) to
get sessions to view.

## Run the web

```bash
cd manager_web
cp .env.example .env.local       # point NEXT_PUBLIC_API_BASE at the backend
npm install
npm run dev                      # http://localhost:3700
```

Sign in with the same `BOTCIRCUITS_MANAGER_ADMIN_*` credentials the backend was
started with.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | `http://127.0.0.1:8700` | Manager backend base URL. |
| `NEXT_PUBLIC_GITHUB_URL` | the repo | Target of the top-bar GitHub link. |

## Structure

```
src/
├── app/
│   ├── signin/            sign-in page
│   ├── tracing/           session list + [id] detail (graph + timeline)
│   ├── workflows/         placeholder (future workflow manager/editor)
│   ├── layout.tsx         providers (theme, auth)
│   └── page.tsx           entry redirect
├── components/            AppShell (nav + topbar), TraceGraph, TraceTimeline, …
└── lib/                   api client, auth + theme contexts, formatting
```

## Build

```bash
npm run build && npm start
```
