# Linux Galaxy DEX

Linux PCとAndroid端末をUSBで接続して、画面表示を行うランチャーとAndroidアプリです。

既存の `mirror` / `desktop` は [scrcpy](https://github.com/Genymobile/scrcpy) を使うADB経路です。新しい `projection` はAndroidのMediaProjectionとAndroid Open Accessory (AOA)を使うため、USBデバッグを有効にせず画面をPCへ送れます。コードは特定の端末モデルを前提にしません。

Galaxy S25 + Omarchyで作成・検証していますが、端末がAndroid USB accessory modeを実装していればほかのAndroid端末でも利用できます。端末メーカーがAOAを無効にしている場合は利用できません。

> [!IMPORTANT]
> `projection` は映像表示専用です。MediaProjectionは画面キャプチャのAPIであり、ADBのようなシステム入力権限を与えません。PCのマウス・キーボード操作が必要な場合は、USBデバッグを有効にした既存のADB `mirror` を使ってください。

> [!WARNING]
> Androidの `FLAG_SECURE` や保護された動画サーフェスはMediaProjectionでも黒画面になることがあります。U-NEXTなどのDRM映像が表示できるようになることは保証しません。

> [!NOTE]
> ADBのMirror/DesktopはGalaxy S25で実機検証しています。MediaProjection経路はAOA対応端末で利用できますが、対応状況とDesktopモードのUIは端末メーカー・Androidバージョンによって異なります。

## Features

### Mirror

Android端末のメインディスプレイをLinux上に低遅延で表示し、PCのマウスとキーボードから操作します。

標準設定:

- USB ADB接続のみ使用
- H.264
- 16 Mbps
- 最大60 fps
- 最大1920 px
- video buffer 0
- UHID keyboard / mouse
- `scrcpy --keep-active`

### Desktop

`scrcpy --new-display` を利用して、Android端末内に独立した横長の仮想ディスプレイを作成します。

標準では:

```text
1920x1080 / 320 dpi
```

の仮想ディスプレイを作成し、`--flex-display` によりPC側のウィンドウサイズに追従させます。

Samsung Galaxyでは端末側のSystem UIによってデスクトップ風のUIを利用できる場合がありますが、**Samsung公式DeXを起動しているわけではありません**。

端末によって仮想ディスプレイにLauncherが表示されない場合は、`GALAXY_DESKTOP_APP` で起動するアプリを指定できます。

例:

```sh
GALAXY_DESKTOP_APP=com.android.settings galaxy-usb desktop
```

### MediaProjection

`projection` は同梱のAndroidアプリが `MediaProjection` でキャプチャした画面を、AOAのbulk endpointでPCへ転送します。USB debugging、ADB認証、scrcpyのサーバーはこの経路では使用しません。

```sh
galaxy-usb projection
```

PC側はAOAの初期化後に、Androidアプリが送るH.264 Annex-Bストリームを `ffplay` で表示します。USBケーブルを接続したまま、Androidアプリで **Start sharing** を押し、画面キャプチャとUSB accessoryの許可を順に承認してください。キャプチャの開始・停止ごとにAndroidが表示するMediaProjection同意を必要とします。

複数のUSB機器が接続されている場合は、先に候補を確認できます。

```sh
receiver/gusb-receiver --list
GALAXY_PROJECTION_DEVICE=BUS:ADDRESS galaxy-usb projection
```

AOAを利用できない端末では、USB tetheringを手動で有効にしてTCP/WebSocketで転送する方式が代替候補になります。ただし、端末ごとにテザリングの有効化手順とIPアドレスが異なり、このリポジトリの既定経路にはしていません。

## Requirements

### Linux host

以下が必要です。

- Linux
- Bash
- ADB / scrcpy **4.0以上**（`mirror` / `desktop` 用）
- `projection` 用の libusb と ffplay（ffmpeg）
- ADBまたはAOA用udevルールなど、一般ユーザーからUSB接続Android端末へアクセスできる設定
- `notify-send`（デスクトップ通知を使う場合のみ）

本ランチャーでは `--flex-display` と `--keep-active` を使用するため、scrcpy 4.0以上を前提としています。

### Android device

#### Mirror mode

scrcpyが対応するAndroid端末で利用できます。

Galaxy S25以外について端末モデルによる制限をコード上で設けていません。

#### Desktop mode

`--new-display` を利用するためAndroid 10以降が必要です。

ただし、仮想ディスプレイ上にLauncherやSystem UIがどのように表示されるかは端末によって異なります。そのため、Galaxy S25と同じDesktop UIになることは保証されません。

#### MediaProjection mode

- Android 10 (API 29) 以降
- Android USB accessory modeを端末がサポートしていること
- USB-Cデータケーブル
- AndroidアプリでMediaProjectionとUSB accessoryの許可を承認すること

このモードはUSB debuggingの設定やADB認証を必要としません。

## Tested environment

現在、以下のADB経路とAOA MediaProjection経路の実機動作を確認しています。AOA対応状況やDesktop UIは端末とAndroidバージョンによって異なります。

- Host: Omarchy / Linux
- Device: Samsung Galaxy S25
- Connection: USB-C data cable
- Mirror: 動作確認済み
- Desktop virtual display: 動作確認済み
- Mouse / keyboard control: 動作確認済み
- MediaProjection/AOA view-only: USB debugging OFFで動作確認済み（H.264 INFO 886x1920 / 60fps、AOA data PID `0x2d00`）

ほかのLinuxディストリビューションやAndroid端末での報告も歓迎します。

## Installation

### 1. Dependencies

#### Arch Linux / Omarchy

```sh
sudo pacman -S --needed android-tools android-udev scrcpy ffmpeg libusb libnotify
```

ほかのLinuxディストリビューションでは、各ディストリビューションのパッケージマネージャーを使用して次を導入してください。

- ADB / Android platform tools
- scrcpy 4.0+
- `projection` 用の libusb と ffplay（ffmpeg）
- Android用udevルール
- `notify-send`（任意）

> [!IMPORTANT]
> ディストリビューション標準リポジトリのscrcpyが4.0未満の場合、本ランチャーのDesktopモードなどで未対応オプションのエラーが発生します。

### 2. Clone

```sh
git clone https://github.com/yu1sh/linux-galaxy-dex.git
cd linux-galaxy-dex
```

### 3. Install launcher

```sh
install -Dm755 galaxy-usb "$HOME/.local/bin/galaxy-usb"

# The receiver is a Python 3/libusb program; install the wrapper and its
# same-directory modules for projection mode.
install -Dm755 receiver/gusb-receiver \
  "$HOME/.local/libexec/gusb-receiver"
install -Dm755 receiver/gusb_receiver.py \
  "$HOME/.local/libexec/gusb_receiver.py"
install -Dm644 receiver/protocol.py \
  "$HOME/.local/libexec/protocol.py"

install -Dm644 desktop/Galaxy-USB-Mirror.desktop \
  "$HOME/.local/share/applications/Galaxy-USB-Mirror.desktop"

install -Dm644 desktop/Galaxy-USB-Desktop.desktop \
  "$HOME/.local/share/applications/Galaxy-USB-Desktop.desktop"

install -Dm644 desktop/Galaxy-USB-MediaProjection.desktop \
  "$HOME/.local/share/applications/Galaxy-USB-MediaProjection.desktop"

install -Dm644 README.md \
  "$HOME/.local/share/doc/linux-galaxy-dex/README.md"

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
```

`$HOME/.local/bin` がPATHに含まれていることを確認してください。

Desktop Entryを使用しない場合は、`galaxy-usb` スクリプトだけインストールしてCLIから利用できます。

### 4. Build the Android app

Android StudioまたはAndroid SDKを用意した環境で、同梱のアプリをDebug署名でビルドします。

```sh
cd android-app
./gradlew :app:assembleDebug
```

生成物は `android-app/app/build/outputs/apk/debug/app-debug.apk` です。APKは開発テスト用のDebug署名で、リポジトリには署名鍵や生成APKをコミットしません。USB debuggingを使わない場合は、USB接続前にMTPやクラウド経由でAPKを端末へコピーし、端末のファイルマネージャーからインストールしてください。Androidの「不明なアプリのインストール」の許可が必要になることがあります。

端末へアプリをインストールした後に、Androidアプリを一度起動して表示名が **Android USB Mirror** であることを確認します。

## Android setup

### ADB mirror / desktop

既存のMirror操作やDesktop仮想ディスプレイを使う場合は、初回のみAndroid側でUSB debuggingを有効にします。

Samsung Galaxyの場合:

1. **設定 → 端末情報 → ソフトウェア情報**
2. **ビルド番号**を7回タップ
3. **開発者向けオプション**を開く
4. **USBデバッグ**を有効化
5. データ通信対応USBケーブルでLinux PCへ接続
6. Android側の **「USBデバッグを許可しますか？」** を許可

接続確認:

```sh
galaxy-usb list
```

例:

```text
List of devices attached
RXXXXXXXXXX    device usb:1-2 product:... model:...
```

本ランチャーは `adb devices -l` の結果から **`device` かつ `usb:` を持つ端末だけ**を選択します。

そのため、ネットワークADB端末は自動選択しません。

### MediaProjection projection

USB debuggingをOFFのまま使う場合は、次の順で起動します。

1. Androidアプリを起動し、USB-CデータケーブルでLinux PCへ接続します。
2. PCで `galaxy-usb projection` を実行するか、アプリ一覧の **Mirror (MediaProjection) - Galaxy USB** を選びます。
3. Android側でUSB accessoryの接続許可を承認します。
4. Androidアプリの **Start sharing** を押し、画面共有の確認ダイアログで表示範囲を承認します。
5. PC側の `ffplay` ウィンドウにAndroid画面が表示されます。

接続中にUSBモードをMTPからAOAへ切り替えるため、APKの転送はprojectionを開始する前に済ませてください。終了時はAndroidアプリの **Stop sharing** またはPCの表示ウィンドウを閉じます。

## Usage

### Mirror

```sh
galaxy-usb mirror
```

引数を省略した場合もMirrorモードになります。

```sh
galaxy-usb
```

### Desktop

```sh
galaxy-usb desktop
```

### MediaProjection (USB debugging off)

```sh
galaxy-usb projection
```

このコマンドはADBを起動せず、PCをAOA USB hostとして初期化してAndroidアプリの映像を受信します。Android側で画面共有の同意が済んでいない場合は、コマンドが待機している間にアプリの許可を承認してください。

### List devices

```sh
galaxy-usb list
```

### Help

```sh
galaxy-usb --help
```

## Multiple USB devices

USB接続された認証済みADB端末が複数存在する場合、ランチャーは誤った端末を選択しないよう停止します。

使用するADB serialを明示してください。

```sh
GALAXY_ADB_SERIAL=XXXXXXXX galaxy-usb mirror
```

ADB serialは次で確認できます。

```sh
adb devices -l
```

## Configuration

現在の環境変数は以下です。

| Variable | Default | Description |
|---|---:|---|
| `GALAXY_WAIT_SECONDS` | `15` | USB ADB端末を待機する秒数 |
| `GALAXY_PROJECTION_WAIT_SECONDS` | `120` | AOA receiverがUSB accessoryとAndroidの許可を待機する秒数 |
| `GALAXY_BITRATE` | `16M` | scrcpy video bitrate |
| `GALAXY_MAX_SIZE` | `1920` | Mirrorの最大解像度 |
| `GALAXY_MAX_FPS` | `60` | 最大フレームレート |
| `GALAXY_KEYBOARD` | `uhid` | Keyboard input mode |
| `GALAXY_MOUSE` | `uhid` | Mouse input mode |
| `GALAXY_ADB_SERIAL` | auto | 使用するADB device serial |
| `GALAXY_DESKTOP_SIZE` | `1920x1080/320` | 仮想ディスプレイのサイズ / DPI |
| `GALAXY_DESKTOP_APP` | `none` | Desktop起動時に開くAndroid package |
| `GALAXY_PROJECTION_RECEIVER` | auto | AOA受信プログラムのパス |
| `GALAXY_PROJECTION_DEVICE` | auto | AOA初期USBデバイスの `BUS:ADDRESS`（複数接続時） |

### SDK input

UHID入力が端末で正常に動作しない場合:

```sh
GALAXY_KEYBOARD=sdk GALAXY_MOUSE=sdk galaxy-usb mirror
```

## Controls

ADBのMirror/Desktopではscrcpy標準ショートカットを利用します。MediaProjectionのprojectionは映像表示専用で、入力イベントを端末へ送信しません。

代表例:

- **Left Alt**: UHID mouse captureを解除
- **Alt + F / F11**: Fullscreen
- **Alt + Q**: Quit

scrcpyのバージョンや設定によってショートカットは異なる場合があります。

## Compatibility

| Environment | Expected compatibility |
|---|---|
| Omarchy | Tested |
| Arch Linux | High |
| Other standard Linux desktop distributions | Expected to work if dependencies are satisfied |
| Galaxy S25 | Tested |
| Other recent Galaxy devices | Expected to work; not fully tested |
| Other Android devices | ADB Mirror likely; Desktop UI and AOA support are device-dependent |
| AOA MediaProjection | Tested with USB debugging off on one AOA-compatible device; broader device compatibility is not established; view-only |

このツールにはOmarchy固有APIやSamsung S25のモデル番号判定はありません。実行ファイル名・環境変数名・Desktop EntryもGalaxy向けの汎用名称に統一しています。

端末モデル名はADBから取得していますが、S25であるかどうかを判定して処理を拒否するコードはありません。

## DRM-protected content

HDCP / DRMなどで保護された動画は、Android側によって画面キャプチャが禁止される場合があります。

その場合、scrcpyでは次のような挙動になることがあります。

- 黒画面
- 映像部分だけ非表示
- キャプチャ拒否

このランチャーはAndroidのDRM制限を回避するものではありません。

## How it works

ADB経路の処理概要:

```text
Android device
      │
      │ USB
      ▼
 Linux USB subsystem
      │
      ▼
     ADB
      │
      ▼
galaxy-usb
      │
      ├─ adb server start
      ├─ USB接続端末のみ検出
      ├─ authorization確認
      ├─ device serial選択
      │
      └─ scrcpy起動
             │
             ├─ Mirror
             │    └─ Android main display
             │
             └─ Desktop
                  └─ Android virtual display
```

ADB経路では、本ランチャー自体がAndroid映像の転送や入力処理を実装しているわけではありません。

実際のADB映像転送・仮想ディスプレイ作成・入力制御はscrcpyが担当し、この経路のランチャーはUSB端末の選択とscrcpyオプションをまとめるBashラッパーです。

MediaProjection経路は次のように動作します。

```text
Android app
  ├─ user grants MediaProjection
  ├─ foreground service + VirtualDisplay
  ├─ MediaCodec (H.264 Annex-B)
  └─ UsbAccessory.openAccessory()
          │ USB bulk endpoints (AOA)
          ▼
PC AOA receiver (libusb)
  └─ framed video payload → ffplay
```

AOAの初期化にはAndroid Open Accessory仕様のcontrol request 51 (Get Protocol)、52 (Send String)、53 (Start)を使います。再列挙後のデータ用PID `0x2d00` / `0x2d01` / `0x2d04` / `0x2d05` のbulk endpointだけを使用し、音声専用PIDは対象外です。Android側の識別文字列は端末機種名ではなく、アプリのaccessory filterと一致する固定の汎用名です。

アプリとPC受信部のデータフレームは次の形式です。

| Field | Size | Description |
|---|---:|---|
| Magic | 4 bytes | ASCII `GUSB` |
| Version | 1 byte | `1` |
| Type | 1 byte | `HELLO=1`, `VIDEO=2`, `INFO=3`, `STOP=4`, `ERROR=5` |
| Flags | 2 bytes | VIDEO: `KEY_FRAME=0x0001`, `CODEC_CONFIG=0x0002`, `END_OF_ACCESS_UNIT=0x0004`; zero otherwise |
| Length | 4 bytes | Big-endian payload length, at most 16000 |
| Payload | Length | UTF-8 JSON for INFO, H.264 bytes for VIDEO |

Android側はMediaProjectionの同意を得た後にINFOを送り、続けてMediaCodecの設定データと映像データをVIDEOフレームで送ります。AOAの論理パケット上限に合わせ、長いCodec出力は複数フレームに分割します。

## Notes

- root不要
- ネットワークADBは自動選択対象外
- Samsung DeXそのものではありません
- `projection` はAndroidアプリのインストールと、毎回のMediaProjection同意が必要です
- `projection` はUSB debuggingを使用しませんが、端末とPCのAOA対応が必要です
- USBケーブルはデータ通信対応のものを使用してください

## References

- [scrcpy](https://github.com/Genymobile/scrcpy)
- [Android MediaProjection](https://developer.android.com/media/grow/media-projection)
- [Android foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types)
- [Android USB accessory overview](https://developer.android.com/develop/connectivity/usb/accessory)
- [Android Open Accessory 1.0 protocol](https://source.android.com/docs/core/interaction/accessories/aoa)
- [Android Open Accessory 2.0 protocol](https://source.android.com/docs/core/interaction/accessories/aoa2)
- [scrcpy virtual display documentation](https://github.com/Genymobile/scrcpy/blob/master/doc/virtual-display.md)
