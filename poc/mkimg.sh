#!/usr/bin/env bash
# poc/mkimg.sh — Phase 0 PoC 用。DJI Mic 3 のストレージ（exFAT）を模した
#                ディスクイメージを作成・attach・detach する使い捨てスクリプト。
#
#   - src/ からは絶対に import / 参照しない（SPEC §6）
#   - tests/ とは別物。CI では実行しない（macOS ホストの hdiutil に依存する）
#   - 測定結果は docs/POC.md に記録する
#
# 使い方:
#   poc/mkimg.sh create     イメージを作る（attach はしない）
#   poc/mkimg.sh attach     attach して §5.1 の fixture を置く
#   poc/mkimg.sh detach     detach する
#   poc/mkimg.sh destroy    detach してイメージファイルを削除する
#   poc/mkimg.sh status     現在の状態を表示する
#
# 環境変数:
#   VDPOC_IMG   イメージのパス（既定 /tmp/vdpoc.dmg）
#   VDPOC_VOL   ボリューム名（既定 VDPOC。exFAT のため 11 文字以内・英大文字）
#   VDPOC_SIZE  イメージサイズ（既定 64m）
#   VDPOC_FS    ファイルシステム（既定 "MS-DOS FAT32"。DJI Mic 3 実機がこれ。
#               docs/POC.md §2 参照。"ExFAT" も指定できる）

set -euo pipefail

IMG="${VDPOC_IMG:-/tmp/vdpoc.dmg}"
VOL="${VDPOC_VOL:-VDPOC}"
SIZE="${VDPOC_SIZE:-64m}"
FS="${VDPOC_FS:-MS-DOS FAT32}"
MNT="/Volumes/${VOL}"

# §5.1 / §5.2 の命名規則にそのまま一致させる（大文字小文字まで含めて）
FIXTURE_DIR="${MNT}/TX_MIC001_20260829_071201"
FIXTURE_DENOISED="${FIXTURE_DIR}/TX01_MIC002_20260829_071204.wav"
FIXTURE_ORIG="${FIXTURE_DIR}/TX01_MIC002_20260829_071204_orig.wav"

is_attached() { /sbin/mount | grep -q " on ${MNT} ("; }

cmd_create() {
  if [ -f "$IMG" ]; then echo "exists: $IMG"; return 0; fi
  hdiutil create -quiet -size "$SIZE" -fs "$FS" -volname "$VOL" "$IMG"
  echo "created: $IMG ($SIZE, $FS, volname=$VOL)"
}

cmd_attach() {
  [ -f "$IMG" ] || { echo "no image: $IMG (run 'create' first)" >&2; return 1; }
  if is_attached; then
    echo "already attached: $MNT"
  else
    hdiutil attach -quiet "$IMG"
    echo "attached: $MNT"
  fi
  mkdir -p "$FIXTURE_DIR"
  [ -f "$FIXTURE_DENOISED" ] || dd if=/dev/urandom of="$FIXTURE_DENOISED" bs=1m count=8 status=none
  [ -f "$FIXTURE_ORIG" ]     || dd if=/dev/urandom of="$FIXTURE_ORIG"     bs=1m count=8 status=none
  sync
  echo "fixture:"
  ls -la "$FIXTURE_DIR"
}

cmd_detach() {
  if is_attached; then hdiutil detach -quiet "$MNT"; echo "detached: $MNT"
  else echo "not attached: $MNT"; fi
}

cmd_destroy() { cmd_detach || true; rm -f "$IMG"; echo "removed: $IMG"; }

cmd_status() {
  printf 'image   : %s %s\n' "$IMG" "$([ -f "$IMG" ] && echo '(exists)' || echo '(absent)')"
  printf 'mounted : %s\n'    "$(is_attached && echo yes || echo no)"
  /sbin/mount | grep " on /Volumes/" || true
}

case "${1:-}" in
  create) cmd_create ;;
  attach) cmd_attach ;;
  detach) cmd_detach ;;
  destroy) cmd_destroy ;;
  status) cmd_status ;;
  *) sed -n '2,30p' "$0" >&2; exit 2 ;;
esac
