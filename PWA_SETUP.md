# Sivdhi installable web apps

This source provides two installable web apps on the existing Sivdhi origin.

## 1. Sivdhi Customer

- Manifest: `/static/pwa/customer/manifest.json`
- Linked from every HTML page under `templates/customer/`.
- Start URL: `/customer/home`
- Scope: `/customer/`
- If the customer is signed out, the existing customer route guard redirects to `/customer/login`.

## 2. Sivdhi Worker & Partner

- Manifest: `/static/pwa/workpartner/manifest.json`
- Linked from every HTML page under `templates/worker/` and `templates/partner/`.
- Start URL: `/login` (the existing shared sign-in route).
- Scope: `/` so one installed app can navigate both existing `/worker/...` and `/partner/...` route families.
- Existing sign-in, role selection, session, and dashboard routing are not changed.

The Worker & Partner scope must be `/` because the existing Worker and Partner pages live in two different top-level URL paths. This broad scope also means same-origin Admin URLs are technically within that installed app's navigation scope. Admin pages do not link an Admin manifest and no separate Admin app is created. For strict OS-level isolation of Admin, the Admin panel would need a separate origin/subdomain or a URL restructuring.

## Install

Serve Sivdhi over HTTPS. In Chrome or Edge, open a Customer page to install **Sivdhi Customer**. For **Sivdhi Worker & Partner**, sign in and open a Worker or Partner page, then use the browser's Install app action. Browser menu wording varies by browser and device.

Both manifests include `name`, `short_name`, `start_url`, `scope`, `display`, and 192px/512px PNG icons. No service worker or offline cache has been added. A service worker is not required for installation in current Chromium-based browsers, but the app still needs a network connection and does not provide offline operation.

## Deployment and data safety

This is a source-only archive. It excludes the SQLite database and its WAL/SHM files, `.secret_key`, `.env`, uploaded user files, and generated Python caches. It retains `routes/_auth_recovered.pyc` because the current `routes/auth.py` loads that exact CPython 3.14 bytecode as its authentication implementation. Keep the production database, `.secret_key`, and `.env` on the AWS server; never commit them to Git.
