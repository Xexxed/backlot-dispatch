# Backlot Dispatch 🎬

Real-time set-disruption recovery for film productions. A 1st AD reports a
disruption in plain language (or voice); agents re-optimize the shooting day
under union constraints; a live mobile crew portal deploys new call times
instantly — with acknowledgment tracking.

**Track:** Replit · Agentic Cinema Hackathon · built with Replit Agent, Gemini
via Vertex AI, and Google Cloud.

## Hard contest gates (how this repo satisfies them)

| Gate | Where |
|---|---|
| Powered by Gemini + Agent Builder | `app/agents/intake.py` (schema-enforced Vertex AI call), `app/agents/narrator.py` |
| Runtime calls BOTH GCP and partner product | Vertex AI at request time (`/debug/gcp` evidence log) + Replit hosting/deploy |
| Only GCP AI + partner AI | No other vendors anywhere in `requirements.txt` |
| Built using Replit Agent | Perform the scaffold session on Replit; capture the session for submission |
| Hosted on `replit.app`/`replit.dev` | Deploy from the Replit workspace |

## Quickstart (local)

```powershell
py -3.12 -m venv .venv          # or: uv venv .venv --python 3.12
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env           # fill credentials (never commit .env)
.venv\Scripts\python.exe scripts\seed_demo.py
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 — the AD dashboard. Without credentials the intake
agent gracefully falls back to the manual form; every other feature works.

## Architecture

```
[voice/text incident] → Intake Agent (Gemini via Vertex AI, google-genai)
                      → structured JSON (response-schema enforced;
                        fallback manual form on low confidence/failure)
[revised schedule]    ← Deterministic Re-optimizer (pure Python, no LLM)
                      ← rulebook config (meals/daylight/travel/deps/blocks)
[AD review+Publish]   → human approval gate (diff view)
[Crew Portal]         → FastAPI + server-rendered mobile-first pages;
                        tokenized personal views + QR codes; acks → dashboard
```

- **Re-optimizer**: minimal-change heuristic; every change carries a
  machine-checkable `rule_id` (`R-BLOCKED-LOCATION`, `R-DAYLIGHT`,
  `R-MEAL-WINDOW`, `R-DEPS`, `R-MEAL-BREAK`, `R-TRAVEL`). Infeasible days
  produce explicit diagnostics — never a silent bad schedule.
- **Narrator**: describes the optimizer's change list for crew comms; it can
  never alter schedule facts. Falls back to a deterministic template.
- **Storage**: production entities live in CSV (`seed/`); runtime state
  (plans, acks, GCP call evidence) in SQLite.

## Demo script (60-second run)

1. Dashboard shows the baseline day board for *Midnight Harvest* (25 scenes).
2. Type: “Generator down at Stage 4, exterior unit blocked until 14:00”.
3. Intake agent returns structured incident JSON (see `/debug/gcp`).
4. Diff view: scenes pushed behind unblocked work, lunch re-timed, dependency
   order preserved — each row citing its rule.
5. **Publish** → per-person links + QR codes regenerate instantly.
6. Open a crew link on a phone: new call time, what-changed-and-why, ack tap
   lands back on the AD dashboard.

## Security model

**Rollout order for deploying this revision:** set `APP_SECRET` **and**
`AD_PASSWORD` in Replit Secrets *before* promoting — the app refuses to boot
on the dev-default `APP_SECRET`, and without `AD_PASSWORD` the AD console is
unreachable (503). Crew links issued before the expiry/rotation feature
deployed keep working until the first explicit rotation.

- **AD console + debug pages** (`/`, `/incident`, `/edit`, `/plans/*`,
  `/sandbox/*`, `/links/rotate`, `/debug/*`) require HTTP Basic auth —
  `AD_USERNAME` / `AD_PASSWORD` env vars. No password configured = routes
  answer 503 (fail closed, never open-by-default).
- **APP_SECRET is mandatory**: startup refuses the dev default or the
  `.env.example` placeholder (`ALLOW_INSECURE_DEV_SECRET=1` opts out for
  local hacking only). Crew links are HMACs of this secret, so a default
  secret = forgeable links.
- **CSRF**: mutating requests with an `Origin`/`Referer` header must match
  the (TrustedHost-validated) request host or a trusted host — cross-site
  form posts get 403.
- **Crew links**: HMAC token + rotation epoch with a configurable TTL
  (`TOKEN_TTL_HOURS`, default 48, 0 = never). "Rotate crew links" on the
  dashboard instantly kills every live link and QR code and issues fresh
  ones. The epoch/issue time live in the SQLite DB, so restarts do not
  extend expiry — but on Replit's ephemeral filesystem a *redeploy* starts a
  fresh DB (epoch 0), so re-share links after redeploying if you have
  rotated. Acks are recorded per person and survive rotation.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests -q     # 107 tests: invariants, E2E, contracts
```

Key invariant: a proposal is feasible **iff** the independent validator finds
zero hard-rule violations — checked across randomized scenarios.

## Repo layout

```
app/            FastAPI app (models, engine, agents, web, templates, static)
seed/           committed demo production CSVs (regenerate via scripts/seed_demo.py)
scripts/        seed generator + scenario debugger
tests/          pytest suite (optimizer invariants, importers, intake, E2E smoke)
```

MIT licensed. Secrets only via environment/Replit Secrets — see `.env.example`.
