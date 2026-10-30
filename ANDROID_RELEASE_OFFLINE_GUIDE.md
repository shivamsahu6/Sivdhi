# Sivdhi Android release signing + offline fallback

This update covers the two Android apps already in this repository:

- Customer: `com.sivdhi.customer`
- Worker + Partner: `com.sivdhi.workpartner`

It does not change Flask routes, booking/assignment logic, the production database, customer/worker privacy rules, or the Admin panel.

## A. What changed

### Release signing

- Debug builds remain available for internal testing. Their application IDs retain the `.debug` suffix.
- Release builds use the stable production package IDs above.
- Release signing reads the PKCS12 keystore and passwords only from environment variables supplied by GitHub Actions Secrets.
- No keystore, password, or private signing data is committed to this repository.
- The workflow defaults to a debug build. A release build must be selected manually after all four Actions secrets are configured.

### Offline behavior (safe first phase)

- The Android app shows a native Sivdhi offline screen when no validated Internet connection is available or the Sivdhi page cannot be reached.
- It observes Android network changes and offers a manual Retry/Reload action when connectivity returns.
- It does not auto-resubmit failed forms or payment requests.
- Booking creation, payment verification, worker assignment, notifications, and live account data still require the online Flask server.
- This phase does **not** cache authenticated booking pages or save an offline booking draft. Caching private pages or silently replaying POST requests could expose account data or create duplicate bookings. A real offline draft/sync feature needs explicit server-side idempotency and conflict handling and should be implemented as a separate backend change.

## B. Create the permanent release key (on your Windows PC)

1. Ensure a JDK is installed and `keytool` is available in PowerShell. JDK 17 is suitable for this build.
2. Extract this update ZIP.
3. Run `CREATE_SIVDHI_RELEASE_KEY.ps1` in PowerShell. It creates:
   `%USERPROFILE%\Documents\SivdhiSigning\sivdhi-release.p12`
4. Choose a strong password. When `keytool` asks for the private-key password, press Enter to use the same password as the PKCS12 keystore.
5. The script copies the base64 representation of the keystore to the clipboard. It does not print the key material.
6. Keep the `.p12` file in at least two secure backups (for example, an encrypted USB and a secure offline backup). Keep the password in a password manager. Never put the `.p12` file in the GitHub repository or send it in chat.

**This key must be retained permanently.** Future updates to the release APKs must be signed with the same key. Losing it can prevent updates to installs signed with it.

## C. Add GitHub Actions Secrets

In `https://github.com/shivamsahu6/Sivdhi`:

1. Open **Settings → Secrets and variables → Actions → New repository secret**.
2. Create these exact secrets:

| Secret name | Value |
|---|---|
| `SIVDHI_RELEASE_KEYSTORE_BASE64` | Paste the base64 text copied by the PowerShell script |
| `SIVDHI_RELEASE_STORE_PASSWORD` | The PKCS12 keystore password |
| `SIVDHI_RELEASE_KEY_ALIAS` | `sivdhi-release` |
| `SIVDHI_RELEASE_KEY_PASSWORD` | Same password as the PKCS12 keystore |

Do not add the password or keystore file to any tracked file. GitHub masks configured secret values in workflow logs, but workflow code should still never print them.

## D. Build the APKs

1. Push the Android update commit to `main`.
2. Open **Actions → Build Sivdhi Android APKs → Run workflow**.
3. Choose:
   - `debug` for test APKs (does not need a release key), or
   - `release` for production-signed APKs (requires all four secrets above).
4. After a successful run, download the artifact:
   - `Sivdhi-Android-APKs-debug`, or
   - `Sivdhi-Android-APKs-release`.
5. Extract the ZIP. It contains `Sivdhi-Customer.apk`, `Sivdhi-Worker-Partner.apk`, `SHA256SUMS.txt`, and `BUILD_TYPE.txt`.

The release APKs use package IDs `com.sivdhi.customer` and `com.sivdhi.workpartner`. Debug APKs use `.debug` suffixes and can be installed alongside the release APKs. A release update must use the same release key and a higher `versionCode` than the previous release; increment `versionCode` in `android-apk/app/build.gradle` for each future public update.

## E. Offline behavior test

1. Install a newly built APK and sign in while Internet is available.
2. Turn off both Wi-Fi and mobile data. The app should show its native offline screen.
3. Restore Internet and tap **Try again** / **Reload Sivdhi** if prompted.
4. Confirm that the app returns to the live Sivdhi page.
5. Do not treat a booking/payment as complete until the live server confirms it.

## F. Production safety

This update only changes Android build and wrapper files. It does not require an AWS database migration and does not require changes to the Flask backend. The live server can remain untouched while the APKs are built and tested.
