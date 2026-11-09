# Sivdhi Admin access and security setup

## Admin website only

Admin is not part of the Android app persistent-login roles. The app session ledger accepts only Customer, Worker and Partner identities; Admin remains a website session.

The Admin blueprint is mounted below `ADMIN_PANEL_PATH`. The effective login URL is:

`https://YOUR-DOMAIN/<ADMIN_PANEL_PATH>/admin/login`

The former `/admin/...` and `/setup-admin` paths are not registered. Admin templates and redirects use the configured prefix. The prefix is an additional discovery barrier, not a substitute for authentication. Admin pages still require a valid Admin account and a signed session.

## Production environment variables

Set these on the server, not in source control:

- `APP_ENV=production`
- `ADMIN_PANEL_PATH`: 32-96 random characters using letters, numbers, `_` or `-`. Do not use the example placeholder.
- `SECRET_KEY`: stable random secret of at least 32 characters. Keep it private and do not rotate it casually because doing so signs users out.
- `ADMIN_MFA_ENCRYPTION_KEY`: a valid Fernet key, separate from `SECRET_KEY`. Generate it with `py -3.14 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Store it in the server's private environment and back it up securely. Losing or changing this key prevents decryption of enrolled Admin TOTP secrets.
- `SESSION_COOKIE_SECURE=1` (production config forces Secure cookies regardless).
- `TRUSTED_PROXY_HOPS=1` only when the app is behind exactly one trusted reverse proxy such as the configured Nginx layer. Do not trust client-supplied forwarding headers directly.
- `ADMIN_SETUP_KEY`: only during one-time first-Admin bootstrap; remove it after the account is created. The bootstrap route is `/<ADMIN_PANEL_PATH>/bootstrap` and returns 404 after an Admin exists.

Example PowerShell generation for the Admin path:

```powershell
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$AdminPanelPath = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
$AdminPanelPath
```

Use the generated value as `ADMIN_PANEL_PATH` in the server's private `.env` file. Keep `.env` out of GitHub and backups shared publicly. The app deliberately refuses to start in production when this value is missing, too short, or still a placeholder.

## Controls included

- Admin URL prefix required in production; default Admin URLs are not registered.
- Existing password-hash verification and shared login throttling remain in place. Admin password and MFA attempts are throttled independently by identifier+IP, account across IPs and IP across identifiers.
- Admin login is password → mandatory authenticator-based TOTP. New/existing Admin accounts without MFA are forced through enrollment before a full Admin session is created.
- TOTP secrets are encrypted at rest with the independent Fernet key. TOTP time counters are stored to reject replay. Ten high-entropy recovery codes are issued at enrollment; only keyed hashes are stored and each code is one-time.
- MFA-pending sessions expire after five minutes and do not receive `role=admin`. Full Admin sessions are created only after successful TOTP or an unused recovery code.
- `admin_security.is_active` is checked on every Admin request. Setting it to `0` revokes access at the next request; `utils.admin_mfa.set_admin_active(admin_id, False)` is the operator-side kill switch.
- CSRF protection remains enabled for state-changing browser forms.
- Admin sessions end after 30 minutes without an Admin request, and have an 8-hour absolute maximum age.
- Admin sessions are not eligible for Android persistent sessions.
- Admin responses are marked `noindex` and `no-store`; this reduces indexing and browser/proxy caching but is not access control.
- Existing production Secure/HttpOnly/SameSite cookie and security-header configuration remains active.

## Before public production deployment

1. Configure the private `ADMIN_PANEL_PATH` and verify the new Admin login URL.
2. Keep `SECRET_KEY` stable and private; ensure `.env`, database files, Firebase service-account JSON, APK signing keys and keystores are not committed.
3. Ensure Nginx exposes only 80/443 publicly, with SSH restricted to a trusted source IP. Keep the Flask/Waitress port bound to localhost or otherwise blocked at the security-group/firewall layer.
4. Configure and back up `ADMIN_MFA_ENCRYPTION_KEY` before first Admin login. Existing Admin accounts must enroll an authenticator on their next password login. Store the one-time recovery codes privately.
5. Add an edge access policy such as Cloudflare Access or a VPN for the Admin path if practical; a secret URL alone cannot stop a person who learns the path from reaching the login page.
6. Keep OS, Python dependencies and Android dependencies patched; maintain encrypted off-server database backups and test restore procedures. Back up the MFA encryption key separately from the database.

A secret URL cannot make an endpoint impossible to discover. Anyone who learns the path will still reach the login page; authentication, rate limiting, CSRF, session expiry, MFA and network controls are the actual protections.
