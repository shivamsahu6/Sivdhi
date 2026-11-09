# Sivdhi QA results — live assignment and notification update

## Checks completed
- `tests/test_assignment_monitor.py`: **16 passed**. Includes the new live-registration test: a worker becoming eligible at 10:20 joins the same booking, the current offer pool is re-divided, and the original 11:00 deadline remains fixed.
- `tests/test_booking_schedule.py`, `tests/test_finance_flow.py`, `tests/test_login_security.py`, `tests/test_otp_security.py`, `tests/test_payment_policy.py`, `tests/test_auth_recovery_templates.py`, and `tests/test_static_security.py` also passed in the available test run.
- Combined focused test run: **59 passed**. The environment lacked `python-dotenv` and Flask, so test-only stubs were used for the email sender/dotenv imports; this did not test real SMTP or launch Flask.
- Python syntax compilation passed for all project Python files.
- SQLite migration and notification-email trigger were exercised against a copy of the supplied Sivdhi database; customer/worker/partner trigger inserts executed successfully.
- An isolated SQLite live-offer simulation passed: Worker A initially had the hour; Worker B became eligible at 10:20; both response deadlines moved to 10:40 while the hard booking deadline remained 11:00. Worker/partner in-app notices and email outbox rows were created.
- ZIP integrity and required `routes/_auth_recovered.pyc` compatibility artifact are checked before packaging.

## Limits that remain explicit
- Full Flask startup/browser end-to-end testing could not run in this environment because Flask is not installed here and package installation is unavailable. The included auth recovery bytecode is a CPython 3.14 artifact, while the available interpreter is Python 3.13.
- Actual SMTP delivery cannot be verified without the user's valid `EMAIL` and `EMAIL_APP_PASSWORD` settings in the local `.env`. Notification events are queued; the monitor attempts delivery and retries failures.
- Live matching polls from the assignment monitor (2-second default interval); the new worker is discovered on the next monitor pass, not by a synchronous hook inside the opaque recovered signup bytecode.

## Admin customer management follow-up (2026-10-23)
- Python AST parsing passed for `routes/admin.py`, `routes/customer.py`, `routes/decorators.py`, and `app.py`.
- Jinja parse passed for admin customer list, customer details, deleted accounts, and dashboard templates.
- SQLite migration and simulated suspend/delete/restore semantics passed on a disposable copy of the bundled database; existing customer rows were preserved.
- Full Flask route/browser integration test was not run in this environment because Flask is not installed here. Email delivery is unrelated to this admin UI addition and was not tested in this run.

## Clean source package audit — 2026-11-09

- Retained the ZIP-root Sivdhi project as the sole source tree; removed nested duplicate project copies and local-only/backup artifacts from the clean package.
- Corrected live assignment offer handling: active offer response windows are rebalanced when new eligible workers join, while the original global booking deadline remains fixed; response-expired offers are marked expired even when the worker did not open the request.
- Clarified the customer cancellation notice: it uses the `Booking Cancelled` title and states that the Admin bank transfer is pending; the application does not claim that an automatic bank refund has occurred.
- Executable test subset: **74 passed** across Admin MFA, assignment monitor, authentication-template/static checks, booking schedule, finance, login security, OTP security, payment policy, partner/admin controls, and static security.
- Python AST/syntax checks: **37/37 passed**. Jinja template parse: **111/111 passed**.
- Full test collection remains environment-blocked for `test_admin_route_security.py`, `test_app_persistent_sessions.py`, `test_customer_category_visibility.py`, and `test_push_notifications.py` because Flask is not installed in this audit container.
- The recovered authentication artifact `routes/_auth_recovered.pyc` is compiled for CPython 3.14; this audit container runs Python 3.13.5, so authentication runtime and full Flask startup could not be verified here. Run the complete suite under the project's Python 3.14 environment with the requirements installed before production deployment.
- Real SMTP/FCM delivery and Android Gradle builds were not exercised in this environment; these require the configured provider credentials/Firebase project and the Gradle toolchain.

## Full Windows Python 3.14 test run — 2026-11-09

- After installing the declared project dependencies and `pytest`, the first full collection exposed a missing Windows IANA timezone database (`Asia/Kolkata`). `tzdata>=2025.2` is now declared in `requirements.txt` so the timezone data is installed with the application dependencies on Windows.
- The Android session-lifetime regression test fixture now includes a test-only `sivdhi_app_session_id`, matching the production rule that 365-day Android cookie lifetime is granted only to a session with a server-issued persistent-session token. No production session-policy code was weakened or changed for this test correction.
- User-reported full test command: `python -m pytest -q -ra` under the project's Windows Python 3.14 virtual environment. Result: **93 passed in 4.86s**.
- The same run had no failures, collection errors, skips, or xfails in the displayed pytest summary.
- This run verifies the repository's automated tests. It does not by itself verify real SMTP/FCM delivery, production secrets, Android Gradle builds, or a live browser/device end-to-end deployment.
