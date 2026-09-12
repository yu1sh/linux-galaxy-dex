# Linux Galaxy USB

Linux PCとSamsung Galaxy / Android端末をUSBで接続し、[scrcpy](https://github.com/Genymobile/scrcpy)を利用して画面表示・操作を行うためのUSB専用ランチャーです。

Galaxy S25 + Omarchyで作成・検証していますが、仕組み自体はOmarchyやGalaxy S25に限定されません。必要な `adb` / `scrcpy` / USB権限が利用できるLinux環境で動作するように構成されています。

> [!NOTE]
> Galaxy S25で実機検証しています。ほかのGalaxy端末やAndroid端末でもMirrorは動作する可能性が高い一方、DesktopモードのUIや挙動は端末メーカー・Androidバージョンによって異なります。

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

## Requirements

### Linux host

以下が必要です。

- Linux
- Bash
- ADB
- scrcpy **4.0以上**
- ADB用udevルールなど、一般ユーザーからUSB接続Android端末へアクセスできる設定
- `notify-send`（デスクトップ通知を使う場合のみ）

本ランチャーでは `--flex-display` と `--keep-active` を使用するため、scrcpy 4.0以上を前提としています。

### Android device

#### Mirror mode

scrcpyが対応するAndroid端末で利用できます。

Galaxy S25以外について端末モデルによる制限をコード上で設けていません。

#### Desktop mode

`--new-display` を利用するためAndroid 10以降が必要です。

ただし、仮想ディスプレイ上にLauncherやSystem UIがどのように表示されるかは端末によって異なります。そのため、Galaxy S25と同じDesktop UIになることは保証されません。

## Tested environment

現在、以下で実機動作を確認しています。

- Host: Omarchy / Linux
- Device: Samsung Galaxy S25
- Connection: USB-C data cable
- Mirror: 動作確認済み
- Desktop virtual display: 動作確認済み
- Mouse / keyboard control: 動作確認済み

ほかのLinuxディストリビューションやAndroid端末での報告も歓迎します。

## Installation

### 1. Dependencies

#### Arch Linux / Omarchy

```sh
sudo pacman -S --needed android-tools android-udev scrcpy libnotify
```

ほかのLinuxディストリビューションでは、各ディストリビューションのパッケージマネージャーを使用して次を導入してください。

- ADB / Android platform tools
- scrcpy 4.0+
- Android用udevルール
- `notify-send`（任意）

> [!IMPORTANT]
> ディストリビューション標準リポジトリのscrcpyが4.0未満の場合、本ランチャーのDesktopモードなどで未対応オプションのエラーが発生します。

### 2. Clone

```sh
git clone https://github.com/yu1sh/linux-galaxy-usb.git
cd linux-galaxy-usb
```

### 3. Install launcher

```sh
install -Dm755 galaxy-usb "$HOME/.local/bin/galaxy-usb"

install -Dm644 Galaxy-USB-Mirror.desktop \
  "$HOME/.local/share/applications/Galaxy-USB-Mirror.desktop"

install -Dm644 Galaxy-USB-Desktop.desktop \
  "$HOME/.local/share/applications/Galaxy-USB-Desktop.desktop"

install -Dm644 README.md \
  "$HOME/.local/share/doc/linux-galaxy-usb/README.md"

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
```

`$HOME/.local/bin` がPATHに含まれていることを確認してください。

Desktop Entryを使用しない場合は、`galaxy-usb` スクリプトだけインストールしてCLIから利用できます。

## Android setup

初回のみAndroid側でUSB debuggingを有効にします。

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
| `GALAXY_BITRATE` | `16M` | scrcpy video bitrate |
| `GALAXY_MAX_SIZE` | `1920` | Mirrorの最大解像度 |
| `GALAXY_MAX_FPS` | `60` | 最大フレームレート |
| `GALAXY_KEYBOARD` | `uhid` | Keyboard input mode |
| `GALAXY_MOUSE` | `uhid` | Mouse input mode |
| `GALAXY_ADB_SERIAL` | auto | 使用するADB device serial |
| `GALAXY_DESKTOP_SIZE` | `1920x1080/320` | 仮想ディスプレイのサイズ / DPI |
| `GALAXY_DESKTOP_APP` | `none` | Desktop起動時に開くAndroid package |

### SDK input

UHID入力が端末で正常に動作しない場合:

```sh
GALAXY_KEYBOARD=sdk GALAXY_MOUSE=sdk galaxy-usb mirror
```

## Controls

scrcpy標準ショートカットを利用します。

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
| Other Android devices | Mirror likely; Desktop UI is device-dependent |

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

処理の概要:

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

本ランチャー自体がAndroid映像の転送や入力処理を実装しているわけではありません。

実際の映像転送・仮想ディスプレイ作成・入力制御はscrcpyが担当し、このリポジトリはUSB端末の選択とscrcpyオプションをまとめる薄いBashラッパーとして動作します。

## Notes

- root不要
- Android側への常駐アプリのインストール不要
- ネットワークADBは自動選択対象外
- Samsung DeXそのものではありません
- USBケーブルはデータ通信対応のものを使用してください

## References

- [scrcpy](https://github.com/Genymobile/scrcpy)
- [scrcpy virtual display documentation](https://github.com/Genymobile/scrcpy/blob/master/doc/virtual-display.md)
