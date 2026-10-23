# Sivdhi

## Local setup
1. Use the Python version expected by the project (the recovered `routes/_auth_recovered.pyc` is CPython 3.14 bytecode).
2. Install dependencies: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and configure a fresh `SECRET_KEY`, database settings, and SMTP credentials. Never share or publish `.env`.
4. Start locally with `python app.py`.
5. For production WSGI deployments, run the assignment monitor as a separate always-on process: `python -m utils.assignment_monitor`. The in-process monitor starts when `app.py` is run directly, but multi-worker WSGI deployments should not rely on a per-web-worker daemon.

## Assignment timing
The system divides the 60-minute assignment budget by the eligible worker count. During each assigned worker's response window, reminders are issued at one-third and two-thirds of the allotted time. When the response deadline expires, reassignment starts immediately. For example, six eligible workers means a 10-minute response window; reminders are sent around 3m20s and 6m40s, then the next eligible worker is attempted at the 10-minute deadline.

## Email
In-app notifications are stored in the database. Email is queued in the transactional outbox and sent by the assignment monitor. Configure valid SMTP credentials in `.env`; delivery cannot be guaranteed without working SMTP credentials.

See `CHANGES_FINAL.md` and `QA_RESULTS.md` for the latest change and verification notes.
