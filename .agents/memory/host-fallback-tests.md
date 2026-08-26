---
name: Host fallback tests on Replit
description: How deployment-origin environment configuration affects host-security fallback tests.
---

Host-security tests that specifically verify request-host fallback must run
without deployment-origin environment variables in the pytest process.

**Why:** Replit Secrets are inherited by test-created settings. A configured
canonical public origin correctly wins over request-host fallback, which makes
a fallback-only assertion fail even though the production behavior is valid.

**How to apply:** When a test explicitly expects the validated request base to
be used because no canonical origin is configured, remove `PUBLIC_BASE_URL` and
`TRUSTED_HOSTS` from that test process. Do not alter or delete the workspace
Secrets.