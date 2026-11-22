package com.sivdhi.mobile;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.SafeBrowsingResponse;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.net.http.SslError;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;
import android.util.Log;

import com.cashfree.pg.api.CFPaymentGatewayService;
import com.cashfree.pg.core.api.CFSession;
import com.cashfree.pg.core.api.callback.CFCheckoutResponseCallback;
import com.cashfree.pg.core.api.exception.CFException;
import com.cashfree.pg.core.api.utils.CFErrorResponse;
import com.cashfree.pg.core.api.webcheckout.CFWebCheckoutPayment;
import com.cashfree.pg.core.api.webcheckout.CFWebCheckoutTheme;

import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

public class MainActivity extends Activity implements CFCheckoutResponseCallback {
    private static final String TRUSTED_HOST = "sivdhi.duckdns.org";
    private static final int FILE_CHOOSER_REQUEST = 4101;
    private static final int GEO_PERMISSION_REQUEST = 4102;
    private static final int MEDIA_PERMISSION_REQUEST = 4103;
    private static final int NOTIFICATION_PERMISSION_REQUEST = 4104;

    private WebView webView;
    private ProgressBar progressBar;
    private FrameLayout root;
    private LinearLayout offlinePanel;
    private TextView offlineTitle;
    private TextView offlineMessage;
    private Button offlineRetryButton;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private boolean networkCallbackRegistered = false;
    private boolean networkAvailable = false;
    private boolean pageLoadFailed = false;
    private String lastKnownUrl;
    private ValueCallback<Uri[]> fileCallback;
    private GeolocationPermissions.Callback pendingGeoCallback;
    private String pendingGeoOrigin;
    private PermissionRequest pendingMediaRequest;
    private String startUrl;
    private String pendingCashfreeOrderId;
    private String deferredCashfreeReturnOrderId;
    private boolean cashfreeCheckoutActive = false;
    private int themeColor = Color.rgb(124, 58, 237);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        readAppConfiguration();
        String notificationTarget = notificationUrlFromIntent(getIntent());
        if (notificationTarget != null) startUrl = notificationTarget;
        configureSystemBars();
        buildLayout();
        configureWebView();
        registerCashfreeCallback();
        if (savedInstanceState != null) {
            pendingCashfreeOrderId = savedInstanceState.getString("sivdhi.cashfree.pending_order");
            deferredCashfreeReturnOrderId = savedInstanceState.getString("sivdhi.cashfree.deferred_order");
            cashfreeCheckoutActive = savedInstanceState.getBoolean("sivdhi.cashfree.active", false);
        }
        configureNetworkMonitoring();
        networkAvailable = hasInternetNetwork();
        requestNotificationPermissionIfNeeded();

        if (savedInstanceState != null) {
            Bundle webState = savedInstanceState.getBundle("sivdhi.webview.state");
            if (webState != null && webView.restoreState(webState) != null) {
                lastKnownUrl = webView.getUrl();
                if (notificationTarget != null) {
                    lastKnownUrl = notificationTarget;
                    if (networkAvailable) webView.loadUrl(notificationTarget);
                }
                if (!networkAvailable) {
                    showOfflinePanel(false);
                }
                return;
            }
        }

