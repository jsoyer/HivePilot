Run a security-focused review of this repository.

Check in priority order:
- secrets or tokens committed in the repository
- authentication and authorization issues
- injection risks
- insecure configuration defaults
- dependency or supply chain concerns
- missing hardening and validation

Expected output:
- Rank findings by severity.
- Create or update SECURITY_REVIEW.md only if explicitly asked or if repository policy requires it.
- Suggest concrete remediations.
- If you fix anything automatically, explain what changed.
