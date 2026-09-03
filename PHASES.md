# Build Phases — Backlot Dispatch

Mirrors §4 of the master plan (`../1787534213721-agentic-cinema-hackathon-ideas.md`).

- [x] **Phase 1 — Scaffold**: repo init, FastAPI skeleton, pytest scaffold, README, MIT LICENSE, `.env.example`
- [x] **Phase 2 — Data model + importers + seed data**: validated CSV importers, committed 25-scene / 40-crew / 8-cast demo production across 3 locations (incl. daylight-dependent exteriors)
- [x] **Phase 3 — Deterministic re-optimizer**: stable-partition blocked repair, dependency repair, daylight swap, meal placement; `rule_id` change log; wait-for-clearance timeline builder
- [x] **Phase 3b — Invariant tests**: feasible ⇔ zero validator violations (24 randomized scenarios), per-rule tests, determinism, loud infeasibility — 52 tests green
- [x] **Phase 4 — Gemini intake agent**: Vertex AI `google-genai`, response-schema enforcement, confidence gate, graceful fallback form, `/debug/gcp` evidence page
- [x] **Phase 5 — Crew portal**: tokenized personal schedules (HMAC), department views, changelog, ack endpoint, AD dashboard w/ responder status
- [x] **Phase 6 — Publish flow**: diff view, human approval gate (+acknowledged override), regenerated personal links + QR codes (segno SVG)
- [x] **Phase 7 — Voice-note input**: mic button on both incident forms records a voice note in-page — `getUserMedia` + `AudioContext` @ 16 kHz mono with a hand-rolled 16-bit WAV encoder (`app/static/voice.js`; MediaRecorder rejected — desktop Chrome emits webm/opus, which Gemini does not accept) — then submits as a plain `multipart/form-data` POST to `/incident/voice` (no fetch, so 303s and fallback pages just work; degraded hint when capture is unavailable: insecure context / no mic / permission denied). One Gemini call (`parse_incident_voice` in `app/agents/intake.py`, `_VoiceIncidentOut` = text schema + `transcript`) transcribes and extracts in a single round trip — transcript becomes `incident.free_text` with `source="voice"` so the plan-review page shows exactly what the model heard before publish; `gcp_calls` logs the `intake_voice` kind with audio bytes / mime / transcript *length*, never the transcript text. The route validates the upload (non-empty, ≤10 MB ≈ 5 min of WAV, RIFF/WAVE magic; Content-Length pre-check + capped read bound memory) and reuses the exact post-intake pipeline (blocking → sandbox group, else single plan) offloaded to a threadpool so the Gemini round trip cannot stall the worker; the audio itself is never persisted. Zero new dependencies, zero new env vars. 190 tests green.
- [ ] **Phase 8 — Backlog top-up before feature lock** (re-assessed 2026-08-27; see "Needs to be added" in `../1787534213721-agentic-cinema-hackathon-ideas.md`): Tier 1 (sandbox DONE — Phase 8b, rollback DONE — Phase 8c, token expiry/rotation DONE — Phase 8f) = PDF Call Sheet/Exhibit G/DPR export (ReportLab), Excel import, offline crew PWA, ops metrics page + import validation report + incident replay; Tier 2 (only if time) = NL edit intents (DONE — Phase 8d), crew preferences (planned — Phase 8e), weather-aware suggestions (DONE — Phase 8g); cut = Movie Magic API, generic UI pass, Twilio SMS, GPS pins/prep windows, penalty-cost engine (skipped 2026-08-27)

- [x] **Phase 8b — What-If Scenario Sandbox**: three deterministic recovery strategies (`minimal` / `cover_set` / `hold`) generated per blocking incident as a sandbox group; `/sandbox/{gid}` comparison cards (wrap, lunch, moves, feasibility, change log) + `/plans/{id}/select` to pick one; narration now runs at selection/publish; legacy plan schema migrated. 81 tests green.
- [x] **Phase 8c — Plan versioning + one-click rollback**: publishing a plan supersedes the prior live plan (status `superseded`); "↩ Revert" on the dashboard banner and changelog restores it via `POST /plans/{id}/revert` and regenerates QR artifacts (crew links are token-based and read the live plan dynamically, so rollback is instant). Trust story: agents propose, humans can undo. `/rollback-plan` Kilo command documents the workflow. 84 tests green.
- [x] **Phase 8d — NL schedule customization (edit intents)**: editor agent (`app/agents/editor.py`, Gemini schema-enforced, mirrors intake fallback contract) turns AD free text into `EditIntent` objects (move_scene / swap_location / add_scene); `app/edit_ops.py` is the ONLY mutation point (deep-copies production, loud `EditError` on invalid intents, every edit logged as `R-EDIT-INTENT`) and feeds the seed through the same repair passes + hard-rule gate via new `replan(seed_order/baseline_order_override/extra_changes)` params. `GET/POST /edit` + `edit_form.html` with manual fallback form. The LLM never mutates the schedule directly. 94 tests green.