        if (networkAvailable) {
            lastKnownUrl = startUrl;
            webView.loadUrl(startUrl);
        } else {
            pageLoadFailed = true;
            showOfflinePanel(false);
        }
    }

    private void readAppConfiguration() {
        try {
            ApplicationInfo info = getPackageManager().getApplicationInfo(
                    getPackageName(), PackageManager.GET_META_DATA);
            Bundle meta = info.metaData;
            startUrl = meta.getString("com.sivdhi.START_URL");
        } catch (Exception ignored) {
            startUrl = null;
        }
        if (startUrl == null || !isTrustedUrl(startUrl)) {
            startUrl = "https://sivdhi.duckdns.org/login";
        }
        if (getPackageName().startsWith("com.sivdhi.customer")) {
            themeColor = Color.rgb(124, 58, 237);
        } else {
            themeColor = Color.rgb(15, 118, 110);
        }
    }

    private void configureSystemBars() {
        Window window = getWindow();
        window.setStatusBarColor(themeColor);
        window.setNavigationBarColor(Color.rgb(250, 249, 252));
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            window.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        }
    }

    private int dp(float value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void buildLayout() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(250, 249, 252));
        root.setFitsSystemWindows(true);

        webView = new WebView(this);
        webView.setBackgroundColor(Color.WHITE);
        FrameLayout.LayoutParams webParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        root.addView(webView, webParams);

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setProgressTintList(android.content.res.ColorStateList.valueOf(themeColor));
        progressBar.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(themeColor));
        FrameLayout.LayoutParams progressParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(3), Gravity.TOP);
        root.addView(progressBar, progressParams);
        buildOfflinePanel();

        setContentView(root);
    }

    private void buildOfflinePanel() {
        offlinePanel = new LinearLayout(this);
        offlinePanel.setOrientation(LinearLayout.VERTICAL);
        offlinePanel.setGravity(Gravity.CENTER);
        offlinePanel.setPadding(dp(28), dp(28), dp(28), dp(28));
        offlinePanel.setBackgroundColor(Color.rgb(250, 249, 252));
        offlinePanel.setVisibility(View.GONE);
        offlinePanel.setClickable(true);
        offlinePanel.setFocusable(true);

        TextView mark = new TextView(this);
        mark.setText("SIVDHI");
        mark.setTextColor(themeColor);
        mark.setTextSize(14);
        mark.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        mark.setLetterSpacing(0.12f);
        mark.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams markParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        markParams.bottomMargin = dp(20);
        offlinePanel.addView(mark, markParams);

        offlineTitle = new TextView(this);
        offlineTitle.setText("You're offline");
        offlineTitle.setTextColor(Color.rgb(30, 27, 41));
        offlineTitle.setTextSize(23);
        offlineTitle.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        offlineTitle.setGravity(Gravity.CENTER);
        offlinePanel.addView(offlineTitle, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        offlineMessage = new TextView(this);
        offlineMessage.setTextColor(Color.rgb(91, 87, 103));
        offlineMessage.setTextSize(15);
        offlineMessage.setGravity(Gravity.CENTER);
        offlineMessage.setLineSpacing(dp(4), 1.0f);
        LinearLayout.LayoutParams messageParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        messageParams.topMargin = dp(14);
        messageParams.bottomMargin = dp(24);
        offlinePanel.addView(offlineMessage, messageParams);

        offlineRetryButton = new Button(this);
        offlineRetryButton.setText("Try again");
        offlineRetryButton.setTextColor(Color.WHITE);
        offlineRetryButton.setTextSize(15);
        offlineRetryButton.setAllCaps(false);
        offlineRetryButton.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        GradientDrawable buttonBackground = new GradientDrawable();
        buttonBackground.setColor(themeColor);
        buttonBackground.setCornerRadius(dp(14));
        offlineRetryButton.setBackground(buttonBackground);
        offlineRetryButton.setPadding(dp(28), dp(10), dp(28), dp(10));
        offlineRetryButton.setOnClickListener(v -> retryFromOfflinePanel());
        offlinePanel.addView(offlineRetryButton, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView safetyNote = new TextView(this);
        safetyNote.setText("Bookings, payments and worker assignments need a live connection. Sivdhi will not submit or confirm them while offline.");
        safetyNote.setTextColor(Color.rgb(112, 108, 123));
        safetyNote.setTextSize(12);
        safetyNote.setGravity(Gravity.CENTER);
        safetyNote.setLineSpacing(dp(3), 1.0f);
        LinearLayout.LayoutParams noteParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        noteParams.topMargin = dp(22);
        offlinePanel.addView(safetyNote, noteParams);

        FrameLayout.LayoutParams offlineParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        root.addView(offlinePanel, offlineParams);
    }

    @SuppressWarnings("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        // Identifies requests coming from the native app so the server can
        // apply the app-only persistent-session policy. This marker is not
        // used as an authentication credential.
        String appUserAgent = settings.getUserAgentString();
        if (appUserAgent == null) appUserAgent = "";
        StringBuilder appMarkers = new StringBuilder(appUserAgent);
        if (!appUserAgent.contains("SivdhiAndroidApp/1.0")) {
            appMarkers.append(" SivdhiAndroidApp/1.0");
        }
        if (!appUserAgent.contains("SivdhiCashfreeNative/1")) {
            appMarkers.append(" SivdhiCashfreeNative/1");
        }
        settings.setUserAgentString(appMarkers.toString());
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadsImagesAutomatically(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setGeolocationEnabled(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            cookies.setAcceptThirdPartyCookies(webView, false);
        }

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return routeUrl(request.getUrl().toString());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return routeUrl(url);
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                progressBar.setVisibility(View.VISIBLE);
                progressBar.setProgress(5);
                if (isTrustedUrl(url)) {
                    lastKnownUrl = url;
                }
                pageLoadFailed = false;
                if (networkAvailable && offlinePanel != null) {
                    offlinePanel.setVisibility(View.GONE);
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                progressBar.setProgress(100);
                progressBar.setVisibility(View.GONE);
                if (isTrustedUrl(url)) {
                    lastKnownUrl = url;
                }
                pageLoadFailed = false;
                if (networkAvailable && offlinePanel != null) {
                    offlinePanel.setVisibility(View.GONE);
                }
                CookieManager.getInstance().flush();
                if (isTrustedUrl(url)) syncPushTokenWithCurrentSession();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    pageLoadFailed = true;
                    progressBar.setVisibility(View.GONE);
                    showOfflinePanel(false);
                }
            }

            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                // Never bypass HTTPS certificate errors.
                handler.cancel();
                Toast.makeText(MainActivity.this, "Secure connection could not be verified.",
                        Toast.LENGTH_LONG).show();
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progressBar.setProgress(newProgress);
                progressBar.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                try {
                    Intent intent = params.createIntent();
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST);
                    return true;
                } catch (ActivityNotFoundException ex) {
                    fileCallback = null;
                    Toast.makeText(MainActivity.this, "No file picker is available on this device.",
                            Toast.LENGTH_LONG).show();
                    return false;
                }
            }

            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                    GeolocationPermissions.Callback callback) {
                Uri uri = Uri.parse(origin);
                if (!"https".equalsIgnoreCase(uri.getScheme()) ||
                        !TRUSTED_HOST.equalsIgnoreCase(uri.getHost())) {
                    callback.invoke(origin, false, false);
                    return;
                }
                if (hasLocationPermission()) {
                    callback.invoke(origin, true, false);
                } else {
                    pendingGeoOrigin = origin;
                    pendingGeoCallback = callback;
                    requestPermissions(new String[]{
                            Manifest.permission.ACCESS_FINE_LOCATION,
                            Manifest.permission.ACCESS_COARSE_LOCATION
                    }, GEO_PERMISSION_REQUEST);
                }
            }

            @Override
            public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> handleWebPermissionRequest(request));
            }
        });

        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            try {
                DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                request.setTitle(URLUtil.guessFileName(url, contentDisposition, mimeType));
                request.setDescription("Downloading from Sivdhi");
                request.setMimeType(mimeType);
                request.addRequestHeader("User-Agent", userAgent);
                String cookie = CookieManager.getInstance().getCookie(url);
                if (cookie != null) request.addRequestHeader("Cookie", cookie);
                request.setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,
                        URLUtil.guessFileName(url, contentDisposition, mimeType));
                DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                manager.enqueue(request);
                Toast.makeText(this, "Download started. Check Downloads.", Toast.LENGTH_LONG).show();
            } catch (Exception e) {
                Toast.makeText(this, "Could not start download.", Toast.LENGTH_LONG).show();
            }
        });
    }

    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    NOTIFICATION_PERMISSION_REQUEST
            );
        }
    }

    private String notificationUrlFromIntent(Intent intent) {
        if (intent == null) return null;
        String route = intent.getStringExtra("sivdhi_notification_route");
        if (route == null || !route.startsWith("/") || route.startsWith("//") ||
                route.contains("\\") || route.contains("..")) return null;
        String target = "https://" + TRUSTED_HOST + route;
        return isTrustedUrl(target) ? target : null;
    }

    private void syncPushTokenWithCurrentSession() {
        if (webView == null || !isTrustedUrl(webView.getUrl())) return;

        FirebaseMessaging.getInstance().getToken().addOnCompleteListener(task -> {
            if (!task.isSuccessful() || task.getResult() == null) return;
            try {
                JSONObject payload = new JSONObject();
                payload.put("token", task.getResult());
                payload.put("app_package", getPackageName());
                String serialized = payload.toString();
                String javascript = "(function(){try{" +
                        "const p=JSON.parse(" + JSONObject.quote(serialized) + ");" +
                        "const csrf=document.querySelector('meta[name=\"csrf-token\"]')?.content||'';" +
                        "if(!csrf)return;" +
                        "const loginPage=/\\/login(?:\\?|$)/.test(location.pathname);" +
                        "if(!loginPage&&sessionStorage.getItem('sivdhi_fcm_token')===p.token)return;" +
                        "fetch('/api/push/register',{method:'POST',credentials:'same-origin'," +
                        "headers:{'Content-Type':'application/json','X-CSRFToken':csrf}," +
                        "body:JSON.stringify(p),cache:'no-store'})" +
                        ".then(r=>{if(r.ok)sessionStorage.setItem('sivdhi_fcm_token',p.token);" +
                        "else if(r.status===401)sessionStorage.removeItem('sivdhi_fcm_token');})" +
                        ".catch(()=>{});" +
                        "}catch(e){}})();";
                runOnUiThread(() -> {
                    if (webView != null && isTrustedUrl(webView.getUrl())) {
                        webView.evaluateJavascript(javascript, null);
                    }
                });
            } catch (Exception ignored) {
                // A transient token/JSON failure is retried on the next trusted page load.
            }
        });
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String target = notificationUrlFromIntent(intent);
        if (target != null && webView != null) {
            if (hasInternetNetwork()) {
                networkAvailable = true;
                pageLoadFailed = false;
                lastKnownUrl = target;
                offlinePanel.setVisibility(View.GONE);
                webView.loadUrl(target);
            } else {
                lastKnownUrl = target;
                networkAvailable = false;
                showOfflinePanel(false);
            }
        }
    }

    private void configureNetworkMonitoring() {
        connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                refreshNetworkState();
            }

            @Override
            public void onLost(Network network) {
                refreshNetworkState();
            }

            @Override
            public void onCapabilitiesChanged(Network network, NetworkCapabilities capabilities) {
                refreshNetworkState();
            }
        };
    }

    private boolean hasInternetNetwork() {
        if (connectivityManager == null) {
            connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        }
        if (connectivityManager == null) return false;
        Network activeNetwork = connectivityManager.getActiveNetwork();
        if (activeNetwork == null) return false;
        NetworkCapabilities capabilities = connectivityManager.getNetworkCapabilities(activeNetwork);
        // Attempt the secure host whenever Android has an internet-capable
        // network. Requiring VALIDATED here can leave WebView blank on networks
        // that route traffic but report validation late or incorrectly.
        return capabilities != null &&
                capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
    }

    private void refreshNetworkState() {
        boolean available = hasInternetNetwork();
        runOnUiThread(() -> applyNetworkState(available));
    }

    private void applyNetworkState(boolean available) {
        networkAvailable = available;
        if (available && deferredCashfreeReturnOrderId != null && webView != null) {
            String orderId = deferredCashfreeReturnOrderId;
            deferredCashfreeReturnOrderId = null;
            if (offlinePanel != null) offlinePanel.setVisibility(View.GONE);
            lastKnownUrl = cashfreeReturnUrl(orderId);
            webView.loadUrl(lastKnownUrl);
            return;
        }
        if (!available) {
            showOfflinePanel(false);
            return;
        }

        if (offlinePanel != null && offlinePanel.getVisibility() == View.VISIBLE) {
            if (pageLoadFailed || webView == null || webView.getUrl() == null) {
                showOfflinePanel(true);
            } else {
                offlinePanel.setVisibility(View.GONE);
            }
        }
    }

    private void showOfflinePanel(boolean connectionRestored) {
        if (offlinePanel == null) return;
        progressBar.setVisibility(View.GONE);
        if (connectionRestored) {
            offlineTitle.setText("Connection restored");
            offlineMessage.setText("Your internet connection is back. Tap below to safely reload Sivdhi. Any interrupted booking or payment was not automatically resubmitted.");
            offlineRetryButton.setText("Reload Sivdhi");
        } else if (!networkAvailable) {
            offlineTitle.setText("You're offline");
            offlineMessage.setText("Connect to Wi-Fi or mobile data to use Sivdhi. The app can open without internet, but live account information and services need a connection.");
            offlineRetryButton.setText("Try again");
        } else {
            offlineTitle.setText("Couldn't reach Sivdhi");
            offlineMessage.setText("Your device appears to be online, but Sivdhi did not respond. Please retry. If this continues, the website server may be temporarily unavailable.");
            offlineRetryButton.setText("Retry Sivdhi");
        }
        offlinePanel.setVisibility(View.VISIBLE);
    }

    private void retryFromOfflinePanel() {
        if (!hasInternetNetwork()) {
            networkAvailable = false;
            showOfflinePanel(false);
            Toast.makeText(this, "No validated internet connection yet.", Toast.LENGTH_SHORT).show();
            return;
        }

        networkAvailable = true;
        pageLoadFailed = false;
        String target = isTrustedUrl(lastKnownUrl) ? lastKnownUrl : startUrl;
        offlinePanel.setVisibility(View.GONE);
        progressBar.setVisibility(View.VISIBLE);
        webView.loadUrl(target);
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (connectivityManager == null) {
            configureNetworkMonitoring();
        }
        if (!networkCallbackRegistered && connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.registerDefaultNetworkCallback(networkCallback);
                networkCallbackRegistered = true;
            } catch (RuntimeException ignored) {
                // The initial connectivity snapshot still works if callback registration is unavailable.
            }
        }
        applyNetworkState(hasInternetNetwork());
    }

    @Override
    protected void onStop() {
        if (networkCallbackRegistered && connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (RuntimeException ignored) {
                // Ignore an already-unregistered callback during lifecycle transitions.
            }
            networkCallbackRegistered = false;
        }
        super.onStop();
    }

    private boolean routeUrl(String rawUrl) {
        Uri uri = Uri.parse(rawUrl);
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        if ("sivdhi-cashfree".equals(scheme)) {
            beginNativeCashfreeCheckout(uri);
            return true;
        }
        if (("https".equals(scheme) || "http".equals(scheme)) &&
                TRUSTED_HOST.equalsIgnoreCase(uri.getHost()) && "https".equals(scheme)) {
            return false;
        }
        if ("https".equals(scheme) || "http".equals(scheme) || "tel".equals(scheme) ||
                "mailto".equals(scheme) || "sms".equals(scheme) || "upi".equals(scheme)) {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            } catch (ActivityNotFoundException e) {
                Toast.makeText(this, "No app found to open this link.", Toast.LENGTH_SHORT).show();
            }
            return true;
        }
        if ("intent".equals(scheme)) {
            try {
                Intent intent = Intent.parseUri(rawUrl, Intent.URI_INTENT_SCHEME);
                startActivity(intent);
            } catch (Exception e) {
                Toast.makeText(this, "Could not open this link.", Toast.LENGTH_SHORT).show();
            }
            return true;
        }
        return true;
    }


    private void registerCashfreeCallback() {
        try {
            CFPaymentGatewayService.getInstance().setCheckoutCallback(this);
        } catch (CFException exception) {
            Log.e("SivdhiCashfree", "Cashfree callback registration failed.");
        }
    }

    private boolean isTrustedPaymentPage() {
        if (webView == null || !isTrustedUrl(webView.getUrl())) return false;
        String path = Uri.parse(webView.getUrl()).getPath();
        return "/customer/payment".equals(path)
                || "/customer/payment/".equals(path)
                || "/customer/payments".equals(path)
                || "/customer/payments/".equals(path)
                || "/customer/bookings".equals(path)
                || "/customer/bookings/".equals(path);
    }

    private void beginNativeCashfreeCheckout(Uri uri) {
        if (cashfreeCheckoutActive) return;
        if (!"checkout".equalsIgnoreCase(uri.getHost()) || !isTrustedPaymentPage()) {
            Toast.makeText(this, "Please reopen checkout from Sivdhi.", Toast.LENGTH_LONG).show();
            return;
        }

        String orderId = uri.getQueryParameter("order_id");
        String paymentSessionId = uri.getQueryParameter("payment_session_id");
        if (paymentSessionId == null || paymentSessionId.isEmpty()) {
            paymentSessionId = uri.getQueryParameter("session_id");
        }
        String environment = uri.getQueryParameter("environment");
        orderId = orderId == null ? "" : orderId.trim();
        paymentSessionId = paymentSessionId == null ? "" : paymentSessionId.trim();
        environment = environment == null ? "" : environment.trim().toLowerCase(Locale.ROOT);

        if (!orderId.matches("[A-Za-z0-9_-]{1,128}")
                || !paymentSessionId.matches("[A-Za-z0-9_-]{8,512}")) {
            Toast.makeText(this, "Secure payment details are incomplete. Please retry checkout.", Toast.LENGTH_LONG).show();
            return;
        }

        final CFSession.Environment cfEnvironment;
        if ("production".equals(environment) || "prod".equals(environment)) {
            cfEnvironment = CFSession.Environment.PRODUCTION;
        } else if ("sandbox".equals(environment)) {
            cfEnvironment = CFSession.Environment.SANDBOX;
        } else {
            Toast.makeText(this, "Payment environment could not be verified.", Toast.LENGTH_LONG).show();
            return;
        }

        try {
            registerCashfreeCallback();
            CFSession cfSession = new CFSession.CFSessionBuilder()
                    .setEnvironment(cfEnvironment)
                    .setPaymentSessionID(paymentSessionId)
                    .setOrderId(orderId)
                    .build();
            CFWebCheckoutTheme theme = new CFWebCheckoutTheme.CFWebCheckoutThemeBuilder()
                    .setNavigationBarBackgroundColor("#7c3aed")
                    .setNavigationBarTextColor("#ffffff")
                    .build();
            CFWebCheckoutPayment checkout = new CFWebCheckoutPayment.CFWebCheckoutPaymentBuilder()
                    .setSession(cfSession)
                    .setCFWebCheckoutUITheme(theme)
                    .build();

            // Keep only the server-issued order reference in memory. Never log or
            // persist the payment session, and never trust an SDK callback as proof of payment.
            pendingCashfreeOrderId = orderId;
            cashfreeCheckoutActive = true;
            CFPaymentGatewayService.getInstance().doPayment(this, checkout);
        } catch (CFException exception) {
            pendingCashfreeOrderId = null;
            cashfreeCheckoutActive = false;
            Log.e("SivdhiCashfree", "Unable to start Cashfree checkout.");
            Toast.makeText(this, "Unable to open secure payment. Please retry from Sivdhi.", Toast.LENGTH_LONG).show();
        }
    }

    @Override
    public void onPaymentVerify(String orderId) {
        finishCashfreeCheckoutFromCallback(orderId, false, null);
    }

    @Override
    public void onPaymentFailure(CFErrorResponse errorResponse, String orderId) {
        // Cashfree supplies the actionable failure reason here. Keep it local to
        // this callback and show a bounded message so checkout failures are diagnosable.
        String failureReason = errorResponse == null ? null : errorResponse.getMessage();
        finishCashfreeCheckoutFromCallback(orderId, true, failureReason);
    }

    private void finishCashfreeCheckoutFromCallback(
            String callbackOrderId, boolean failedCallback, String failureReason) {
        runOnUiThread(() -> {
            String expectedOrderId = pendingCashfreeOrderId;
            if (expectedOrderId == null || callbackOrderId == null
                    || !expectedOrderId.equals(callbackOrderId)) {
                Log.w("SivdhiCashfree", "Ignored an unmatched checkout callback.");
                return;
            }

            pendingCashfreeOrderId = null;
            cashfreeCheckoutActive = false;
            if (failedCallback) {
                String reason = failureReason == null ? "" : failureReason.trim();
                if (reason.length() > 110) reason = reason.substring(0, 110);
                Log.w("SivdhiCashfree", "Cashfree checkout failure callback received: "
                        + (reason.isEmpty() ? "No reason supplied." : reason));
                String failureText = reason.isEmpty()
                        ? "Cashfree checkout could not start. No payment is confirmed."
                        : "Cashfree checkout failed: " + reason;
                Toast.makeText(this, failureText, Toast.LENGTH_LONG).show();
            } else {
                Toast.makeText(this, "Checking payment with Sivdhi…", Toast.LENGTH_SHORT).show();
            }

            if (hasInternetNetwork() && webView != null) {
                lastKnownUrl = cashfreeReturnUrl(expectedOrderId);
                webView.loadUrl(lastKnownUrl);
            } else {
                deferredCashfreeReturnOrderId = expectedOrderId;
                networkAvailable = false;
                showOfflinePanel(false);
            }
        });
    }

    private String cashfreeReturnUrl(String orderId) {
        return Uri.parse("https://" + TRUSTED_HOST + "/customer/cashfree/return")
                .buildUpon()
                .appendQueryParameter("order_id", orderId)
                .build()
                .toString();
    }

    private boolean isTrustedUrl(String url) {
        Uri uri = Uri.parse(url);
        return "https".equalsIgnoreCase(uri.getScheme()) &&
                TRUSTED_HOST.equalsIgnoreCase(uri.getHost());
    }

    private boolean hasLocationPermission() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M ||
                checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
                checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    private void handleWebPermissionRequest(PermissionRequest request) {
        List<String> needed = new ArrayList<>();
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                needed.add(Manifest.permission.CAMERA);
            } else if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                needed.add(Manifest.permission.RECORD_AUDIO);
            } else {
                request.deny();
                return;
            }
        }
        boolean allGranted = true;
        for (String permission : needed) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M &&
                    checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
                allGranted = false;
            }
        }
        if (allGranted) {
            request.grant(request.getResources());
        } else {
            pendingMediaRequest = request;
            requestPermissions(needed.toArray(new String[0]), MEDIA_PERMISSION_REQUEST);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == GEO_PERMISSION_REQUEST && pendingGeoCallback != null) {
            boolean granted = hasLocationPermission();
            pendingGeoCallback.invoke(pendingGeoOrigin, granted, false);
            pendingGeoCallback = null;
            pendingGeoOrigin = null;
        } else if (requestCode == MEDIA_PERMISSION_REQUEST && pendingMediaRequest != null) {
            boolean granted = true;
            for (int result : grantResults) {
                if (result != PackageManager.PERMISSION_GRANTED) granted = false;
            }
            if (granted) pendingMediaRequest.grant(pendingMediaRequest.getResources());
            else pendingMediaRequest.deny();
            pendingMediaRequest = null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQUEST && fileCallback != null) {
            Uri[] result = null;
            if (resultCode == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    int count = data.getClipData().getItemCount();
                    result = new Uri[count];
                    for (int i = 0; i < count; i++) result[i] = data.getClipData().getItemAt(i).getUri();
                } else if (data.getData() != null) {
                    result = new Uri[]{data.getData()};
                }
            }
            fileCallback.onReceiveValue(result);
            fileCallback = null;
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        outState.putString("sivdhi.cashfree.pending_order", pendingCashfreeOrderId);
        outState.putString("sivdhi.cashfree.deferred_order", deferredCashfreeReturnOrderId);
        outState.putBoolean("sivdhi.cashfree.active", cashfreeCheckoutActive);
        Bundle webState = new Bundle();
        webView.saveState(webState);
        outState.putBundle("sivdhi.webview.state", webState);
        super.onSaveInstanceState(outState);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (networkCallbackRegistered && connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (RuntimeException ignored) {
                // Callback may already have been removed in onStop.
            }
            networkCallbackRegistered = false;
        }
        if (fileCallback != null) fileCallback.onReceiveValue(null);
        if (pendingGeoCallback != null) pendingGeoCallback.invoke(pendingGeoOrigin, false, false);
        if (pendingMediaRequest != null) pendingMediaRequest.deny();
        if (webView != null) {
            webView.stopLoading();
            webView.setWebChromeClient(null);
            webView.setWebViewClient(null);
            webView.destroy();
        }
        super.onDestroy();
    }
}
