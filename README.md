# Galaxy S25 USB表示 for Omarchy

PCとGalaxy S25をUSB-Cのデータ通信対応ケーブルで接続し、S25の画面をOmarchy上に低遅延で表示・操作するための導線です。インストールされる起動ファイルは次の2つです。

- **Samsung S25 (USB) - Mirror**: S25のメイン画面をそのまま表示。PCのマウスとキーボードで操作できます。
- **Samsung S25 (USB) - Desktop**: `scrcpy --new-display` で1920×1080の横長仮想ディスプレイを作成します。S25のシステムUIを使ってSamsung DeXに似た操作感にできますが、Samsung公式DeXではありません。

## 必要なもの

- OmarchyなどのLinuxデスクトップ環境
- `bash`
- `adb`（Arch系では `android-tools`）
- 仮想ディスプレイに対応した最近の `scrcpy`
- USB権限用の `android-udev` またはディストリビューションに対応するADB用udevルール
- 通知を表示する場合のみ `libnotify`（`notify-send`）

Arch/Omarchyでは、例えば次のパッケージをインストールします。

```sh
sudo pacman -S --needed android-tools android-udev scrcpy libnotify
```

このリポジトリは第三者バイナリを同梱せず、インストール済みの `adb` と `scrcpy` を使用します。USB権限の変更後は、ディストリビューションの案内に従って再ログインやudevの再読み込みを行ってください。

## インストール

GitHubから取得したリポジトリのディレクトリで次を実行します。

```sh
git clone https://github.com/yu1sh/omarchy-s25-usb.git
cd omarchy-s25-usb
install -Dm755 omarchy-s25-usb "$HOME/.local/bin/omarchy-s25-usb"
install -Dm644 Samsung-S25-USB-Mirror.desktop "$HOME/.local/share/applications/Samsung-S25-USB-Mirror.desktop"
install -Dm644 Samsung-S25-USB-Desktop.desktop "$HOME/.local/share/applications/Samsung-S25-USB-Desktop.desktop"
install -Dm644 README.md "$HOME/.local/share/doc/omarchy-s25-usb/README.md"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
```

`$HOME/.local/bin`をPATHに含めてください。アプリメニューに表示されない場合は、ログインし直すかデスクトップ環境のアプリケーションキャッシュを更新してください。

## 初回接続

1. S25で「設定 → 端末情報 → ソフトウェア情報 → ビルド番号」を7回タップして開発者向けオプションを有効にします。
2. 「設定 → 開発者向けオプション → USBデバッグ」をオンにします。
3. S25をロック解除した状態で、データ通信対応のUSB-CケーブルをOmarchy PCへ接続します。
4. S25に表示される「USBデバッグを許可しますか？」でこのPCを許可します。
5. Omarchyのアプリメニューから上記のMirrorまたはDesktopを起動します。

端末から確認する場合は `omarchy-s25-usb list` を実行します。S25の行が `device` になり、`usb:` が含まれていればUSB接続は準備完了です。ネットワークADBはこの起動ファイルでは受け付けません。

## 操作

標準設定は低遅延のH.264、最大60 fps、16 Mbps、キーボード・マウスはUHIDです。UHID入力を初めて使うときは、S25の「物理キーボード」設定を一度開いてください。入力方式に問題がある場合は、端末から次のようにSDK方式で起動できます。

UHIDマウスをPCへ戻すには左Altキーを単押しします。全画面はAlt+FまたはF11、終了はAlt+Qです。

```sh
S25_KEYBOARD=sdk S25_MOUSE=sdk omarchy-s25-usb mirror
```

Desktopで特定のアプリを仮想画面へ起動したい場合は、例えば `S25_DESKTOP_APP=com.android.settings omarchy-s25-usb desktop` のように指定できます。通常はS25のシステムUIを使うため、アプリ指定は不要です。

## 位置づけ

Desktop起動はUSB ADB上のscrcpy仮想ディスプレイを使うため、S25のシステムUIをPCウィンドウへリアルタイム転送し、PC入力を返すDeX風構成です。Samsung公式DeXではなく、OSやアプリによって表示・入力・互換性が異なります。

## DRM保護コンテンツの制限

HDCPなどで保護された映像はAndroid側が画面キャプチャを禁止するため、scrcpyでは黒画面や表示拒否になることがあります。U-NEXTなどのDRM保護映像をこのランチャーで表示・録画できるようにはなりません。

## 検証状況

起動ファイルのBash構文、Desktop Entry、未接続時の案内を検証済みです。Galaxy S25実機でMirrorの映像転送と、Desktopの1920×1080仮想表示・マウス・キーボード操作を確認しています。端末のAndroidバージョンやscrcpyの版によって利用できる機能は変わります。音声転送は未確認です。

参考: [scrcpy公式の仮想ディスプレイ仕様](https://github.com/Genymobile/scrcpy/blob/master/doc/virtual-display.md)
