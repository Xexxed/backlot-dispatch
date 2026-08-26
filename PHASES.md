# Build Phases — Backlot Dispatch

Mirrors §4 of the master plan (`../1787534213721-agentic-cinema-hackathon-ideas.md`).

- [x] **Phase 1 — Scaffold**: repo init, FastAPI skeleton, pytest scaffold, README, MIT LICENSE, `.env.example`
- [x] **Phase 2 — Data model + importers + seed data**: validated CSV importers, committed 25-scene / 40-crew / 8-cast demo production across 3 locations (incl. daylight-dependent exteriors)
- [x] **Phase 3 — Deterministic re-optimizer**: stable-partition blocked repair, dependency repair, daylight swap, meal placement; `rule_id` change log; wait-for-clearance timeline builder
- [x] **Phase 3b — Invariant tests**: feasible ⇔ zero validator violations (24 randomized scenarios), per-rule tests, determinism, loud infeasibility — 52 tests green
- [x] **Phase 4 — Gemini intake agent**: Vertex AI `google-genai`, response-schema enforcement, confidence gate, graceful fallback form, `/debug/gcp` evidence page
- [x] **Phase 5 — Crew portal**: tokenized personal schedules (HMAC), department views, changelog, ack endpoint, AD dashboard w/ responder status
- [x] **Phase 6 — Publish flow**: diff view, human approval gate (+acknowledged override), regenerated personal links + QR codes (segno SVG)
- [ ] **Phase 7 — Voice-note input** (polish; cut if time-boxed): upload audio → Gemini transcription into intake schema
- [ ] **Phase 8 — Stretch** (only after 1–6 solid): Twilio SMS; programmatic microsite spin-up; ops metrics page
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
