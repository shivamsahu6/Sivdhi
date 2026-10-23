# Sivdhi change log — live worker assignment, notifications, and privacy

## Live assignment during the same one-hour window
- The assignment monitor now discovers newly eligible workers during an existing pending/assigned booking, including workers who register or become active after the booking started.
- Matching uses the existing category, availability/schedule, location, and each worker's configured service radius rules.
- Newly eligible workers receive an offer and their partner receives an alert. The existing worker's response slice is recalculated when the eligible offer pool changes.
- Each worker's allocated response window is divided from the remaining time. No recalculation can extend the booking's original 60-minute deadline. Two reminders are scheduled inside each active allocation window, and each partner receives one reminder per pool stage rather than one duplicate per worker.
- Worker acceptance is guarded by a SQLite write transaction and a conditional booking update. The first accepted offer locks the booking and cancels all other outstanding offers.
- The monitor keeps progressing through unresponsive/rejected workers and runs the existing no-worker/deadline handling when the fixed deadline expires.

## Customer-facing booking status
- Customer booking cards use customer-facing lifecycle labels rather than internal assignment states.
- Internal rejection, timeout, worker retries, and reassignment details are not sent to the customer.
- Customer notifications are limited to booking lifecycle updates and relevant payment/refund notices; worker and partner assignment operations stay internal.

## Notifications and email
- In-app customer/worker/partner notifications automatically create transactional email outbox rows for the registered email when available.
- Email recipient lookup supports internal and public worker/partner identifiers where those columns exist, and legacy database schemas remain supported.
- The assignment monitor delivers pending outbox emails and records retry/error state when SMTP delivery fails.
- Assignment pool changes send each offered worker an updated response deadline and send partner updates without repeating the same partner notification multiple times in one pool update.
- Booking arrival, start, completion, and demo balance-payment events notify the relevant worker/partner/customer roles.

## Privacy
- Partner job lists/details no longer expose the customer's mobile number. Customer mobile is available only on the worker-side booking view as required by the call workflow.
- Customer-facing booking pages do not display worker or partner phone numbers.

## Verification limits
- Focused test suite: 59 passed, including a live-registration/time-split simulation.
- Python syntax and SQLite migration/trigger checks passed.
- Full Flask/browser end-to-end tests and live SMTP delivery remain unverified in this environment. Configure `.env` with valid SMTP credentials and run the project using its supported Python 3.14 runtime before production use.

## Admin customer management (2026-10-23)
- Added customer account status fields (`status`, `is_deleted`) through additive SQLite migrations that preserve existing customer records.
- Admin dashboard now links to customer management and shows a customer total.
- Customer list includes account status, management, and soft-delete actions.
- Customer details page supports suspend/activate and moving accounts to Deleted Accounts.
- Deleted Accounts now includes customers with restore and permanent-delete actions.
- Permanent deletion is blocked when a customer has booking history, preserving booking records.
- Customer protected routes already use the customer login guard; it now has the status fields available and clears sessions for suspended/deleted accounts.
