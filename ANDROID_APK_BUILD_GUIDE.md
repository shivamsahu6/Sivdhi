# Sivdhi Android APK Builder (Android only)

This project creates two separate Android APKs that open the existing live Sivdhi service inside a native Android WebView container. It does **not** create Chrome home-screen shortcuts.

## Apps

- Debug APK package `com.sivdhi.customer.debug` — Sivdhi Customer, starts at `https://sivdhi.duckdns.org/customer/login`
- Debug APK package `com.sivdhi.workpartner.debug` — Sivdhi Worker & Partner, starts at `https://sivdhi.duckdns.org/login`

Both apps use the same live Sivdhi backend and production database over HTTPS; the APK project does not bundle or copy the server database. The Worker + Partner APK is one app; users get the appropriate dashboard after signing in. Admin is not a separate app.

## Build APKs through GitHub Actions

1. Copy `android-apk/` and `.github/workflows/build-sivdhi-android-apks.yml` into the root of the Sivdhi GitHub repository.
2. Commit and push those new files to `main`.
3. Open GitHub repository → **Actions** → **Build Sivdhi Android APKs** → **Run workflow**.
4. Wait for the run to finish successfully, open the run summary and download the artifact named **Sivdhi-Android-APKs**.
5. The artifact contains:
   - `Sivdhi-Customer.apk`
   - `Sivdhi-Worker-Partner.apk`
   - `SHA256SUMS.txt`

The workflow uses JDK 17, Android SDK API 36 / Build Tools 36.0.0, Android Gradle Plugin 9.5.0 and Gradle 9.6.0.

## Important signing note

The workflow currently produces **debug-signed APKs for installation/testing**. They are real signed APK files and can be installed directly on Android after allowing installation from the browser/file manager. The debug application IDs end in `.debug`. These builds are suitable for first device testing, but not the final long-term public-release signing setup: a permanent private release keystore must be created and secured before publishing stable updates. Do not upload a keystore or its password into the Git repository. A later release build with a different signing key may require uninstalling the debug build first.

## App behavior

- Opens Sivdhi in a standalone Android Activity (not Chrome).
- JavaScript, DOM storage and cookies enabled for Flask session/login flows.
- Only HTTPS requests to `sivdhi.duckdns.org` remain inside the app; external links open in the appropriate external app/browser.
- File chooser, downloads, geolocation and camera/microphone permissions are handled through Android runtime permission prompts when the website requests them.
- SSL certificate errors are rejected; the app never bypasses HTTPS validation.
- Internet connection is required. Offline caching is not included.

## Requirements

GitHub Actions performs the build; no Android Studio/SDK/Gradle installation is required on the developer PC for this workflow.

## Install on Android

Download the Actions artifact ZIP to the phone (or copy the APKs to it), extract it, then tap the required `.apk`. Android may ask you to allow the browser or Files app to install unknown apps. Enable that permission for the source you used, return to the APK, and tap Install. Install Customer and Worker + Partner separately.
