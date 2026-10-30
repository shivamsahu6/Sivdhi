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
import android.net.Uri;
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
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

public class MainActivity extends Activity {
    private static final String TRUSTED_HOST = "sivdhi.duckdns.org";
    private static final int FILE_CHOOSER_REQUEST = 4101;
    private static final int GEO_PERMISSION_REQUEST = 4102;
    private static final int MEDIA_PERMISSION_REQUEST = 4103;

    private WebView webView;
    private ProgressBar progressBar;
    private FrameLayout root;
    private ValueCallback<Uri[]> fileCallback;
    private GeolocationPermissions.Callback pendingGeoCallback;
    private String pendingGeoOrigin;
    private PermissionRequest pendingMediaRequest;
    private String startUrl;
    private int themeColor = Color.rgb(124, 58, 237);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        readAppConfiguration();
        configureSystemBars();
        buildLayout();
        configureWebView();

        if (savedInstanceState != null) {
            Bundle webState = savedInstanceState.getBundle("sivdhi.webview.state");
            if (webState != null && webView.restoreState(webState) != null) {
                return;
            }
        }
        webView.loadUrl(startUrl);
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

        setContentView(root);
    }

    @SuppressWarnings("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings settings = webView.getSettings();
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
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                progressBar.setProgress(100);
                progressBar.setVisibility(View.GONE);
                CookieManager.getInstance().flush();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    progressBar.setVisibility(View.GONE);
                    Toast.makeText(MainActivity.this,
                            "Connection problem. Please check your internet and retry.",
                            Toast.LENGTH_LONG).show();
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

    private boolean routeUrl(String rawUrl) {
        Uri uri = Uri.parse(rawUrl);
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
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
