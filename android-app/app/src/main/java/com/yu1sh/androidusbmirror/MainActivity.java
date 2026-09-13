package com.yu1sh.androidusbmirror;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.hardware.usb.UsbAccessory;
import android.hardware.usb.UsbManager;
import android.media.projection.MediaProjectionManager;
import android.media.projection.MediaProjectionConfig;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** The small setup screen for the USB accessory and user-approved projection. */
public final class MainActivity extends Activity {
    private static final int REQUEST_MEDIA_PROJECTION = 1001;
    private static final int REQUEST_USB_PERMISSION = 1002;
    private static final int REQUEST_NOTIFICATIONS = 1003;

    private String usbPermissionAction;
    private UsbManager usbManager;
    private MediaProjectionManager projectionManager;
    private UsbAccessory accessory;
    private TextView statusView;
    private Button startButton;
    private Button stopButton;

    private final BroadcastReceiver usbReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            if (UsbManager.ACTION_USB_ACCESSORY_ATTACHED.equals(action)
                    || UsbManager.ACTION_USB_ACCESSORY_DETACHED.equals(action)) {
                refreshAccessory(readAccessory(intent));
            }
        }
    };

    private final BroadcastReceiver usbPermissionReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            boolean granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false);
            if (granted) {
                setStatus("USB accessory permission granted. Tap Start sharing.");
            } else {
                setStatus("USB accessory permission was denied.");
            }
            refreshAccessory(null);
        }
    };

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String message = intent.getStringExtra(ScreenCaptureService.EXTRA_STATUS_MESSAGE);
            if (message != null) {
                setStatus(message);
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        usbManager = (UsbManager) getSystemService(Context.USB_SERVICE);
        projectionManager = (MediaProjectionManager)
                getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        usbPermissionAction = getPackageName() + ".USB_PERMISSION";
        setContentView(createContentView());
        registerReceivers();
        refreshAccessory(readAccessory(getIntent()));

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    REQUEST_NOTIFICATIONS);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        refreshAccessory(readAccessory(intent));
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshAccessory(null);
    }

    @Override
    protected void onDestroy() {
        unregisterReceiver(usbReceiver);
        unregisterReceiver(usbPermissionReceiver);
        unregisterReceiver(statusReceiver);
        super.onDestroy();
    }

    private View createContentView() {
        int padding = dp(24);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(padding, padding, padding, padding);

        TextView title = new TextView(this);
        title.setText(getString(com.yu1sh.androidusbmirror.R.string.app_name));
        title.setTextSize(24);
        title.setGravity(Gravity.CENTER_HORIZONTAL);
        content.addView(title, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView description = new TextView(this);
        description.setText(R.string.description);
        description.setTextSize(16);
        description.setPadding(0, dp(12), 0, dp(12));
        content.addView(description, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView instructions = new TextView(this);
        instructions.setText(R.string.instructions);
        instructions.setTextSize(14);
        content.addView(instructions, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        statusView = new TextView(this);
        statusView.setTextSize(15);
        statusView.setTextIsSelectable(true);
        statusView.setPadding(0, dp(20), 0, dp(20));
        content.addView(statusView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        startButton = new Button(this);
        startButton.setText(R.string.start_sharing);
        startButton.setOnClickListener(view -> startSharing());
        content.addView(startButton, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        stopButton = new Button(this);
        stopButton.setText(getString(com.yu1sh.androidusbmirror.R.string.stop));
        stopButton.setOnClickListener(view -> stopSharing());
        content.addView(stopButton, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        ScrollView scrollView = new ScrollView(this);
        scrollView.addView(content);
        return scrollView;
    }

    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    private void registerReceivers() {
        IntentFilter usbFilter = new IntentFilter();
        usbFilter.addAction(UsbManager.ACTION_USB_ACCESSORY_ATTACHED);
        usbFilter.addAction(UsbManager.ACTION_USB_ACCESSORY_DETACHED);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(usbReceiver, usbFilter, Context.RECEIVER_EXPORTED);
        } else {
            registerReceiver(usbReceiver, usbFilter);
        }

        IntentFilter permissionFilter = new IntentFilter(usbPermissionAction);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(usbPermissionReceiver, permissionFilter, Context.RECEIVER_NOT_EXPORTED);
            registerReceiver(statusReceiver,
                    new IntentFilter(ScreenCaptureService.ACTION_STATUS),
                    Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(usbPermissionReceiver, permissionFilter);
            registerReceiver(statusReceiver, new IntentFilter(ScreenCaptureService.ACTION_STATUS));
        }
    }

    private void refreshAccessory(UsbAccessory fromIntent) {
        UsbAccessory found = fromIntent;
        UsbAccessory[] accessories = usbManager == null ? null : usbManager.getAccessoryList();
        if (accessories != null) {
            for (UsbAccessory candidate : accessories) {
                if (AccessoryIdentity.matches(candidate)) {
                    found = candidate;
                    break;
                }
            }
            if (found == null && accessories.length > 0) {
                found = accessories[0];
            }
        }
        accessory = found;
        if (accessory == null) {
            setStatus("No supported USB accessory is connected. Connect the PC after it enters AOA mode.");
            setButtons(false, false);
            return;
        }
        if (!AccessoryIdentity.matches(accessory)) {
            setStatus("Unsupported USB accessory: " + AccessoryIdentity.describe(accessory));
            setButtons(false, false);
            return;
        }
        if (!usbManager.hasPermission(accessory)) {
            setStatus("USB accessory found. Tap Start sharing to grant USB permission.");
            setButtons(true, false);
        } else {
            setStatus("USB accessory ready: " + AccessoryIdentity.describe(accessory));
            setButtons(true, true);
        }
    }

    private void startSharing() {
        if (accessory == null || !AccessoryIdentity.matches(accessory)) {
            refreshAccessory(null);
            return;
        }
        if (!usbManager.hasPermission(accessory)) {
            requestAccessoryPermission();
            return;
        }
        if (projectionManager == null) {
            setStatus("MediaProjection is not available on this device.");
            return;
        }
        setStatus("Waiting for Android screen-capture consent...");
        Intent captureIntent;
        if (Build.VERSION.SDK_INT >= 34) {
            // This app mirrors the complete device display. Android 14's
            // default picker also permits app-window sharing, which would
            // produce a different stream and size than the desktop expects.
            captureIntent = projectionManager.createScreenCaptureIntent(
                    MediaProjectionConfig.createConfigForDefaultDisplay());
        } else {
            captureIntent = projectionManager.createScreenCaptureIntent();
        }
        startActivityForResult(captureIntent, REQUEST_MEDIA_PROJECTION);
    }

    private void requestAccessoryPermission() {
        Intent permissionIntent = new Intent(usbPermissionAction)
                .setPackage(getPackageName());
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pendingIntent = PendingIntent.getBroadcast(
                this, REQUEST_USB_PERMISSION, permissionIntent, flags);
        usbManager.requestPermission(accessory, pendingIntent);
        setStatus("Approve the USB accessory permission dialog, then tap Start sharing.");
    }

    private void stopSharing() {
        stopService(new Intent(this, ScreenCaptureService.class)
                .setAction(ScreenCaptureService.ACTION_STOP));
        setStatus("Stopping screen sharing...");
    }

    @Override
    @SuppressWarnings("deprecation")
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_MEDIA_PROJECTION) {
            return;
        }
        if (resultCode != RESULT_OK || data == null) {
            setStatus("Screen-capture consent was cancelled.");
            return;
        }
        Intent serviceIntent = new Intent(this, ScreenCaptureService.class)
                .setAction(ScreenCaptureService.ACTION_START)
                .putExtra(ScreenCaptureService.EXTRA_RESULT_CODE, resultCode)
                .putExtra(ScreenCaptureService.EXTRA_RESULT_DATA, data)
                .putExtra(UsbManager.EXTRA_ACCESSORY, accessory);
        startForegroundService(serviceIntent);
        setButtons(false, true);
        setStatus("Starting screen sharing...");
    }

    private UsbAccessory readAccessory(Intent intent) {
        if (intent == null) {
            return null;
        }
        if (Build.VERSION.SDK_INT >= 33) {
            return intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY, UsbAccessory.class);
        }
        //noinspection deprecation
        return (UsbAccessory) intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY);
    }

    private void setStatus(String message) {
        if (statusView != null) {
            statusView.setText(message);
        }
    }

    private void setButtons(boolean canStart, boolean canStop) {
        if (startButton != null) {
            startButton.setEnabled(canStart);
        }
        if (stopButton != null) {
            stopButton.setEnabled(canStop);
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