- [x] **Phase 8f — Security hardening**: AD console + `/debug/*` behind HTTP Basic auth (`AD_USERNAME`/`AD_PASSWORD`, fail-closed 503 when unset); startup refuses the dev-default/placeholder `APP_SECRET` outside an explicit `ALLOW_INSECURE_DEV_SECRET=1` opt-in; Origin/Referer check rejects cross-site form posts (TrustedHost stays outermost); crew links gain expiry (`TOKEN_TTL_HOURS`, persisted in SQLite so restarts don't extend it) and one-shot rotation — `POST /links/rotate` bumps the token epoch, kills every live link/QR, regenerates fresh artifacts. Acks are keyed per subject so rotation doesn't orphan them; epoch 0 keeps the pre-rotation token wire format; startup reconciles missing QR artifacts; per-request store reads keep multi-instance rotations convergent. 113 tests green.

- [x] **Phase 8g — Weather-aware schedule suggestions**: forecast hazards at exterior locations become one-click recovery plans. `app/weather.py` is pure and FastAPI-free — `Hour`/`HazardWindow`, threshold policy (precip ≥ `WEATHER_PRECIP_PCT` 60%, thunderstorm, wind ≥ `WEATHER_WIND_KMH` 40 km/h) with contiguity merging, and EXT-scene intersection over the meal-inclusive baseline day or the live published plan (completed scenes excluded; window past end-of-day clamps to the last scene end). Committed fixtures (`seed/weather/*.json`; demo scenario: rain at Harper Ranch 14:00–16:30) drive the offline demo; `POST /weather/refresh` is the only network path, pulling the Google Weather API (Maps Platform key `GOOGLE_MAPS_API_KEY`) into `instance/weather_cache.json`, honored for `WEATHER_CACHE_MAX_AGE_MIN` (60) before fixtures win again. The dashboard "Weather watch" card lists per-location windows + threatened scene ids with per-advisory "Generate weather pivot plan" — `POST /weather/adopt` materializes a blocking `WEATHER` incident (`source=weather_advisor`) through the shared sandbox-group helper, so the standard strategy sandbox → review → publish pipeline executes it unchanged. Core change: `Incident.blocked_from` (manual form gains an optional "Blocked from" field; intake's Gemini schema untouched, default stays "now") pins the window start, and the timeline builder waits only when a scene's actual span (travel included) would overlap the window — an afternoon storm no longer voids the morning. Fetch failures/malformed fixtures degrade to fixtures; dashboards never block on network; `NOW_OVERRIDE` pins the demo clock on UTC servers. 170 tests green.

## Phase 8e — Crew Preference Ingestion (implementation plan)

**Goal:** the AD enters crew preferences as natural language ("C-3's babysitter drops her at 10:00", "GE-09 must eat by 13:00", "keep A's and B's calls an hour apart"); a preferences agent converts them into structured `Preference` objects; a deterministic scoring layer ranks plans by constraints honored. Ranked strictly below hard union rules: a preference can NEVER flip `is_feasible` — binding violations surface as WARN diagnostics only. AI suggests; engine decides.

**Design decisions**

- **Taxonomy filter: only time-shaped preferences schedule.** A preference enters the engine only if it constrains *when* a person works, *how long* their scenes take, or *when they eat*. Content prefs (vegetarian, allergies) are catering data → stored on the person record as logistics notes, never scored.
- **v1 kinds (3 schedulable + 1 social):**
  - `start_window` — person not available before N minutes from call ("babysitter drops at 10:00")
  - `end_window` — person off by N minutes from call ("grip leaves at 18:00")
  - `meal_deadline` — person must eat by HH:MM ("diabetic, eat by 13:00"); v1 scored company-wide against the plan's single lunch (min deadline wins); documented simplification
  - `pair_separation` — keep two people's call times ≥ N minutes apart. **Sensitivity rules:** same-scene pairs are unschedulable (HR/casting matter — agent rejects); the stored record is AD-private, reason-less (agent discards the narrative — it schedules time, not people's history), and the UI carries a standing HR nudge line ("if this involves safety or conduct, contact production HR")
  - Deferred: accessibility buffers (+5 min interpreter setup etc.) — touches all duration math, first cut under deadline; v1.5
- **Ranking layer, not reordering:** preferences NEVER reorder scenes. They score the options the engine already generates — the What-If Sandbox cards gain "crew constraints honored: 4/5"; plan_diff and dashboard show WARN rows (`R-PREF`) for binding violations. This keeps determinism and makes "AI suggests, engine decides" visible on screen.
- **Live scoring, stored payloads stable:** preferences are computed against a plan at render time (dashboard/diff/sandbox), not baked into stored proposals — so adding/deleting a preference immediately re-scores existing plans with no migration.

**Steps (ordered, one commit each)**

1. `app/models.py`: `PREFERENCE_KINDS` + `Preference(BaseModel)` (kind, person_id, person_b_id for pairs, minutes-from-call offset, clock HH:MM alt form, binding flag, AD-facing note, source/confidence — mirrors EditIntent).
2. `app/store.py`: `preferences` table (id, kind, person_id, person_b_id, minutes, clock, binding, note, created_at) + `save_preference` / `list_preferences` / `delete_preference`. Runtime state, not seed CSV.
3. `app/schedule_view.py`: add `_last_scene_end(production, kind, subject_id, rows)` (mirror of `_first_scene_start`) — end_window needs the person's last commitment, not their call.
4. `app/preferences_ops.py` (new, pure, zero LLM): `score_plan(plan_payload, preferences, production, rbc) -> PrefScore(honored, total, violations)` where violations carry AD-facing messages + binding flag. Uses `schedule_view.compute_calls` for call times and the proposed timeline for last-end. Lunch start read from the MEAL slot. pair_separation via `|call_a − call_b| ≥ gap`.
5. `app/agents/preferences.py`: `parse_preferences(text, settings, production, store)` mirroring editor.py — schema-enforced `_PreferencesOut`, taxonomy rules in the system prompt (exact person ids; REJECT non-time-shaped asks like dietary content — not an intent; pair_separation stored reason-less), confidence gate → `FallbackRequired`, logged to `/debug/gcp` as kind `preferences`. Plus `manual_preference(...)` form builder.
6. Routes: `GET/POST /preferences` (NL entry + manual fallback + active list with delete), `POST /preferences/{id}/delete`; wire `score_plan` into dashboard, plan_diff, and sandbox contexts (score chip + WARN rows).
7. Templates: `preferences.html`; "crew constraints honored x/y" chip on sandbox cards + plan_diff; WARN rows with `R-PREF`; dashboard "N crew constraints active" banner linking to /preferences. Pair separation rendered neutrally ("call-time separation request") + HR nudge line, AD-only.
8. `tests/test_preferences.py`: start/end window honored+violated (engineered timelines); meal_deadline scored vs lunch slot; pair_separation via compute_calls deltas; **invariant: preferences never change `is_feasible`**; determinism; manual≡Gemini contract; e2e — add via manual form → sandbox chip shows x/y → delete; privacy — pair note never rendered on crew card.
9. Run full pytest; update PHASES.md + ideas file (item 9 → DONE).

**Pitfalls**

- `compute_calls` returns baseline when no plan published — scoring must always read the *proposed* timeline rows, never baseline.
- end_window without a last-end helper silently degrades — `_last_scene_end` is a prerequisite (step 3 before 4).
- meal_deadline is per-person in reality but company-wide in v1 (one lunch) — document in UI ("tightest deadline wins").
- Never render pair_separation records or notes on crew-facing pages — test pins it.
- Preferences must not leak into `is_feasible`, the publish gate, or diagnostics severity — WARN only, enforced by test.

**Demo payoff:** sandbox cards read "crew constraints honored 5/5"; a binding violation shows "⚠ C-3's 10:00 start missed by 25 min" without blocking publish; the pitch line "the system schedules time, not people's history" lands the ethical boundary.

- [x] **Phase 9 prep — Deploy artifacts**: `.env.example`, PORT-aware entrypoint, deployment checklist (below) — actual Replit Agent session + deploy performed by the user on Replit
- [x] **Phase 10 prep — Submission package**: README demo script, before/after framing, gate audit checklist (below) — video still to be recorded

## Replit deployment checklist (gate audit)

1. Import repo to Replit; run scaffold session with **Replit Agent** (capture recording/screenshots).
2. Add Secrets: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_APPLICATION_CREDENTIALS` (or `GOOGLE_API_KEY`), and a freshly generated `APP_SECRET`. Set `PUBLIC_BASE_URL` to the canonical HTTPS origin if QR links should be pinned, and set `TRUSTED_HOSTS` to comma-separated bare hostnames (never `https://...`).
3. The Replit build installs dependencies; the run command starts `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Deploy → verify `*.replit.app` URL survives sleep; re-run demo incident end-to-end.
5. Audit: LICENSE visible · no secrets in git or reachable history · `/debug/gcp` shows real Vertex calls · video ≤3 min showing live runs.

### Credential exposure recovery (required before deployment)

- Treat any Google service-account key and `APP_SECRET` that appeared in prior
  repository revisions as compromised. The credential owner must revoke/delete
  that Google key, generate replacements where needed, and save only the new
  values in Replit Secrets.
- The normal GitHub branch history has been rewritten. Any append-only backup
  that retains the old revisions must be deleted or recreated by its
  administrator because it cannot accept a force-update.
- Do not add credentials, host settings, or replacement values to `.replit` or
  any tracked file.
