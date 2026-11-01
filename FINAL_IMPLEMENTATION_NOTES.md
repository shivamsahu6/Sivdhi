# Sivdhi – Final source changes (1 Nov 2026)

## Booking schedule and worker matching
- Normal booking slots close for the current day at 6:00 PM; a new Normal booking after that time must be for tomorrow or later.
- Normal matching prefers workers within 5 km and extends to the worker's eligible maximum, capped at 15 km.
- Full-Day matching prefers workers within 10 km and extends to the eligible maximum, capped at 30 km.
- Worker profile range is capped at 30 km on both the form and server validation.
- Worker heartbeat freshness is three days. A Partner's explicit Online override can keep an assigned worker eligible without a worker-side smartphone heartbeat; explicit Offline always excludes that worker.

## Partner controls
- Partner can set each linked worker Online or Offline.
- Partner can Accept, Arrive, Start Job, and Complete Job for that Partner's own assigned worker.
- Arrive is restricted until one hour after the customer's scheduled booking date/time.
- Start Job requires arrival and any outstanding balance to be recorded as paid.
- Complete Job requires the job to be in progress, arrival to be recorded, and all required payment statuses to be paid; normal settlement is then recorded.
- Partner booking scope supports both database worker IDs and public worker IDs for legacy assignments.
- Customer phone number is not displayed in Partner booking details.

## Payment and notifications
- Assignment timeout does not mark a refund as completed. A paid, unassigned booking is cancelled with refund status pending Admin action.
- Admin must transfer the amount to the customer's bank outside Sivdhi, then use the Refund action to record the completed bank transfer. The optional bank reference is saved; duplicate refund recording is blocked.
- Recording a refund creates the customer's Refund Processed notification/email. Cancellation sends the customer a Booking Cancelled notification/email. Booking creation sends Booking Confirmed. Internal assignment, arrival, start and completion events are kept with workers/partners and are not sent to customers.

## Database compatibility
- Additive migration adds the Partner availability override to workers and refund status/amount/reference/timestamp to bookings.
- The authentication backup `routes/_auth_recovered.pyc` from the supplied source archive is included because `routes/auth.py` uses it. It is a CPython 3.14 bytecode artifact; use the project's matching Python 3.14 runtime unless the original auth source is restored.

## Verification performed
- 68 tests passed with the two Flask-dependent test modules excluded because Flask is not installed in this isolated editing environment.
- Python `compileall` passed.
- All 108 HTML templates parsed successfully with Jinja.
- Temporary SQLite database migration check passed, including the new worker and booking columns.
- Flask request-level/browser tests, live SMTP delivery, payment gateway transactions, and a full production deployment were not run here. The manual refund action records a transfer; it does not transfer money.
