# Security and release notes

- The distributable excludes `.env` and `.secret_key`; create a local `.env` from `.env.example` and use a fresh secret key.
- Do not share SMTP app passwords, production database backups, or user uploads publicly.
- Customer-facing service data strips internal commission fields. Checkout recalculates service amounts from the database rather than trusting submitted prices.
- Legacy problem-price data is retained for compatibility, but the new customer flow no longer asks customers to choose a problem item.
- Keep the included SQLite database backed up before running migrations or testing. For public production traffic, use a managed database, HTTPS, process supervision, and a real payment provider.
- A real bank refund is not possible until a payment gateway integration supports refunds and verifies provider responses.
- See `QA_RESULTS.md`; runtime/browser and provider-level tests were not available in this build environment.
