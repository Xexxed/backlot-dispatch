---
name: Credential-history incident recovery
description: How to complete a credential-exposure cleanup when repository history and backups differ in rewrite capability.
---

When credentials have appeared in repository history, remove all reachable
published history, revoke and replace the exposed credentials in their owners'
systems, and keep replacement values only in Replit Secrets.

**Why:** Rewriting the GitHub branch removes the exposure from normal published
history, but an append-only backup service rejects force-updates and can keep
the old blobs reachable.

**How to apply:** Use a clean root commit for GitHub when a normal force-push
cannot be authenticated. Ask the backup administrator to delete or recreate an
append-only backup, then confirm all public and backup refs are clean before
deployment.