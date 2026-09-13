package com.yu1sh.androidusbmirror;

import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/**
 * GUSB/1 framing shared with the desktop AOA host.
 *
 * <p>Every frame has a 12-byte header: magic "GUSB", version, type, flags
 * (big-endian u16), and payload length (big-endian u32). AOA logical packets
 * are limited to 16,384 bytes, so a frame payload is capped at 16,000 bytes.
 */
final class Protocol {
    static final int HEADER_LENGTH = 12;
    static final int MAX_PAYLOAD = 16_000;
    static final int USB_TRANSFER_BUFFER_SIZE = 16_384;
    static final byte VERSION = 1;

    static final int TYPE_HELLO = 1;
    static final int TYPE_VIDEO = 2;
    static final int TYPE_INFO = 3;
    static final int TYPE_STOP = 4;
    static final int TYPE_ERROR = 5;

    // Flags apply to VIDEO chunks. A receiver may ignore flags and concatenate
    // all VIDEO payloads, but these make keyframes/configuration observable.
    static final int FLAG_KEY_FRAME = 0x0001;
    static final int FLAG_CODEC_CONFIG = 0x0002;
    static final int FLAG_END_OF_ACCESS_UNIT = 0x0004;

    private static final byte[] MAGIC = new byte[]{'G', 'U', 'S', 'B'};

    private Protocol() {
    }

    static final class Frame {
        final int type;
        final int flags;
        final byte[] payload;

        Frame(int type, int flags, byte[] payload) {
            this.type = type;
            this.flags = flags;
            this.payload = payload;
        }
    }

    static void writeFrame(OutputStream output, int type, int flags, byte[] payload)
            throws IOException {
        if (payload == null) {
            payload = new byte[0];
        }
        writeFrame(output, type, flags, payload, 0, payload.length);
    }

    static void writeFrame(OutputStream output, int type, int flags,
                           byte[] payload, int offset, int length) throws IOException {
        if (payload == null) {
            if (offset != 0 || length != 0) {
                throw new IOException("null GUSB payload with non-empty range");
            }
            payload = new byte[0];
        }
        if (offset < 0 || length < 0 || offset > payload.length - length) {
            throw new IOException("invalid GUSB payload range");
        }
        if (length > MAX_PAYLOAD) {
            throw new IOException("GUSB payload exceeds " + MAX_PAYLOAD + " bytes");
        }
        byte[] header = new byte[HEADER_LENGTH];
        System.arraycopy(MAGIC, 0, header, 0, MAGIC.length);
        header[4] = VERSION;
        header[5] = (byte) (type & 0xff);
        header[6] = (byte) ((flags >>> 8) & 0xff);
        header[7] = (byte) (flags & 0xff);
        header[8] = (byte) ((length >>> 24) & 0xff);
        header[9] = (byte) ((length >>> 16) & 0xff);
        header[10] = (byte) ((length >>> 8) & 0xff);
        header[11] = (byte) (length & 0xff);
        output.write(header);
        output.write(payload, offset, length);
        output.flush();
    }

    static byte[] utf8(String value) {
        return value.getBytes(StandardCharsets.UTF_8);
    }

    /**
     * Reads one AOA logical packet at a time into a full-size buffer and then
     * parses frames from an accumulator. This avoids partial reads dropping the
     * remainder of a USB transfer, as required by UsbManager.openAccessory().
     */
    static final class Reader {
        private final InputStream input;
        private final byte[] transferBuffer = new byte[USB_TRANSFER_BUFFER_SIZE];
        private byte[] pending = new byte[USB_TRANSFER_BUFFER_SIZE * 2];
        private int pendingStart;
        private int pendingEnd;

        Reader(InputStream input) {
            this.input = input;
        }

        Frame read() throws IOException, ProtocolException {
            while (true) {
                Frame frame = takeFrame();
                if (frame != null) {
                    return frame;
                }
                int count = input.read(transferBuffer, 0, transferBuffer.length);
                if (count < 0) {
                    throw new EOFException("USB accessory disconnected");
                }
                if (count == 0) {
                    continue;
                }
                append(transferBuffer, count);
            }
        }

        private void append(byte[] source, int count) {
            int available = pendingEnd - pendingStart;
            ensureCapacity(available + count);
            if (pendingStart != 0 && available != 0) {
                System.arraycopy(pending, pendingStart, pending, 0, available);
            }
            pendingStart = 0;
            pendingEnd = available;
            System.arraycopy(source, 0, pending, pendingEnd, count);
            pendingEnd += count;
        }

        private void ensureCapacity(int required) {
            if (pending.length >= required) {
                return;
            }
            int next = pending.length;
            while (next < required) {
                next = Math.min(USB_TRANSFER_BUFFER_SIZE * 8, next * 2);
                if (next < required && next == USB_TRANSFER_BUFFER_SIZE * 8) {
                    // The frame length is checked before this can grow without
                    // bound. Keep the fallback useful for a split frame.
                    next = required;
                    break;
                }
            }
            pending = Arrays.copyOf(pending, next);
        }

        private Frame takeFrame() throws ProtocolException {
            int available = pendingEnd - pendingStart;
            if (available < HEADER_LENGTH) {
                return null;
            }
            int offset = pendingStart;
            for (int i = 0; i < MAGIC.length; i++) {
                if (pending[offset + i] != MAGIC[i]) {
                    throw new ProtocolException("invalid GUSB magic");
                }
            }
            if ((pending[offset + 4] & 0xff) != (VERSION & 0xff)) {
                throw new ProtocolException("unsupported GUSB version");
            }
            int type = pending[offset + 5] & 0xff;
            int flags = ((pending[offset + 6] & 0xff) << 8)
                    | (pending[offset + 7] & 0xff);
            int length = ((pending[offset + 8] & 0xff) << 24)
                    | ((pending[offset + 9] & 0xff) << 16)
                    | ((pending[offset + 10] & 0xff) << 8)
                    | (pending[offset + 11] & 0xff);
            if (length < 0 || length > MAX_PAYLOAD) {
                throw new ProtocolException("invalid GUSB payload length: " + length);
            }
            if (available < HEADER_LENGTH + length) {
                return null;
            }
            byte[] payload = Arrays.copyOfRange(
                    pending, offset + HEADER_LENGTH, offset + HEADER_LENGTH + length);
            pendingStart += HEADER_LENGTH + length;
            if (pendingStart == pendingEnd) {
                pendingStart = 0;
                pendingEnd = 0;
            }
            return new Frame(type, flags, payload);
        }
    }

    static final class ProtocolException extends IOException {
        ProtocolException(String message) {
            super(message);
        }
    }
}
