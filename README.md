# Sivdhi

## Local setup
1. Use the Python version expected by the project (the recovered `routes/_auth_recovered.pyc` is CPython 3.14 bytecode).
2. Install dependencies: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and configure a fresh `SECRET_KEY`, database settings, and SMTP credentials. Never share or publish `.env`.
4. Start locally with `python app.py`.
5. For production WSGI deployments, run the assignment monitor as a separate always-on process: `python -m utils.assignment_monitor`. The in-process monitor starts when `app.py` is run directly, but multi-worker WSGI deployments should not rely on a per-web-worker daemon.

## Assignment timing
The system sends a booking request to the five nearest eligible workers together. Every 15 seconds it adds the next five nearest workers, while every earlier request stays active. The first worker whose acceptance reaches the server receives the booking; all other pending offers stop immediately. If nobody accepts within the fixed 60-minute window, the booking is cancelled and the customer receives an in-app notification and email with the refund-request status.

## Email
In-app notifications are stored in the database. Email is queued in the transactional outbox and sent by the assignment monitor. Configure valid SMTP credentials in `.env`; delivery cannot be guaranteed without working SMTP credentials.

See `CHANGES_FINAL.md` and `QA_RESULTS.md` for the latest change and verification notes.
