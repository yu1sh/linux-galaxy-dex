package com.yu1sh.androidusbmirror;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.hardware.usb.UsbAccessory;
import android.hardware.usb.UsbManager;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.ParcelFileDescriptor;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Display;
import android.view.Surface;
import android.view.WindowManager;
import android.view.WindowMetrics;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.util.ArrayDeque;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Owns one user-approved MediaProjection session and its USB AOA stream.
 *
 * <p>The service is deliberately USB-only: it has no INTERNET permission and
 * only accepts the expected Android Open Accessory identity before waiting for
 * a GUSB/1 HELLO frame from the physical desktop host.
 */
public final class ScreenCaptureService extends Service {
    public static final String ACTION_START =
            "com.yu1sh.androidusbmirror.action.START";
    public static final String ACTION_STOP =
            "com.yu1sh.androidusbmirror.action.STOP";
    public static final String ACTION_STATUS =
            "com.yu1sh.androidusbmirror.action.STATUS";
    public static final String EXTRA_RESULT_CODE =
            "com.yu1sh.androidusbmirror.extra.RESULT_CODE";
    public static final String EXTRA_RESULT_DATA =
            "com.yu1sh.androidusbmirror.extra.RESULT_DATA";
    public static final String EXTRA_STATUS_MESSAGE =
            "com.yu1sh.androidusbmirror.extra.STATUS_MESSAGE";

    private static final int NOTIFICATION_ID = 7001;
    private static final String NOTIFICATION_CHANNEL_ID = "screen_projection";
    private static final int TARGET_FPS = 60;
    private static final int MAX_CAPTURE_DIMENSION = 1920;
    private static final int VIDEO_BIT_RATE = 8_000_000;
    private static final String TAG = "AndroidUsbMirror";
    private static final int MAX_QUEUED_ACCESS_UNITS = 1;
    private static final long TELEMETRY_INTERVAL_NS = 1_000_000_000L;

    private final AtomicBoolean stopRequested = new AtomicBoolean();
    private final AtomicBoolean syncFrameRequested = new AtomicBoolean();
    private final Object outputLock = new Object();
    private final Object cleanupLock = new Object();
    private final Object videoQueueLock = new Object();
    private final ArrayDeque<EncodedAccessUnit> videoQueue = new ArrayDeque<>();
    private final Object telemetryLock = new Object();

    // The video queue is intentionally bounded to one complete access unit.
    // A slow AOA host can therefore cause a frame drop, but can never make old
    // encoded frames accumulate and increase interactive latency indefinitely.
    private boolean videoWriterActive;
    private volatile Thread videoWriterThread;

    // These counters are only diagnostic and never cross the GUSB wire. They
    // are emitted at most once per second under the AndroidUsbMirror tag.
    private long telemetryWindowStartNs;
    private long telemetryEncodedAccessUnits;
    private long telemetryEncodedBytes;
    private long telemetryPtsSamples;
    private long telemetryPtsFirstUs = -1L;
    private long telemetryPtsLastUs = -1L;
    private long telemetryWrittenAccessUnits;
    private long telemetryWrittenBytes;
    private long telemetryWriteNanos;
    private long telemetryMaxChunkWriteNanos;
    private long telemetryDroppedAccessUnits;

