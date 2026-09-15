# J.A.R.V.I.S — System Architecture

Generated from the actual import graph of this deployment (the cloud/multi-tenant
fork at `האפליקציה הסופית` — `JARVIS_DESKTOP_TOOLS=false` in `render.yaml`, so the
desktop-control tools shown below are wired into the code but not exposed to the
LLM in production). Dashed edges are conditional or not yet wired in.

```mermaid
graph TD
    %% ===== Browser =====
    subgraph Browser["Browser HUD"]
        HUD["templates/index.html<br/>vanilla JS — fetch() + EventSource<br/>no framework, no build step"]
    end

    %% ===== Flask app =====
    subgraph Flask["app.py — Flask"]
        Routes["Routes: /, /auth/*, /api/*"]
        LLMLoop["run_llm()<br/>Groq tool-calling loop"]
        SSERoute["/api/stream"]
    end

    RT["event_stream.py<br/>in-memory per-user SSE pub/sub"]

    subgraph Guard["guardrails.py — safety layer"]
        GuardFns["resolve_safe_path() · refuse_if_elevated()<br/>request_confirmation() / resolve_confirmation()<br/>pending-token registry (confirm-to-act)"]
    end

    subgraph BG["daily_briefing.py — background thread"]
        Sched["due reminders + weekly summary<br/>checked every 60s, delivered by email"]
    end

    Productivity["productivity_service.py<br/>merges providers, confirm-gates every write"]

    subgraph ProviderClients["Provider clients"]
        GoogleSvc["google_service.py<br/>Calendar + Gmail"]
        MSSvc["microsoft_service.py<br/>Outlook/365 (built, not wired to login)"]
    end

    subgraph ToolModules["Tool modules"]
        FileTools["file_tools.py"]
        Vision["vision_action.py"]
        SelfHeal["self_healing.py"]
        Tavily["tavily_service.py"]
        Stocks["stocks_service.py"]
        TTS["tts.py"]
        Cursor["cursor_indicator.py"]
    end

    Users["users.py"]
    DB[("users.db — SQLite<br/>ephemeral on Render free tier")]
    Cert["cert_bootstrap.py<br/>TLS cert patch — must import first"]

    %% ===== External services =====
    subgraph External["External services"]
        Groq[("Groq API<br/>LLM + tool-calling")]
        GCal[("Google Calendar / Gmail API")]
        MSGraph[("Microsoft Graph API")]
        TavilyAPI[("Tavily Search API")]
        Yahoo[("Yahoo Finance (unofficial)")]
        EdgeTTS[("Microsoft Edge TTS")]
        WinOS[("Windows OS<br/>processes / mouse / windows")]
    end

    %% ===== Browser <-> Flask =====
    Routes -- "renders on GET /" --> HUD
    HUD -- "fetch() + EventSource" --> Routes
    Routes --> SSERoute
    SSERoute --> RT

    %% ===== Flask internals =====
    Routes --> LLMLoop
    Routes --> TTS
    Routes --> GuardFns
    Routes --> RT
    LLMLoop -- "tool calls" --> Groq
    LLMLoop --> Productivity
    LLMLoop --> Tavily
    LLMLoop --> Stocks
    LLMLoop -.->|"JARVIS_DESKTOP_TOOLS=true only"| FileTools
    LLMLoop -.->|"JARVIS_DESKTOP_TOOLS=true only"| Vision
    LLMLoop -.->|"JARVIS_DESKTOP_TOOLS=true only"| SelfHeal

    %% ===== Service layer =====
    Productivity --> GoogleSvc
    Productivity -.->|"not reachable via sign-in yet"| MSSvc
    Productivity --> Users
    Productivity --> GuardFns

    %% ===== Provider clients =====
    GoogleSvc --> Cert
    GoogleSvc --> Users
    GoogleSvc --> GCal
    MSSvc --> Cert
    MSSvc --> MSGraph

    %% ===== Tool modules =====
    FileTools --> GuardFns
    Vision --> GuardFns
    Vision --> Cursor
    Vision -- "own Groq calls" --> Groq
    SelfHeal --> GuardFns
    Cursor -.-> WinOS
    FileTools -.->|"kill_process"| WinOS
    Tavily --> Cert
    Tavily --> TavilyAPI
    Stocks --> Cert
    Stocks --> Yahoo
    TTS --> EdgeTTS

    %% ===== Data =====
    Users --> DB

    %% ===== Background thread =====
    Sched --> GoogleSvc
    Sched --> Productivity
    Sched --> Users
```

## Notes on what the diagram is actually saying

- **`templates/index.html` never talks to any Python module directly** — every
  interaction goes through `app.py`'s HTTP routes or its one SSE endpoint. There's
  no server-side templating (`render_template("index.html")` is called with zero
  context vars); all state lives in the browser's JS.
- **`guardrails.py` has no internal imports of its own** — it's the dependency
  sink every other module (except `event_stream.py` and `users.py`) reaches into,
  never the other way around. That's deliberate (see its own docstring): one
  audited module is easier to trust than scattered checks.
- **`event_stream.py` is only reachable from `app.py`** — `productivity_service.py`
  returns plain dicts (`status`/`token`/`kind`/`details`); `app.py` is what decides
  to push those over SSE. `daily_briefing.py` deliberately does **not** use SSE at
  all (a background thread can't assume a browser tab is open) — it emails
  directly through `google_service.send_email` instead.
- **`microsoft_service.py` is a dead branch in this build** — fully implemented
  and imported by `productivity_service.py`, but this fork's sign-in flow is
  Google-only, so it's never actually reached today.
- **Desktop-control tools are present but gated** — `file_tools.py` (beyond its
  sandboxed workspace functions), `vision_action.py`, and `self_healing.py` are
  unconditionally imported, but `app.py`'s `ACTIVE_TOOLS` only exposes them to the
  LLM when `JARVIS_DESKTOP_TOOLS=true`. `render.yaml` sets it `false` for this
  cloud deployment, since there's no Windows desktop on the other end of a Render
  dyno to control.
- **`cert_bootstrap.py` isn't part of the data flow** — it's a side-effect-only
  import (patches `certifi`/`SSL_CERT_FILE` for the process) that every module
  making its own HTTPS calls (`google_service`, `microsoft_service`,
  `tavily_service`, `stocks_service`) imports first, per this repo's `CLAUDE.md`.
