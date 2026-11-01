package com.sivdhi.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import java.util.Map;

/** Receives FCM data messages even when the WebView activity is not visible. */
public class SivdhiFirebaseMessagingService extends FirebaseMessagingService {
    private static final String CHANNEL_ID = "sivdhi_updates";
    private static final String PREFS = "sivdhi_fcm";

    @Override
    public void onNewToken(String token) {
        super.onNewToken(token);
        getSharedPreferences(PREFS, MODE_PRIVATE)
                .edit()
                .putString("latest_token", token)
                .apply();
        // MainActivity re-registers the current token against the signed-in
        // Sivdhi session the next time a trusted website page is loaded.
    }

    @Override
    public void onMessageReceived(RemoteMessage remoteMessage) {
        super.onMessageReceived(remoteMessage);
        Map<String, String> data = remoteMessage.getData();
        String title = clean(data.get("title"), "Sivdhi update");
        String body = clean(data.get("body"),
                "You have a new notification. Open Sivdhi to view it.");
        String route = safeRoute(data.get("route"));
        showNotification(title, body, route);
    }

    private String clean(String value, String fallback) {
        if (value == null || value.trim().isEmpty()) return fallback;
        return value.trim().length() > 180 ? value.trim().substring(0, 180) : value.trim();
    }

    private String safeRoute(String value) {
        if (value == null || !value.startsWith("/") || value.startsWith("//") ||
                value.contains("..") || value.contains("\\")) {
            return "/";
        }
        return value;
    }

    private void showNotification(String title, String body, String route) {
        if (Build.VERSION.SDK_INT >= 33 &&
                checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
            return;
        }

        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager == null) return;

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "Sivdhi updates",
                    NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("Booking and account updates from Sivdhi");
            manager.createNotificationChannel(channel);
        }

        Intent openApp = new Intent(this, MainActivity.class);
        openApp.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        openApp.putExtra("sivdhi_notification_route", route);
        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                (int) (System.currentTimeMillis() & 0x7fffffff),
                openApp,
                pendingFlags
        );

        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
            builder.setPriority(Notification.PRIORITY_HIGH);
        }
        builder.setSmallIcon(R.drawable.ic_stat_sivdhi)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(pendingIntent)
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_MESSAGE)
                .setDefaults(Notification.DEFAULT_ALL);

        manager.notify((int) (System.currentTimeMillis() & 0x7fffffff), builder.build());
    }
}
