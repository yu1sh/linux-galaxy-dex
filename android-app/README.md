# Android USB Mirror

This directory is a standalone Android application. It captures the Android
display only after the user grants a fresh MediaProjection consent and sends
hardware encoded H.264 over an Android Open Accessory (AOA) connection.
The project supports Android 10 (API 29) and newer, targets Android 15 (API
35), and is view-only.

The application has no `INTERNET` permission and does not use ADB or USB
debugging. The desktop must first switch the device into AOA mode with the
following accessory identity:

```text
manufacturer: Omarchy
model: Galaxy USB MediaProjection
description: USB screen projection
version: 1
uri: https://github.com/yu1sh/omarchy-s25-usb
serial: omarchy-galaxy-usb
```

The Android-side filter matches the manufacturer and model. The service also
checks those two values at runtime before opening the accessory.

## GUSB/1 framing

Each frame uses this 12-byte big-endian header:

```text
magic[4] = "GUSB"
version  u8 = 1
type     u8
flags    u16
length   u32
payload  length bytes
```

`length` is at most 16,000 bytes, keeping the whole frame below the AOA
16,384-byte logical packet limit.

| Type | Direction | Meaning |
| ---: | :---: | :--- |
| 1 | PC → Android | HELLO |
| 2 | Android → PC | VIDEO, Annex-B H.264 bytes |
| 3 | Android → PC | INFO JSON (`width`, `height`, `fps`, `codec`, `format`, `protocol`) |
| 4 | either | STOP / end of session |
| 5 | either | ERROR, UTF-8 message |

VIDEO access units are split into payload-sized chunks. Flags are
`0x0001` keyframe, `0x0002` codec configuration, and `0x0004` end of access
unit. The PC can concatenate VIDEO payloads and feed them to an Annex-B H.264
decoder.

## Build

Install Android SDK platform 35 and build tools, set `ANDROID_SDK_ROOT` (or
`ANDROID_HOME`), then run:

```sh
./gradlew --no-daemon assembleDebug
```

The debug APK is written to `app/build/outputs/apk/debug/app-debug.apk`.
