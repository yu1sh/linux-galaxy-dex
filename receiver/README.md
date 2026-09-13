# GUSB receiver

`gusb-receiver` is the Linux-side, view-only receiver for the Android
MediaProjection app. It uses the Android Open Accessory (AOA) protocol over
USB and does not use ADB or USB debugging. The receiver depends only on
Python 3.10+, `libusb-1.0`, and `ffplay` for the default display path.

## CLI

Run it from the repository root:

```sh
receiver/gusb-receiver
```

The command first looks for an already re-enumerated AOA data accessory. If
the phone is still in its ordinary USB/MTP state, it probes `GET_PROTOCOL`,
selects a single AOA-capable device, sends the six identifier strings, issues
AOA `START`, and waits for the Google accessory data PID to re-enumerate.
`--device BUS:ADDRESS` can be used when more than one USB device is attached;
the address is the one shown by `--list` or `lsusb` before AOA `START`.

```sh
receiver/gusb-receiver --list
receiver/gusb-receiver --device 1:7
receiver/gusb-receiver --wait 30 --hello-timeout 180
```

`--list` performs only descriptor inspection and `GET_PROTOCOL`; it never
sends identifier strings or `START`. It prints both ordinary USB AOA
responders and already re-enumerated data interfaces. Google AOA audio-only
PIDs (`0x2d02`, `0x2d03`) are rejected. When no device is specified, a
normal USB-mode device is selected only if exactly one device answers
`GET_PROTOCOL`; multiple responders require `--device`.

The default path starts `ffplay` after the first INFO frame. Raw H.264 has no
container timestamps, so the INFO `fps` value is supplied as ffplay's input
frame rate. The window title defaults to `Mirror (MediaProjection) - Galaxy
USB` and can be changed with `--window-title`. A rotation or size change is
accepted and the same H.264 pipe is kept open for the encoder's new SPS/PPS.

For a pipeline or capture test, use:

```sh
receiver/gusb-receiver --stdout >capture.h264
```

In this mode stdout contains only concatenated Annex-B H.264 VIDEO payloads;
all diagnostics go to stderr. The command does not read terminal stdin, so a
desktop entry with `Terminal=false` cannot be left waiting for input. Closing
stdout, closing the ffplay window, Ctrl-C, a malformed frame, or a USB
disconnect causes a best-effort GUSB STOP so the Android capture can end.

## GUSB/1 wire format

Every bulk transfer is parsed as a byte stream. A frame header is 12 bytes:

```text
magic[4] = "GUSB"
version  u8 = 1
type     u8
flags    u16 big-endian
length   u32 big-endian
```

`length` is limited to 16,000 bytes. Types are HELLO=1, VIDEO=2, INFO=3,
STOP=4, and ERROR=5. The PC sends a JSON HELLO; Android sends INFO JSON with
`width`, `height`, `fps`, and `codec: "h264"`, followed by Annex-B H.264 VIDEO
fragments. Invalid magic, version, type, or length closes the session and
causes a bounded ERROR diagnostic to be sent when possible.

## AOA identifiers

The values sent with AOA `SEND_STRING` are:

```text
manufacturer: Omarchy
model:        Galaxy USB MediaProjection
description:  USB screen projection
version:      1
uri:          https://github.com/yu1sh/linux-galaxy-dex
serial:       omarchy-galaxy-usb
```

The identifiers are application matching values, not an authentication
secret. Android OS USB permission and the app's manufacturer/model filter
guard the accessory endpoint. MediaProjection remains subject to Android
capture consent and protected or DRM surfaces may still be blank; this
receiver does not claim to bypass those restrictions.
