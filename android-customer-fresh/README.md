# Sivdhi Customer Android — Fresh Build

Customer-only Android app source. It keeps the existing Sivdhi Flask website/backend and database unchanged.

## App identity

- Package: `com.sivdhi.customer`
- Version: `1.0.3` (version code 4)
- Android: min SDK 24, target SDK 36
- Java 17, Android Gradle Plugin 9.5.0, Gradle 9.6.0

## What this app does

- Opens the existing Sivdhi customer site in a dedicated Android WebView.
- Keeps trusted-host-only HTTPS navigation, cookie/session handling, geolocation, file chooser and downloads.
- Identifies app traffic with both the existing persistent-session marker and the Cashfree-native marker.
- Opens Cashfree hosted checkout with Cashfree's official Android SDK (not Chrome).
- Checks the callback order reference, then returns to Sivdhi's server-side `/customer/cashfree/return` endpoint. The Android callback never marks an order paid.
- Shows a retry/offline screen and retries server reconciliation after connectivity returns.
- Keeps Firebase Cloud Messaging registration/service.

## Build

The GitHub Actions workflow builds a release APK and verifies its signature. It uses the existing repository Actions secrets:
`SIVDHI_RELEASE_KEYSTORE_BASE64`, `SIVDHI_RELEASE_STORE_PASSWORD`,
`SIVDHI_RELEASE_KEY_ALIAS`, and `SIVDHI_RELEASE_KEY_PASSWORD`.

Local release builds use the same environment variables and the user's existing private PKCS12 keystore. Do not put a keystore or password in this source tree or upload either to chat.

## Critical runtime dependency

The app requires the current Sivdhi HTTPS host. It does not bundle website pages, Flask code, database data, Cashfree merchant secrets, or payment sessions. If the server's public TCP 443 endpoint is unreachable, login/bookings/payment cannot work in any client app until that network/server issue is fixed.

## Payment safety

Cashfree order/session values arrive only from the trusted Sivdhi payment page. The SDK callback is treated only as a signal to return to the server; final order/payment status remains a backend verification decision.