    private volatile Thread sessionThread;
    private volatile Thread controlThread;
    private volatile ParcelFileDescriptor fileDescriptor;
    private volatile InputStream input;
    private volatile OutputStream output;
    private volatile MediaCodec codec;
    private volatile Surface codecSurface;
    private volatile VirtualDisplay virtualDisplay;
    private volatile MediaProjection mediaProjection;
    private volatile HandlerThread displayThread;
    private volatile Handler displayHandler;
    private volatile DisplayManager displayManager;
    private volatile CaptureSize pendingSize;
    private volatile boolean stopFrameSent;
    private MediaProjection.Callback projectionCallback;
    private DisplayManager.DisplayListener displayListener;
    private UsbAccessory requestedAccessory;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            requestStop();
            return START_NOT_STICKY;
        }
        if (!ACTION_START.equals(action)) {
            return START_NOT_STICKY;
        }
        if (sessionThread != null && sessionThread.isAlive()) {
            return START_NOT_STICKY;
        }

        int resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED);
        Intent resultData = readResultData(intent);
        requestedAccessory = readAccessory(intent);
        if (resultCode != Activity.RESULT_OK || resultData == null) {
            publishStatus("Screen-capture consent data is missing.");
            stopSelf(startId);
            return START_NOT_STICKY;
        }

        stopRequested.set(false);
        stopFrameSent = false;
        resetTelemetry();
        startForegroundWithProjectionType(buildNotification("Preparing USB screen sharing"));
        sessionThread = new Thread(
                () -> runSession(resultCode, resultData), "AndroidUsbMirror-session");
        sessionThread.start();
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        requestStop();
        cleanupSession();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void runSession(int resultCode, Intent resultData) {
        Protocol.Reader reader = null;
        try {
            publishStatus("Waiting for the USB accessory...");
            UsbAccessory accessory = findAccessory();
            if (accessory == null) {
                throw new IOException("expected USB accessory is not connected");
            }
            UsbManager usbManager = (UsbManager) getSystemService(Context.USB_SERVICE);
            if (usbManager == null || !usbManager.hasPermission(accessory)) {
                throw new IOException("USB accessory permission was not granted");
            }

            fileDescriptor = usbManager.openAccessory(accessory);
            if (fileDescriptor == null) {
                throw new IOException("UsbManager.openAccessory returned null");
            }
            input = new FileInputStream(fileDescriptor.getFileDescriptor());
            output = new FileOutputStream(fileDescriptor.getFileDescriptor());
            reader = new Protocol.Reader(input);
            publishStatus("Waiting for the PC HELLO frame...");

            Protocol.Frame hello = reader.read();
            if (hello.type != Protocol.TYPE_HELLO) {
                sendError("expected GUSB HELLO");
                throw new Protocol.ProtocolException("first frame is not HELLO");
            }
            if (stopRequested.get()) {
                return;
            }

            startVideoWriter();
            initializeProjection(resultCode, resultData);
            CaptureSize size = currentCaptureSize();
            sendInfo(size);
            Protocol.Reader activeReader = reader;
            controlThread = new Thread(() -> readControlFrames(activeReader),
                    "AndroidUsbMirror-control");
            controlThread.start();
            publishStatus("Sharing " + size.width + "x" + size.height + " over USB");
            encodeLoop(size);
        } catch (Throwable error) {
            if (!stopRequested.get()) {
                String message = error.getMessage();
                if (message == null || message.isEmpty()) {
                    message = error.getClass().getSimpleName();
                }
                sendError(message);
                publishStatus("Screen sharing stopped: " + message);
            }
        } finally {
            sendStopFrame();
            cleanupSession();
            stopForeground(true);
            stopSelf();
        }
    }

    private UsbAccessory findAccessory() throws InterruptedException {
        UsbManager usbManager = (UsbManager) getSystemService(Context.USB_SERVICE);
        if (usbManager == null) {
            return null;
        }
        for (int attempt = 0; attempt < 50 && !stopRequested.get(); attempt++) {
            UsbAccessory[] accessories = usbManager.getAccessoryList();
            if (accessories != null) {
                if (requestedAccessory != null
                        && AccessoryIdentity.matches(requestedAccessory)
                        && usbManager.hasPermission(requestedAccessory)) {
                    return requestedAccessory;
                }
                for (UsbAccessory accessory : accessories) {
                    if (AccessoryIdentity.matches(accessory)
                            && usbManager.hasPermission(accessory)) {
                        return accessory;
                    }
                }
            }
            Thread.sleep(100);
        }
        return null;
    }

    private void initializeProjection(int resultCode, Intent resultData) throws IOException {
        MediaProjectionManager manager = (MediaProjectionManager)
                getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        if (manager == null) {
            throw new IOException("MediaProjectionManager is unavailable");
        }
        mediaProjection = manager.getMediaProjection(resultCode, resultData);
        if (mediaProjection == null) {
            throw new IOException("MediaProjection permission was not accepted");
        }

        displayThread = new HandlerThread("AndroidUsbMirror-display");
        displayThread.start();
        displayHandler = new Handler(displayThread.getLooper());
        projectionCallback = new MediaProjection.Callback() {
            @Override
            public void onStop() {
                publishStatus("Android stopped the screen-capture session.");
                requestStop();
            }

            @Override
            public void onCapturedContentResize(int width, int height) {
                if (width > 0 && height > 0) {
                    int density = getResources().getConfiguration().densityDpi;
                    pendingSize = captureSizeForBounds(width, height, density);
                }
            }
        };
        mediaProjection.registerCallback(projectionCallback, displayHandler);

        CaptureSize size = currentCaptureSize();
        codec = createHardwareEncoder(size);
        codecSurface = codec.createInputSurface();
        codec.start();
        virtualDisplay = mediaProjection.createVirtualDisplay(
                "Android USB Mirror",
                size.width,
                size.height,
                size.densityDpi,
                android.hardware.display.DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                codecSurface,
                new VirtualDisplay.Callback() {
                    @Override
                    public void onStopped() {
                        requestStop();
                    }
                },
                displayHandler);
        if (virtualDisplay == null) {
            throw new IOException("MediaProjection could not create a virtual display");
        }

        displayManager = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
        if (displayManager != null) {
            displayListener = new DisplayManager.DisplayListener() {
                @Override
                public void onDisplayAdded(int displayId) {
                }

                @Override
                public void onDisplayRemoved(int displayId) {
                }

                @Override
                public void onDisplayChanged(int displayId) {
                    if (displayId == Display.DEFAULT_DISPLAY) {
                        pendingSize = currentCaptureSize();
                    }
                }
            };
            displayManager.registerDisplayListener(displayListener, displayHandler);
        }
    }

    private void encodeLoop(CaptureSize size) throws IOException {
        MediaCodec.BufferInfo bufferInfo = new MediaCodec.BufferInfo();
        CaptureSize currentSize = size;
        while (!stopRequested.get()) {
            CaptureSize changedSize = pendingSize;
            if (changedSize != null && !changedSize.equals(currentSize)) {
                pendingSize = null;
                reconfigureCapture(changedSize);
                currentSize = changedSize;
                sendInfo(currentSize);
            }

            MediaCodec activeCodec = codec;
            if (activeCodec == null) {
                throw new IOException("video encoder is unavailable");
            }
            int outputIndex;
            try {
                outputIndex = activeCodec.dequeueOutputBuffer(bufferInfo, 10_000);
            } catch (IllegalStateException error) {
                if (stopRequested.get()) {
                    return;
                }
                throw error;
            }
            if (outputIndex == MediaCodec.INFO_TRY_AGAIN_LATER) {
                continue;
            }
            if (outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                sendCodecConfig(activeCodec.getOutputFormat());
                continue;
            }
            if (outputIndex < 0) {
                continue;
            }
            try {
                ByteBuffer buffer = activeCodec.getOutputBuffer(outputIndex);
                if (buffer != null && bufferInfo.size > 0) {
                    int start = Math.max(0, bufferInfo.offset);
                    int end = Math.min(buffer.capacity(), start + bufferInfo.size);
                    if (end > start) {
                        ByteBuffer duplicate = buffer.duplicate();
                        duplicate.position(start);
                        duplicate.limit(end);
                        byte[] encoded = new byte[end - start];
                        duplicate.get(encoded);
                        sendVideoAccessUnit(encoded, bufferInfo.presentationTimeUs,
                                (bufferInfo.flags & MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0,
                                (bufferInfo.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0);
                    }
                }
            } finally {
                activeCodec.releaseOutputBuffer(outputIndex, false);
            }
            if ((bufferInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                return;
            }
        }
    }

    private void readControlFrames(Protocol.Reader reader) {
        try {
            while (!stopRequested.get()) {
                Protocol.Frame frame = reader.read();
                if (frame.type == Protocol.TYPE_STOP) {
                    publishStatus("Stop requested by the PC.");
                    requestStop();
                    return;
                }
                if (frame.type == Protocol.TYPE_ERROR) {
                    requestStop();
                    return;
                }
                if (frame.type != Protocol.TYPE_HELLO) {
                    sendError("unexpected control frame: " + frame.type);
                    requestStop();
                    return;
                }
                // HELLO may be repeated by a reconnecting desktop host. It is
                // harmless after the first frame and keeps the control path
                // forward-compatible.
            }
        } catch (IOException error) {
            if (!stopRequested.get()) {
                publishStatus("USB accessory disconnected.");
                requestStop();
            }
        }
    }

    private void reconfigureCapture(CaptureSize newSize) throws IOException {
        MediaCodec oldCodec = codec;
        Surface oldSurface = codecSurface;
        MediaCodec newCodec = createHardwareEncoder(newSize);
        Surface newSurface = newCodec.createInputSurface();
        try {
            newCodec.start();
            if (virtualDisplay == null) {
                throw new IOException("virtual display is unavailable");
            }
            // Android 14+ requires resizing the existing virtual display and
            // replacing its surface for orientation/size changes.
            virtualDisplay.resize(newSize.width, newSize.height, newSize.densityDpi);
            virtualDisplay.setSurface(newSurface);
            codec = newCodec;
            codecSurface = newSurface;
            releaseCodec(oldCodec, oldSurface);
        } catch (Throwable error) {
            releaseCodec(newCodec, newSurface);
            if (error instanceof IOException) {
                throw (IOException) error;
            }
            throw new IOException("could not resize the virtual display", error);
        }
    }

    private MediaCodec createHardwareEncoder(CaptureSize size) throws IOException {
        MediaCodecInfo selected = null;
        MediaCodecList codecList = new MediaCodecList(MediaCodecList.REGULAR_CODECS);
        for (MediaCodecInfo info : codecList.getCodecInfos()) {
            if (!info.isEncoder() || info.isSoftwareOnly()) {
                continue;
            }
            boolean supportsAvc = false;
            for (String type : info.getSupportedTypes()) {
                if (MediaFormat.MIMETYPE_VIDEO_AVC.equalsIgnoreCase(type)) {
                    supportsAvc = true;
                    break;
                }
            }
            if (!supportsAvc) {
                continue;
            }
            try {
                MediaCodecInfo.CodecCapabilities capabilities =
                        info.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC);
                for (int colorFormat : capabilities.colorFormats) {
                    if (colorFormat == MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface) {
                        selected = info;
                        break;
                    }
                }
            } catch (IllegalArgumentException ignored) {
                // Try the next advertised encoder.
            }
            if (selected != null) {
                break;
            }
        }
        if (selected == null) {
            throw new IOException("no hardware H.264 surface encoder is available");
        }

        MediaFormat format = MediaFormat.createVideoFormat(
                MediaFormat.MIMETYPE_VIDEO_AVC, size.width, size.height);
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        format.setInteger(MediaFormat.KEY_BIT_RATE, VIDEO_BIT_RATE);
        format.setInteger(MediaFormat.KEY_FRAME_RATE, TARGET_FPS);
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
        // These optional encoder hints are defined for real-time video
        // sharing: one frame of codec latency and no reordering B-frames.
        format.setInteger(MediaFormat.KEY_LATENCY, 1);
        format.setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0);
        // Priority 0 asks the codec for realtime treatment when supported.
        format.setInteger(MediaFormat.KEY_PRIORITY, 0);

        MediaCodec encoder = null;
        try {
            encoder = MediaCodec.createByCodecName(selected.getName());
            encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            return encoder;
        } catch (Exception error) {
            if (encoder != null) {
                try {
                    encoder.release();
                } catch (Exception ignored) {
                }
            }
            throw new IOException("could not start hardware H.264 encoder "
                    + selected.getName(), error);
        }
    }

    private void sendInfo(CaptureSize size) throws IOException {
        // INFO must precede any frames from the new capture configuration.
        // Discard a queued old frame and wait for an in-flight AU to finish so
        // a resize cannot reorder stale video after its new dimensions.
        discardQueuedVideo();
        awaitVideoWriterIdle();
        if (stopRequested.get()) {
            return;
        }
        JSONObject info = new JSONObject();
        try {
            info.put("width", size.width);
            info.put("height", size.height);
            info.put("fps", TARGET_FPS);
            info.put("codec", "h264");
            info.put("format", "annexb");
            info.put("protocol", "GUSB/1");
        } catch (JSONException error) {
            throw new IOException("could not build INFO payload", error);
        }
        synchronized (outputLock) {
            Protocol.writeFrame(output, Protocol.TYPE_INFO, 0,
                    Protocol.utf8(info.toString()));
        }
    }

    private void sendCodecConfig(MediaFormat format) throws IOException {
        sendCodecConfigBuffer(format, "csd-0");
        sendCodecConfigBuffer(format, "csd-1");
    }

    private void sendCodecConfigBuffer(MediaFormat format, String key) throws IOException {
        ByteBuffer csd = format.getByteBuffer(key);
        if (csd == null || !csd.hasRemaining()) {
            return;
        }
        ByteBuffer duplicate = csd.duplicate();
        byte[] data = new byte[duplicate.remaining()];
        duplicate.get(data);
        // Codec configuration is a stream barrier. Write it after any old
        // queued AU has drained so a new decoder never sees video first.
        discardQueuedVideo();
        awaitVideoWriterIdle();
        if (!stopRequested.get()) {
            writeVideoAccessUnit(new EncodedAccessUnit(
                    toAnnexB(data), 0L, false, true));
        }
    }

    private void sendVideoAccessUnit(byte[] encoded, long presentationTimeUs,
                                     boolean keyFrame, boolean codecConfig) throws IOException {
        byte[] annexB = toAnnexB(encoded);
        if (annexB.length == 0) {
            return;
        }
        if (!codecConfig) {
            recordEncodedAccessUnit(presentationTimeUs, annexB.length);
        }
        enqueueVideoAccessUnit(new EncodedAccessUnit(
                annexB, presentationTimeUs, keyFrame, codecConfig));
    }

    private void startVideoWriter() {
        synchronized (videoQueueLock) {
            Thread existing = videoWriterThread;
            if (existing != null && existing.isAlive()) {
                return;
            }
            Thread writer = new Thread(this::runVideoWriter,
                    "AndroidUsbMirror-video-writer");
            videoWriterThread = writer;
            writer.start();
        }
    }

    private void runVideoWriter() {
        while (true) {
            EncodedAccessUnit accessUnit;
            synchronized (videoQueueLock) {
                while (videoQueue.isEmpty() && !stopRequested.get()) {
                    try {
                        videoQueueLock.wait();
                    } catch (InterruptedException ignored) {
                        if (stopRequested.get()) {
                            return;
                        }
                    }
                }
                if (stopRequested.get() || videoQueue.isEmpty()) {
                    return;
                }
                accessUnit = videoQueue.removeFirst();
                videoWriterActive = true;
            }

            try {
                writeVideoAccessUnit(accessUnit);
            } catch (IOException error) {
                if (!stopRequested.get()) {
                    Log.w(TAG, "AOA video write failed", error);
                    requestStop();
                }
                return;
            } finally {
                synchronized (videoQueueLock) {
                    videoWriterActive = false;
                    videoQueueLock.notifyAll();
                }
            }
        }
    }

    private void enqueueVideoAccessUnit(EncodedAccessUnit accessUnit) {
        if (stopRequested.get()) {
            return;
        }
        EncodedAccessUnit dropped = null;
        synchronized (videoQueueLock) {
            if (stopRequested.get()) {
                return;
            }
            if (videoQueue.size() >= MAX_QUEUED_ACCESS_UNITS) {
                // Preserve an unsent IDR/config AU.  Dropping it in favor of
                // a predictive frame leaves the desktop decoder unrecoverable
                // until a later sync request completes.
                EncodedAccessUnit oldest = videoQueue.peekFirst();
                if (oldest != null && oldest.keyFrame && !accessUnit.keyFrame) {
                    dropped = accessUnit;
                } else {
                    dropped = videoQueue.removeFirst();
                }
            }
            if (dropped != accessUnit) {
                videoQueue.addLast(accessUnit);
            }
            videoQueueLock.notifyAll();
        }
        if (dropped != null) {
            telemetryDroppedAccessUnit();
            // If an IDR was discarded before it reached the desktop, clear the
            // coalescing latch so the next drop can request another sync frame.
            if (dropped.keyFrame) {
                syncFrameRequested.set(false);
            }
            requestSyncFrame();
        }
    }

    private void writeVideoAccessUnit(EncodedAccessUnit accessUnit) throws IOException {
        long startedNs = System.nanoTime();
        long maxChunkNs = 0L;
        boolean completed = false;
        try {
            synchronized (outputLock) {
                OutputStream stream = output;
                if (stream == null) {
                    throw new IOException("USB output is closed");
                }
                int offset = 0;
                boolean first = true;
                while (offset < accessUnit.data.length) {
                    if (stopRequested.get()) {
                        throw new IOException("USB video writer stopped");
                    }
                    int length = Math.min(Protocol.MAX_PAYLOAD,
                            accessUnit.data.length - offset);
                    int frameFlags = 0;
                    if (first && accessUnit.keyFrame) {
                        frameFlags |= Protocol.FLAG_KEY_FRAME;
                    }
                    if (first && accessUnit.codecConfig) {
                        frameFlags |= Protocol.FLAG_CODEC_CONFIG;
                    }
                    if (offset + length == accessUnit.data.length) {
                        frameFlags |= Protocol.FLAG_END_OF_ACCESS_UNIT;
                    }
                    long chunkStartedNs = System.nanoTime();
                    Protocol.writeFrame(stream, Protocol.TYPE_VIDEO, frameFlags,
                            accessUnit.data, offset, length);
                    maxChunkNs = Math.max(maxChunkNs,
                            System.nanoTime() - chunkStartedNs);
                    first = false;
                    offset += length;
                }
                completed = true;
            }
        } finally {
            telemetryVideoWrite(accessUnit, System.nanoTime() - startedNs,
                    maxChunkNs, completed);
        }
        if (completed && accessUnit.keyFrame) {
            syncFrameRequested.set(false);
        }
    }

    private void discardQueuedVideo() {
        int dropped = 0;
        boolean droppedKeyFrame = false;
        synchronized (videoQueueLock) {
            while (!videoQueue.isEmpty()) {
                EncodedAccessUnit accessUnit = videoQueue.removeFirst();
                dropped++;
                droppedKeyFrame |= accessUnit.keyFrame;
            }
            videoQueueLock.notifyAll();
        }
        if (dropped != 0) {
            telemetryDroppedAccessUnits(dropped);
            if (droppedKeyFrame) {
                syncFrameRequested.set(false);
            }
        }
    }

    private void awaitVideoWriterIdle() throws IOException {
        synchronized (videoQueueLock) {
            while ((!videoQueue.isEmpty() || videoWriterActive)
                    && !stopRequested.get()) {
                try {
                    videoQueueLock.wait(50L);
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt();
                    throw new IOException("interrupted while waiting for USB video", error);
                }
            }
        }
    }

    private void requestSyncFrame() {
        if (!syncFrameRequested.compareAndSet(false, true)) {
            return;
        }
        MediaCodec activeCodec = codec;
        if (activeCodec == null) {
            syncFrameRequested.set(false);
            return;
        }
        Bundle parameters = new Bundle();
        parameters.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0);
        try {
            activeCodec.setParameters(parameters);
        } catch (RuntimeException error) {
            syncFrameRequested.set(false);
            Log.w(TAG, "hardware encoder did not accept sync-frame request", error);
        }
    }

    private void resetTelemetry() {
        synchronized (telemetryLock) {
            telemetryWindowStartNs = System.nanoTime();
            telemetryEncodedAccessUnits = 0L;
            telemetryEncodedBytes = 0L;
            telemetryPtsSamples = 0L;
            telemetryPtsFirstUs = -1L;
            telemetryPtsLastUs = -1L;
            telemetryWrittenAccessUnits = 0L;
            telemetryWrittenBytes = 0L;
            telemetryWriteNanos = 0L;
            telemetryMaxChunkWriteNanos = 0L;
            telemetryDroppedAccessUnits = 0L;
        }
        syncFrameRequested.set(false);
    }

    private void recordEncodedAccessUnit(long presentationTimeUs, int bytes) {
        synchronized (telemetryLock) {
            telemetryEncodedAccessUnits++;
            telemetryEncodedBytes += bytes;
            if (presentationTimeUs >= 0L) {
                if (telemetryPtsFirstUs < 0L) {
                    telemetryPtsFirstUs = presentationTimeUs;
                }
                if (telemetryPtsLastUs < 0L || presentationTimeUs >= telemetryPtsLastUs) {
                    telemetryPtsLastUs = presentationTimeUs;
                    telemetryPtsSamples++;
                }
            }
        }
        logTelemetryIfDue(false);
    }

    private void telemetryVideoWrite(EncodedAccessUnit accessUnit, long durationNs,
                                     long maxChunkNs, boolean completed) {
        synchronized (telemetryLock) {
            telemetryWrittenAccessUnits++;
            telemetryWriteNanos += durationNs;
            telemetryMaxChunkWriteNanos = Math.max(
                    telemetryMaxChunkWriteNanos, maxChunkNs);
            if (completed) {
                telemetryWrittenBytes += accessUnit.data.length;
            }
        }
        logTelemetryIfDue(false);
    }

    private void telemetryDroppedAccessUnit() {
        telemetryDroppedAccessUnits(1);
    }

    private void telemetryDroppedAccessUnits(int count) {
        synchronized (telemetryLock) {
            telemetryDroppedAccessUnits += count;
        }
        logTelemetryIfDue(false);
    }

    private void logTelemetryIfDue(boolean force) {
        String line = null;
        synchronized (telemetryLock) {
            long nowNs = System.nanoTime();
            if (telemetryWindowStartNs == 0L) {
                telemetryWindowStartNs = nowNs;
            }
            long elapsedNs = nowNs - telemetryWindowStartNs;
            if (!force && elapsedNs < TELEMETRY_INTERVAL_NS) {
                return;
            }
            if (telemetryEncodedAccessUnits == 0L
                    && telemetryWrittenAccessUnits == 0L
                    && telemetryDroppedAccessUnits == 0L) {
                telemetryWindowStartNs = nowNs;
                return;
            }
            double elapsedSeconds = Math.max(0.001,
                    elapsedNs / (double) TELEMETRY_INTERVAL_NS);
            double outputFps = telemetryEncodedAccessUnits / elapsedSeconds;
            double ptsFps = 0.0;
            if (telemetryPtsSamples > 1L && telemetryPtsLastUs > telemetryPtsFirstUs) {
                ptsFps = (telemetryPtsSamples - 1L) * 1_000_000.0
                        / (telemetryPtsLastUs - telemetryPtsFirstUs);
            }
            double averageWriteMs = telemetryWrittenAccessUnits == 0L
                    ? 0.0 : telemetryWriteNanos / 1_000_000.0
                    / telemetryWrittenAccessUnits;
            double maxChunkWriteMs = telemetryMaxChunkWriteNanos / 1_000_000.0;
            line = "videoStats outputFps=" + outputFps
                    + " ptsFps=" + ptsFps
                    + " encodedAUs=" + telemetryEncodedAccessUnits
                    + " encodedBytes=" + telemetryEncodedBytes
                    + " writtenAUs=" + telemetryWrittenAccessUnits
                    + " writtenBytes=" + telemetryWrittenBytes
                    + " avgAuWriteMs=" + averageWriteMs
                    + " maxChunkWriteMs=" + maxChunkWriteMs
                    + " droppedAUs=" + telemetryDroppedAccessUnits;
            telemetryWindowStartNs = nowNs;
            telemetryEncodedAccessUnits = 0L;
            telemetryEncodedBytes = 0L;
            telemetryPtsSamples = 0L;
            telemetryPtsFirstUs = -1L;
            telemetryPtsLastUs = -1L;
            telemetryWrittenAccessUnits = 0L;
            telemetryWrittenBytes = 0L;
            telemetryWriteNanos = 0L;
            telemetryMaxChunkWriteNanos = 0L;
            telemetryDroppedAccessUnits = 0L;
        }
        Log.i(TAG, line);
    }

    private void sendError(String message) {
        discardQueuedVideo();
        try {
            awaitVideoWriterIdle();
        } catch (IOException ignored) {
        }
        OutputStream stream = output;
        if (stream == null) {
            return;
        }
        byte[] payload = Protocol.utf8(message == null ? "unknown error" : message);
        if (payload.length > Protocol.MAX_PAYLOAD) {
            byte[] shortened = new byte[Protocol.MAX_PAYLOAD];
            System.arraycopy(payload, 0, shortened, 0, shortened.length);
            payload = shortened;
        }
        try {
            synchronized (outputLock) {
                Protocol.writeFrame(stream, Protocol.TYPE_ERROR, 0, payload);
            }
        } catch (IOException ignored) {
        }
    }

    private void sendStopFrame() {
        discardQueuedVideo();
        try {
            awaitVideoWriterIdle();
        } catch (IOException ignored) {
        }
        OutputStream stream = output;
        if (stream == null || stopFrameSent) {
            return;
        }
        synchronized (outputLock) {
            if (stopFrameSent) {
                return;
            }
            try {
                Protocol.writeFrame(stream, Protocol.TYPE_STOP, 0, new byte[0]);
            } catch (IOException ignored) {
            }
            stopFrameSent = true;
        }
    }

    private void requestStop() {
        stopRequested.set(true);
        stopVideoWriter();
        Thread control = controlThread;
        if (control != null) {
            control.interrupt();
        }
        ParcelFileDescriptor descriptor = fileDescriptor;
        if (descriptor != null) {
            try {
                descriptor.close();
            } catch (IOException ignored) {
            }
        }
    }

    private void stopVideoWriter() {
        synchronized (videoQueueLock) {
            videoQueue.clear();
            videoQueueLock.notifyAll();
        }
        Thread writer = videoWriterThread;
        if (writer != null) {
            writer.interrupt();
        }
    }

    private void cleanupSession() {
        synchronized (cleanupLock) {
            stopRequested.set(true);
            stopVideoWriter();
            Thread videoWriter = videoWriterThread;
            Thread control = controlThread;
            if (control != null && control != Thread.currentThread()) {
                control.interrupt();
            }

            ParcelFileDescriptor descriptor = fileDescriptor;
            fileDescriptor = null;
            // Close the descriptor before waiting on outputLock. This is what
            // releases a writer blocked in an AOA transfer when the host stops
            // reading or the cable is unplugged.
            closeQuietly(descriptor);

            InputStream inputStream = input;
            input = null;
            closeQuietly(inputStream);

            if (videoWriter != null && videoWriter != Thread.currentThread()) {
                try {
                    videoWriter.join(1_000);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
            }
            videoWriterThread = null;

            OutputStream outputStream;
            synchronized (outputLock) {
                outputStream = output;
                output = null;
                closeQuietly(outputStream);
            }

            if (control != null && control != Thread.currentThread()) {
                try {
                    control.join(1_000);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
            }
            controlThread = null;

            DisplayManager manager = displayManager;
            DisplayManager.DisplayListener listener = displayListener;
            displayManager = null;
            displayListener = null;
            if (manager != null && listener != null) {
                try {
                    manager.unregisterDisplayListener(listener);
                } catch (Exception ignored) {
                }
            }

            VirtualDisplay display = virtualDisplay;
            virtualDisplay = null;
            if (display != null) {
                try {
                    display.release();
                } catch (Exception ignored) {
                }
            }

            MediaProjection projection = mediaProjection;
            mediaProjection = null;
            if (projection != null) {
                if (projectionCallback != null) {
                    try {
                        projection.unregisterCallback(projectionCallback);
                    } catch (Exception ignored) {
                    }
                }
                try {
                    projection.stop();
                } catch (Exception ignored) {
                }
            }
            projectionCallback = null;

            MediaCodec activeCodec = codec;
            Surface activeSurface = codecSurface;
            codec = null;
            codecSurface = null;
            releaseCodec(activeCodec, activeSurface);

            HandlerThread thread = displayThread;
            displayThread = null;
            displayHandler = null;
            if (thread != null) {
                thread.quitSafely();
            }
            pendingSize = null;
            requestedAccessory = null;
            logTelemetryIfDue(true);
        }
    }

    private void releaseCodec(MediaCodec activeCodec, Surface activeSurface) {
        if (activeCodec != null) {
            try {
                activeCodec.stop();
            } catch (Exception ignored) {
            }
            try {
                activeCodec.release();
            } catch (Exception ignored) {
            }
        }
        if (activeSurface != null) {
            try {
                activeSurface.release();
            } catch (Exception ignored) {
            }
        }
    }

    private CaptureSize currentCaptureSize() {
        int rawWidth = 0;
        int rawHeight = 0;
        if (Build.VERSION.SDK_INT >= 30) {
            WindowManager windowManager = (WindowManager) getSystemService(Context.WINDOW_SERVICE);
            if (windowManager != null) {
                WindowMetrics windowMetrics = windowManager.getMaximumWindowMetrics();
                rawWidth = windowMetrics.getBounds().width();
                rawHeight = windowMetrics.getBounds().height();
            }
        }
        if (rawWidth <= 0 || rawHeight <= 0) {
            DisplayManager manager = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
            Display display = manager == null ? null : manager.getDisplay(Display.DEFAULT_DISPLAY);
            DisplayMetrics metrics = new DisplayMetrics();
            if (display != null) {
                display.getRealMetrics(metrics);
                rawWidth = metrics.widthPixels;
                rawHeight = metrics.heightPixels;
            } else {
                metrics.setTo(getResources().getDisplayMetrics());
                rawWidth = metrics.widthPixels;
                rawHeight = metrics.heightPixels;
            }
        }
        int density = getResources().getConfiguration().densityDpi;
        return captureSizeForBounds(rawWidth, rawHeight, density);
    }

    private CaptureSize captureSizeForBounds(int rawWidth, int rawHeight, int density) {
        rawWidth = Math.max(2, rawWidth);
        rawHeight = Math.max(2, rawHeight);
        float scale = Math.min(1f, MAX_CAPTURE_DIMENSION
                / (float) Math.max(rawWidth, rawHeight));
        int width = even(Math.max(2, Math.round(rawWidth * scale)));
        int height = even(Math.max(2, Math.round(rawHeight * scale)));
        int scaledDensity = Math.max(72, Math.round(Math.max(1, density) * scale));
        return new CaptureSize(width, height, scaledDensity);
    }

    private static int even(int value) {
        return value % 2 == 0 ? value : value - 1;
    }

    private static byte[] toAnnexB(byte[] input) {
        if (input.length == 0 || startsWithStartCode(input, 0)) {
            return input;
        }
        // Some AVC encoders expose CSD/access units as four-byte length
        // prefixed NAL units. Convert them so the desktop can feed ffplay's
        // Annex-B demuxer without codec-specific branching.
        int offset = 0;
        int nalCount = 0;
        int convertedLength = 0;
        while (offset + 4 <= input.length) {
            int length = ((input[offset] & 0xff) << 24)
                    | ((input[offset + 1] & 0xff) << 16)
                    | ((input[offset + 2] & 0xff) << 8)
                    | (input[offset + 3] & 0xff);
            offset += 4;
            if (length <= 0 || length > input.length - offset) {
                return prependStartCode(input);
            }
            convertedLength += 4 + length;
            offset += length;
            nalCount++;
        }
        if (offset != input.length || nalCount == 0) {
            return prependStartCode(input);
        }
        byte[] result = new byte[convertedLength];
        offset = 0;
        int outputOffset = 0;
        while (offset + 4 <= input.length) {
            int length = ((input[offset] & 0xff) << 24)
                    | ((input[offset + 1] & 0xff) << 16)
                    | ((input[offset + 2] & 0xff) << 8)
                    | (input[offset + 3] & 0xff);
            offset += 4;
            result[outputOffset++] = 0;
            result[outputOffset++] = 0;
            result[outputOffset++] = 0;
            result[outputOffset++] = 1;
            System.arraycopy(input, offset, result, outputOffset, length);
            outputOffset += length;
            offset += length;
        }
        return result;
    }

    private static boolean startsWithStartCode(byte[] data, int offset) {
        return data.length - offset >= 4
                && data[offset] == 0
                && data[offset + 1] == 0
                && ((data[offset + 2] == 0 && data[offset + 3] == 1)
                || data[offset + 2] == 1);
    }

    private static byte[] prependStartCode(byte[] input) {
        byte[] output = new byte[input.length + 4];
        output[0] = 0;
        output[1] = 0;
        output[2] = 0;
        output[3] = 1;
        System.arraycopy(input, 0, output, 4, input.length);
        return output;
    }

    private void startForegroundWithProjectionType(Notification notification) {
        startForeground(NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
    }

    private Notification buildNotification(String message) {
        Intent launchIntent = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent launchPendingIntent = PendingIntent.getActivity(
                this, 7002, launchIntent, pendingIntentFlags());
        Intent stopIntent = new Intent(this, ScreenCaptureService.class)
                .setAction(ACTION_STOP);
        PendingIntent stopPendingIntent = PendingIntent.getService(
                this, 7003, stopIntent, pendingIntentFlags());
        Notification.Builder builder = new Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_menu_view)
                .setContentTitle(getString(R.string.notification_title))
                .setContentText(message)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(launchPendingIntent)
                .addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_media_pause,
                        getString(R.string.stop), stopPendingIntent).build());
        if (Build.VERSION.SDK_INT >= 31) {
            builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        }
        return builder.build();
    }

    private int pendingIntentFlags() {
        return PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
    }

    private void createNotificationChannel() {
        NotificationManager manager = (NotificationManager)
                getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) {
            NotificationChannel channel = new NotificationChannel(
                    NOTIFICATION_CHANNEL_ID,
                    getString(R.string.notification_channel_name),
                    NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Shows when Android USB screen sharing is active.");
            manager.createNotificationChannel(channel);
        }
    }

    private void publishStatus(String message) {
        Intent status = new Intent(ACTION_STATUS)
                .setPackage(getPackageName())
                .putExtra(EXTRA_STATUS_MESSAGE, message);
        sendBroadcast(status);
        NotificationManager manager = (NotificationManager)
                getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null && sessionThread != null) {
            manager.notify(NOTIFICATION_ID, buildNotification(message));
        }
    }

    private Intent readResultData(Intent intent) {
        if (Build.VERSION.SDK_INT >= 33) {
            return intent.getParcelableExtra(EXTRA_RESULT_DATA, Intent.class);
        }
        //noinspection deprecation
        return (Intent) intent.getParcelableExtra(EXTRA_RESULT_DATA);
    }

    private UsbAccessory readAccessory(Intent intent) {
        if (Build.VERSION.SDK_INT >= 33) {
            return intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY, UsbAccessory.class);
        }
        //noinspection deprecation
        return (UsbAccessory) intent.getParcelableExtra(UsbManager.EXTRA_ACCESSORY);
    }

    private static void closeQuietly(InputStream stream) {
        if (stream != null) {
            try {
                stream.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static void closeQuietly(OutputStream stream) {
        if (stream != null) {
            try {
                stream.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static void closeQuietly(ParcelFileDescriptor descriptor) {
        if (descriptor != null) {
            try {
                descriptor.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static final class EncodedAccessUnit {
        final byte[] data;
        final long presentationTimeUs;
        final boolean keyFrame;
        final boolean codecConfig;

        EncodedAccessUnit(byte[] data, long presentationTimeUs,
                          boolean keyFrame, boolean codecConfig) {
            this.data = data;
            this.presentationTimeUs = presentationTimeUs;
            this.keyFrame = keyFrame;
            this.codecConfig = codecConfig;
        }
    }

    private static final class CaptureSize {
        final int width;
        final int height;
        final int densityDpi;

        CaptureSize(int width, int height, int densityDpi) {
            this.width = width;
            this.height = height;
            this.densityDpi = densityDpi;
        }

        @Override
        public boolean equals(Object object) {
            if (!(object instanceof CaptureSize)) {
                return false;
            }
            CaptureSize other = (CaptureSize) object;
            return width == other.width && height == other.height
                    && densityDpi == other.densityDpi;
        }

        @Override
        public int hashCode() {
            int result = width;
            result = 31 * result + height;
            return 31 * result + densityDpi;
        }
    }
}
