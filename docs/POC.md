# VoiceDock Phase 0 PoC 測定記録

`docs/SPEC.md` が「こう設計する」を書き、**本ファイルは「実際にこう動いた」を書く。**
両者が食い違う場合は本ファイルの実測値を正とし、**SPEC を直すチケットを起票する。**

| 章 | 内容 | 対応 SPEC | 記入チケット | 結果 |
|---|---|---|---|---|
| 1 | ホスト環境 | §3.1 | #2 | ✅ |
| 2 | **ファイル共有実装の決定的な差**（R-1） | §3.2, §4.1, §18.2 | #2 | ⚠ **VirtioFS で FAIL / gRPC FUSE で PASS** |
| 3 | ホットプラグ伝播と実機読み取り（P0-6 / P0-7 / P0-9） | §5.5, §10.1, §18.3 | #2 | ✅ PASS |
| 4 | マウントモードの効き（ND-23 / P0-8） | §14.2 | #2 | ✅ PASS |
| 5 | 非 DJI ボリュームと symlink（R-19 / R-20） | §5.4, §14.1.1 | #2 | ✅ PASS |
| 6 | DJI Mic 3 実機の実測（P0-2〜P0-5） | §5.1, §5.2, §5.3 | #2 | ✅ PASS（SPEC に誤り 3 件） |
| 7 | ファイル共有実装の性能比較 | §3.2 | #2 | ✅ 実測 |
| 8 | **アーキテクチャの決定** | §4.1, §5.6 | #2 | ✅ **§5.6 案 A（ホスト Helper）を採用** |
| 9 | Phase 0 ハードゲート判定 | §5.5, §21.2 | #2 / #3 | ⚠ 一部保留（#3 待ち） |
| 10 | DJI Mic 3 実機 残り（P0-10〜P0-15） | §5.5 | #3 | ⬜ 未実施 |
| 11 | Model Runner と LLM スループット（R-21） | §12.1, §18.2 | #13 | ⬜ 未実施 |
| 12 | 文字起こし性能（R-4） | §10.6, §21.2 | #22 | ⬜ 未実施 |

## 0. 記録の規約

- **測定日・コマンド・生の出力**をそのまま貼る。要約した数値だけを書かない
- PASS / FAIL の**判断**を必ず書き、根拠が測定値のどれなのかを示す
- 代替手段で測った場合は**その限界**を明記する
- 測定には `poc/tmo`（ホスト側の有界タイムアウト）を必ず使う。
  素で `docker exec` を打つと §2.3 の症状で操作者のシェルごと無応答になる

---

## 1. ホスト環境

測定日 **2026-09-12**。

| 項目 | 実測 |
|---|---|
| 機種 / macOS | Mac16,11 (Apple M4 Pro 14 core) / macOS 26.6.2 |
| Docker Desktop | **4.90.0** |
| Docker Engine | 29.7.2 |
| Docker Compose | 5.5.1 |
| 仮想化バックエンド | Apple Virtualization Framework（`UseVirtualizationFramework = True`） |
| ファイル共有実装 | 既定は **VirtioFS**。本チケットで **VirtioFS では動かない**と判明（§2） |
| File sharing ディレクトリ | `FilesharingDirectories` 未設定 ＝ 既定（`/Users` `/Volumes` `/private` `/tmp` `/var/folders`） |
| 検証用イメージ | `debian@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171` |

§3.1 の Engine / Compose の値と一致する。

---

## 2. ファイル共有実装の決定的な差（R-1）

### 2.1 結論

**Docker Desktop 4.90.0 の VirtioFS は、物理 USB マスストレージを bind mount 越しに読めない。
gRPC FUSE なら完全に読める。**

同じホスト・同じデバイス・同じコマンドで、共有実装だけを変えた結果:

| 測定 | VirtioFS | gRPC FUSE |
|---|---|---|
| コンテナ内の `mount` | `virtiofs1 ... type virtiofs` | `grpcfuse ... type fuse.grpcfuse` |
| `/Volumes` ツリーの `ls` | ❌ ハング、または `ENOTDIR` | ✅ 全ボリュームを列挙 |
| DJI を直接 bind mount して `ls` | ❌ `cannot open directory: Not a directory` | ✅ 列挙できる |
| DJI 配下のサブディレクトリ | ❌ 不可 | ✅ 辿れる |
| ファイルの `stat` | ❌ 不可 | ✅ サイズを正確に取得 |
| ファイルの読み取り | ❌ 0 バイト | ✅ **全バイト読める。SHA-256 がホストと一致** |

### 2.2 VirtioFS での症状

`-v "/Volumes/DJIMIC3:/x:ro"` で bind mount したとき:

```text
mount            : virtiofs1 on /x type virtiofs (ro,nosuid,nodev,relatime,ignore_atime,no_xattr)
ls -ld /x        : drwx------ 1 root root 0 Dec 26  1970 /x     ← ディレクトリだと主張する
stat -c '%F' /x  : directory                                     ← statx も成功する
ls -la /x        : ls: cannot open directory '/x': Not a directory   ← opendir が ENOTDIR
ls -f /x         : 同上
find /x -maxdepth 1 : find: '/x': Not a directory
stat /x/.Trashes : stat: cannot statx '/x/.Trashes': Not a directory
cat /x/<既知のファイル> | wc -c : 0
```

`stat` はディレクトリだと答えるのに、**中へ入る操作はすべて `ENOTDIR` を返す**矛盾したビュー。
`--user 0:0`（root）でも同じ。

サブディレクトリを直接 bind mount しようとすると、**コンテナ起動前に失敗する**:

```text
$ docker run --rm -v "/Volumes/DJIMIC3/.Trashes/501/TX_MIC001_20260912_120950:/x:ro" debian ...
docker: Error response from daemon: failed to create task for container: ...
  failed to fulfil mount request:
  open /host_mnt/Volumes/DJIMIC3/.Trashes/501/TX_MIC001_20260912_120950: not a directory
```

→ **§5.6 案 B（個別パスを bind mount する）は VirtioFS では成立しない。**

### 2.3 最初に観測された、より重い失敗モード

**最初の `/Volumes` ツリー bind mount は、Docker Desktop の実行基盤ごと固めた。**

1. `docker run -d -v /Volumes:/host-volumes:ro debian sleep infinity` → 成功、`docker exec ... id` も成功
2. `docker exec ... ls /host-volumes` → **無応答**。コンテナ内の `timeout(1)` でも中断できない
3. 以降、**bind mount を一切使わない `docker run --rm debian echo` すら起動しなくなった**
4. 詰まったコンテナは `docker rm -f` でも落ちない
   → `could not kill container: tried to kill container, but did not receive an exit event`
5. daemon API（`docker version` / `docker ps`）は応答し続け、既存の他コンテナも動き続ける
6. **回復には Docker Desktop の再起動が必要だった**

**SPEC §22 R-1 は失敗モードを「`/host-volumes` に現れない」と想定していたが、実際は
「ハングし、Docker Desktop が新規コンテナを起動できなくなる」というより重い失敗モードだった。**
無人常駐を前提とするアプリにとって、これは検出も復旧もできない状態である。

（再起動後は同じ操作でハングせず `ENOTDIR` を返すようになった。ハングの再現条件は未特定。）

### 2.4 原因の切り分け

同じ `/Volumes` 配下にディスクイメージを置いて比較し、消去法で絞った。

| 対象 | FS | Protocol | Device Node | mount flags | VirtioFS での結果 |
|---|---|---|---|---|---|
| `VDPOC`（DMG） | exFAT | Disk Image | `/dev/disk5s1` | `fskit` | ✅ 完全成功 |
| `VDFAT32`（DMG） | FAT32 | Disk Image | `/dev/disk6s1` | `fskit` | ✅ 完全成功 |
| `VDSF`（DMG・パーティション無し） | FAT32 | Disk Image | `/dev/disk5` | `fskit` | ✅ 完全成功 |
| **`DJIMIC3`（実機）** | FAT32 | **USB** | `/dev/disk4` | `fskit` | ❌ 全滅 |

| 仮説 | 判定 | 根拠 |
|---|---|---|
| FSKit（macOS 26 のユーザー空間 FS）が原因 | ❌ 否定 | DMG 3 本も `fskit` でマウントされるが全て動く |
| FAT32 / msdos が原因 | ❌ 否定 | FAT32 の DMG が動く |
| パーティションマップ無し（superfloppy）が原因 | ❌ 否定 | `hdiutil create -layout NONE` で作った `/dev/disk5` の DMG が動く |
| 権限 / uid が原因 | ❌ 否定 | `--user 0:0`（root）でも同じ `ENOTDIR` |
| `Macintosh HD -> /` の symlink が原因 | ❌ 否定 | DJI 不在なら `/Volumes` ツリー全体が symlink 込みで動く（§5） |
| Docker 起動時点で既にマウントされていたこと | ❌ 否定 | 物理抜き差し後も `readdir` は失敗のまま（`statx` だけは回復した） |
| **物理 USB マスストレージであること** | ✅ **これが原因** | `Protocol: Disk Image` は全て成功、`Protocol: USB` だけ失敗。共有実装を gRPC FUSE に変えると成功する |

**未切り分け**: 手元に DJI 以外の USB デバイスが無いため、「物理 USB 全般」か「DJI Mic 3 固有」かは未確定。
どちらであっても結論（VirtioFS を使えない）は変わらない。

### 2.5 代替証拠の限界

当初 #2 は「実機を待たずにディスクイメージで代替検証する」計画だった。**結果として、その代替は
成立しないことが判明した。**ディスクイメージは exFAT / FAT32 / superfloppy のいずれでも完全に動作し、
**実機だけが失敗する。**

> **教訓**: ディスクイメージの成功は USB マスストレージの成功をまったく保証しない。
> 逆向きの含意（DMG で失敗すれば強い否定証拠）だけが成り立つ。

このチケットを実機と同時に実施できたのは幸運だった。DMG だけで PASS と判定していたら、
Phase 1 以降のすべてを誤った前提の上に積み上げていた。

---

## 3. ホットプラグ伝播と実機読み取り（gRPC FUSE 上）

常駐コンテナ（`docker run -d -u 1000:1000 -v /Volumes:/host-volumes:ro`）を**一度も再起動せずに**測定した。

### 3.1 P0-6 ホットプラグ伝播 — PASS

| 事象 | コンテナが検知するまで |
|---|---|
| デバイスの取り外し | **1 秒以内**（`GONE after 1s`） |
| デバイスの再接続 | **9 秒以内**（`VISIBLE after 9s`） |

再接続時には、**接続中に新しく録音されたフォルダ `TX_MIC001_20260912_163444` も同時に見えた。**

```text
--- ls -la /host-volumes
drwx------ 1 1000 1000    0 DJIMIC3
lrwxr-xr-x 1 1000 1000    1 Macintosh HD -> /
--- ls -la /host-volumes/DJIMIC3
drwx------ 1 1000 1000 262144 .Spotlight-V100
drwx------ 1 1000 1000 262144 .Trashes
drwx------ 1 1000 1000 262144 .fseventsd
drwx------ 1 1000 1000 262144 TX_MIC001_20260912_163444
```

> **§10.1 への含意**: `poll_interval_seconds: 5`（既定）で十分。取り外しは 1 秒、再接続は 9 秒で
> ホスト側に反映されるため、5 秒周期の `scandir` が取りこぼす余地はない。

### 3.2 P0-7 / P0-9 コンテナからの読み取り — PASS

uid 1000（non-root）で、実機の WAV を**全バイト読めた**。

```text
$ docker exec vd-poc sh -c 'ls -la /host-volumes/DJIMIC3/TX_MIC001_20260912_163444'
-rwx------ 1 1000 1000 856456 TX00_MIC001_20260912_163444_orig.wav

$ 各ファイルを stat して全部読む
TX00_MIC001_20260912_163444_orig.wav   stat=856456  read=856456  magic=RIFF  ✅一致
```

**データ完全性の検証**（共有層越しで 1 バイトも壊れていないこと）:

```text
ホスト   : 90e3d77fc95f79d50253506358fe1aa05ffd003f437f0785f6d77debb88edd80
コンテナ : 90e3d77fc95f79d50253506358fe1aa05ffd003f437f0785f6d77debb88edd80
```

- ホストの uid 501 が、コンテナ内では uid 1000 に写る（`noowners` マウントのため所有権は無視される）
- `stat` が `UNKNOWN(1000)` と出るのは、コンテナ内の `/etc/passwd` に uid 1000 が無いためで、読み取りには影響しない
- **§18.3 の「non-root uid 1000」方針は変更不要**

`.Trashes` 配下のファイルだけは `stat` はできても読み取りが 0 バイトになる。macOS がゴミ箱を
保護しているためで、共有実装の制約ではない（`.Trashes` 外のファイルは読める）。
**§10.2 で `.Trashes` を走査対象から外す根拠になる。**

---

## 4. マウントモードの効き（ND-23）— PASS

> **v5.2 で P0-8 の定義が変わったため、本節から `P0-8` の名を外した**（#93）。
> 本節が測ったのは「**コンテナから**デバイスへ書けるか」であり、
> **v4.0 でホスト側 Helper へ移行した後、コンテナは `/Volumes` を一切マウントしない**（N-3）。
> 現行の P0-8 は「`mount` が read-only で `heartbeat.json` の `mount_readonly` が `true` になり、
> **両者が一致すること**」である（§10）。**測定値はそのまま残す** — ND-23 が前提にしている
> 「`ro` なら OS レベルで削除不可」の実証としては、いまも有効だからである。

### 4.1 `ro` マウントは書き込みを拒む（安全ロック 2 の実証）

**実機の上で**、`ro` マウントのコンテナから書き込みを試みた。

```text
$ touch /host-volumes/DJIMIC3/TX_MIC001_20260912_163444/should_fail
touch: cannot touch '...': Read-only file system          exit=1
$ rm /host-volumes/DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC001_20260912_163444_orig.wav
rm: cannot remove '...': Read-only file system            exit=1
$ ls -la <同ディレクトリ>
-rwx------ 1 1000 1000 856456 TX00_MIC001_20260912_163444_orig.wav   ← 無傷
```

**§14.2 の安全ロック 2（`DJI_MOUNT_MODE=ro`）が、実機の上で OS レベルに効いている。**
ND-23 が前提にしている「`ro` なら OS レベルで削除不可」は実証された。

### 4.2 `rw` なら削除できる（ND-23 の裏返し）

**実機では行わない**（#2 の非スコープ）。使い捨ての FAT32 ディスクイメージ上で確認した。
**実機での削除は Phase 7（#39）まで行わない。**

```text
touch OK / rm OK / DELETE_OK / WAV_DELETE_OK
```

uid 1000 で FAT32 上のファイルを作成・削除できる。

---

## 5. 非 DJI ボリュームと symlink（R-19 / R-20）— PASS

DJI を取り外した状態で `/Volumes` ツリー全体を bind mount した結果（VirtioFS 上）:

```text
--- ls -la /host-volumes
drwxr-xr-x 5 root root  160 .
lrwxr-xr-x 1 1000 1000    1 Macintosh HD -> /
drwx------ 1 1000 1000    0 VDFAT32
drwx------ 1 1000 1000 4096 VDPOC
--- readlink "/host-volumes/Macintosh HD"     : /
--- readlink -f "/host-volumes/Macintosh HD"  : /
--- ls "/host-volumes/Macintosh HD/"          : bin boot dev etc home host-volumes lib media ...
TREE_READDIR_OK
```

（gRPC FUSE でも同じ結果。`Macintosh HD` の見え方は共有実装に依存しない。）

### 5.1 R-19 の直接証拠（§14.1.1 の根拠）

- `Macintosh HD` は **コンテナ内でも symlink のまま見える**
- 辿ると **コンテナのルート `/`** に出る（ホストの起動ディスクではない）
- **その中に `host-volumes` 自身が含まれる**ため、
  `/host-volumes/Macintosh HD/host-volumes/Macintosh HD/…` と**無限に再帰できる**

したがって §14.1.1 を `os.path.realpath()` 解決後の判定にした改訂（v3.3 / #1）は**必須である**ことが
実証された。字句判定（`device.path in target.parents`）だけでは、`device.path` が
`/host-volumes/Macintosh HD` のときに封じ込めを満たしてしまう。

§5.4 の「ボリュームルートが symlink ならスキップ」（v3.3 / #1）も、この経路を入口で塞ぐ。

### 5.2 R-20 の判断

- `/Volumes` に常駐する非 DJI エントリは `Macintosh HD`（symlink）のみだった
- `Macintosh HD` は §5.4 の symlink 除外で落ちる。**`device.exclude_volumes` の既定値を変える必要は無い**
- ただし §2.3 で「1 個の不良ボリュームが Docker 全体を固めうる」ことが分かったため、
  **拒否リストより許可リストのほうが防壁として強い**。`device.include_volumes` の新設を別途起票する

---

## 6. DJI Mic 3 実機の実測（P0-2〜P0-5）

### 6.1 デバイス（P0-2）

```text
$ diskutil info /Volumes/DJIMIC3
   Device Node:               /dev/disk4          ← パーティションマップ無し（superfloppy）
   Device / Media Name:       Mic Tx
   Volume Name:               DJIMIC3             ← 出荷時は "NO NAME"。ユーザーが改名
   File System Personality:   MS-DOS FAT32
   Protocol:                  USB
   Volume Total Space:        30.0 GB (30048256000 Bytes)
   Removable Media:           Removable
$ /sbin/mount | grep DJIMIC3
/dev/disk4 on /Volumes/DJIMIC3 (msdos, local, nodev, nosuid, noowners, noatime, fskit)
```

| # | SPEC の記述 | 実測 | 判定 |
|---|---|---|---|
| a | #2 / #3 が「**exFAT** ストレージ」前提 | **MS-DOS FAT32** | **誤り** |
| b | §5.1「**32 GB** の内蔵ストレージ」 | 公称 32 GB / **使用可能 30.0 GB** | 要追記（残容量表示 §17.2 / D-6 の基準） |
| c | 記載なし | ボリューム名の出荷時既定は **`NO NAME`** | **要追記。**§10.4 の `session_key` が `NO NAME:YYYYMMDD` になり、**名前の無い他の USB メモリと衝突する**。改名を推奨する運用記述が必要 |

### 6.2 録音ファイル（P0-3 / P0-4 / P0-5）

測定した録音 2 本（`.Trashes` に残っていた 1 本 + 本チケットで新規に録った 1 本）。

§5.2 の正規表現との突き合わせ:

```text
folder 'TX_MIC001_20260912_120950'              FOLDER_RE=MATCH {'mic':'MIC001','date':'20260912','time':'120950'}
  file 'TX00_MIC001_20260912_120950_orig.wav'   FILE_RE=MATCH   {'tx':'TX00','mic':'MIC001',...,'orig':'_orig'}
  file '._TX00_MIC001_20260912_120950_orig.wav' FILE_RE=NO      ← AppleDouble
folder 'TX_MIC001_20260912_163444'              FOLDER_RE=MATCH
  file 'TX00_MIC001_20260912_163444_orig.wav'   FILE_RE=MATCH
```

- **フォルダ名・ファイル名とも §5.2 の正規表現に一致する** ✅（P0-3 PASS）
- **`TX00` が実在する**。§5.5 の境界値テストの想定どおり
- **観測された Variant は `_orig` のみ**。denoised（非 `_orig`）は 2 本とも生成されなかった。
  §5.3 の「orig のみ → 1 Part・1 Variant」分岐が実運用のかたち（P0-4）
- ホストから WAV を読める（P0-5 PASS）

WAV ヘッダの実測（新しい録音・856,456 B）:

```text
chunks : fmt (16), bext(602), iXML(1,092), cue (28), PAD (30,978), data(823,680)
fmt    : tag=0x0001 (PCM) ch=1 rate=48,000Hz byterate=144,000 B/s align=3 bits=24
data   : header=32,776 B / data=823,680 B → 5.72 秒
```

| # | SPEC §5.1 の記述 | 実測 | 判定 |
|---|---|---|---|
| d | 「実測値は **192,000 B/秒**（48 kHz / **32 bit float** / モノラル）」 | **48 kHz / 24 bit / mono PCM = 144,000 B/秒**（録音 2 本とも） | **誤り** |
| e | 「1 時間あたり（1 Variant）約 691 MB」 | **約 518 MB** | **誤り**（d の連鎖） |
| f | 「本体 32 GB での録音可能時間 両 Variant で約 23 時間」 | 30.0 GB ÷ 1.037 GB/h ＝ **約 29 時間** | **誤り**（d の連鎖） |
| g | 記載なし | WAV が **Broadcast Wave**（`bext` / `iXML` / `cue` / `PAD`）。ヘッダ約 32 KB | 要追記。変換には影響しないが `iXML` に機材情報が入る |

> **注**: §5.1 の「形式: 48 kHz / 24 bit **または** 32 bit float」という記述自体は誤りではない。
> 誤っているのは **「実測値は 192,000 B/秒」と断定している点**と、それを前提にした容量見積り表である。
> 本機の現在の設定では 24 bit が出る。

### 6.3 macOS がデバイス上に作るファイル（§10.2 の走査に影響）

| 名前 | 正体 | 影響 |
|---|---|---|
| `._<元の名前>` | **AppleDouble**（拡張属性の受け皿）。FAT/exFAT に書くと macOS が作る | §5.2 の正規表現に一致しないため、**ファイルごとに `WARN unparsable_filename` が鳴る**。`.` 始まりは黙って無視する規定が要る |
| `.Spotlight-V100` | Spotlight の索引 | 走査対象から外す |
| `.fseventsd` | FSEvents のログ | 同上 |
| `.Trashes` | ゴミ箱。Finder で消すとここへ移る | **`.Trashes` 配下の録音を走査してはならない**（ユーザーが意図的に消したもの）。読み取りも macOS に拒否される |

いずれも DJI 実機とディスクイメージの両方で観測した。

---

## 7. ファイル共有実装の性能比較

同一ホスト・同一ワークロードで、共有実装だけを変えて測定した
（`~/vd-bench` を `rw` で bind mount し、コンテナ内から実行）。

| 項目 | VirtioFS | gRPC FUSE | 倍率 |
|---|---:|---:|---:|
| 小ファイル 2000 個 作成 | 418 ms | 1,562 ms | **3.7×** |
| 小ファイル 2000 個 `stat` | 609 ms | 1,207 ms | **2.0×** |
| 小ファイル 2000 個 read | 568 ms | 1,388 ms | **2.4×** |
| 連続書き込み 256 MiB | 141 ms | 662 ms | **4.7×** |
| 連続読み取り 256 MiB | 91 ms | 531 ms | **5.8×** |
| `readdir`（2000 件）×20 | 47 ms | 82 ms | **1.7×** |
| **合計** | **1,874 ms** | **5,432 ms** | **2.9×** |

スループット換算: 連続読み **2.8 GB/s → 482 MB/s**、連続書き **1.8 GB/s → 387 MB/s**。

**VoiceDock 自身への影響は無視できる。**bind mount 越しに扱うのは 1 日あたり DJI からの約 8.3 GB の
読み取りと Obsidian への数百 KB の書き込みだけで、律速は whisper.cpp の CPU である。
482 MB/s でも 8.3 GB は 17 秒。

**問題は、この設定が Docker Desktop 全体のグローバル設定である**ことだった（§8）。

---

## 8. アーキテクチャの決定 — §5.6 案 A（ホスト側 Helper）を採用

### 8.1 選択肢

§2 の結果から、取りうる道は 2 つだけだった。

| | 案 1: gRPC FUSE を必須前提にする | **案 2: §5.6 案 A（ホスト側 Helper）** |
|---|---|---|
| VoiceDock の実装量 | 0（設定と `doctor` 検査のみ） | Helper + 削除キュー + 追加テスト |
| VoiceDock の性能 | 影響なし | 影響なし |
| **他プロジェクトへの影響** | **bind mount が 2〜6× 遅くなる**（§7） | **なし** |
| ホストに入れるもの | Docker Desktop + Obsidian のみ | **＋ Helper と LaunchAgent** |
| 1 日のホスト書き込み | 1.8 GB | **約 10 GB**（原本コピー 8.3 GB が加算） |
| 削除の実行主体 | `cleaner.py`（§20.4 の 23 テストが守る） | **ホストの Helper**（新しい信頼境界） |
| 設定を戻されたとき | **VoiceDock が壊れる**（最悪 §2.3 の Docker 固着） | 影響なし |

### 8.2 決定

**案 2（ホスト側 Helper）を採用する。**

決定理由: ファイル共有実装は **Docker Desktop 全体のグローバル設定**であり、VoiceDock のために
同じ Mac の他のすべてのプロジェクトへ 2〜6 倍の I/O 劣化を恒久的に課すことになる。
VoiceDock は「刺したら勝手に動く」常駐アプリであり、その代償を常時払い続ける形になる。

### 8.3 この決定が要求する設計変更

| 項目 | 変更 |
|---|---|
| コンテナが見るもの | `/host-volumes`（`/Volumes` ツリー）→ **ホストのローカルディレクトリ（inbox）** |
| デバイス検出・走査 | コンテナ内 → **ホストの Helper**（launchd `StartOnMount`） |
| 原本の扱い | デバイスから直接ストリーム変換（§10.5）→ **Helper が inbox へコピーしてから変換** |
| 削除の実行 | `cleaner.py` が直接 unlink → **コンテナが削除キューを書き、Helper が実行** |
| 安全ロック 2 | `DJI_MOUNT_MODE=ro`（OS レベル）→ **別の機構が必要**（§14.2 の再設計） |
| §3.3 の前提 | 「ホストへ Python を入れない」→ Helper を shell か単体バイナリで書く |

**§14 の安全設計に構造変更が入る。**§14.1 の削除条件の判断はコンテナ内（テスト済みコード）に
残せるが、実行がホストへ出るため、§20.4 の ND-01〜ND-23 はキュー経路を含む形へ拡張が必要になる。
これは別チケットで設計する。

---

## 9. Phase 0 ハードゲート判定

| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| P0-1 | 送信機を USB 接続すると `/Volumes/<名前>` に現れる | ✅ PASS | §6.1 |
| P0-2 | Volume 名を記録する | ✅ PASS | §6.1。出荷時 `NO NAME` → `DJIMIC3` へ改名済み |
| P0-3 | フォルダ・ファイル名が §5.1 の規則と一致する | ✅ PASS | §6.2。正規表現が両方 MATCH |
| P0-4 | `_orig` と非 `_orig` の両方が生成されるか | ⚠ 片方のみ | §6.2。録音 2 本とも `_orig` のみ。設定依存の可能性（#3 / P0-13 で再確認） |
| P0-5 | ホストから WAV を読み取れる | ✅ PASS | §6.2 |
| P0-6 | **コンテナ起動後の接続が `/host-volumes` に現れる** | — 廃止 | §3.1 の測定は残す。**v4.0 でコンテナは `/Volumes` をマウントしなくなった**（N-3） |
| P0-7 | コンテナから WAV を読み取れる | — 廃止 | §3.2。同上。**読むのはホストの Helper である**（§10.1） |
| P0-8 | **`mount` が read-only で `heartbeat.json` の `mount_readonly` が `true`。両者が一致する** | ⬜ 未実施 | #3。**v5.2 で定義が変わった**（旧定義「コンテナから削除できる」の測定は §4.2） |
| P0-9 | non-root（uid 1000）で読めるか | — 廃止 | §3.2。**Helper はユーザー権限で動く**ので、コンテナの uid は無関係になった |
| P0-10〜P0-15 | — | ⬜ 未実施 | #3 |

**Phase 0 は通過していない。**P0-8 と P0-10〜P0-15 が #3 待ちである。

**「廃止」は「不合格」ではない。**§8 の設計変更（コンテナはデバイスを直接見なくなる）により、
P0-6 / P0-7 / P0-9 が問うていた前提そのものが無くなった。**測定値と本文は §3 にそのまま残す** —
なぜその経路を捨てたのかは、測った結果を見なければ分からないからである。
**判定欄だけを「廃止」にする理由は、✅ のままだと現行設計が通っていると読めるためである。**

**この時点で §21.2 Phase 1 以降（`src/` の実装）へ進んではならない。**

> **上の一文は v4.0 当時の判断であり、実際には守られていない。**Phase 1〜6 の実装は
> P0-8 / P0-10〜P0-15 を残したまま先行した（`src/` と `helper/` が揃っている）。
> **記録として残す** — 何を措いて進んだのかが分からなくなるほうが危ういからである。
>
> **いま実際に握られているハードゲートは §21.1（Phase 7 / 削除の有効化）である。**
> こちらは ND-01〜ND-31 と E2E-01〜E2E-12 の全件 PASS を要求しており、
> **ND は全件緑だが E2E は 1 件も実施していない**ので、#38 / #39 は着手していない。
> 実機で測れば **P0-8 と E2E が同時に埋まる**（#3 → #35）。

---

## 10. DJI Mic 3 実機 残り（#3 で記入）

**`./scripts/probe.sh` を実機で 1 回叩き、出力をこの節へそのまま貼る。**
§0 の記録の規約どおり、要約した数値だけを書かない。

```bash
./helper/install.sh              # #53。まだなら
# DJI を USB 接続する（launchd の StartOnMount で ingest が走る）
./scripts/probe.sh > /tmp/probe-ro.md
```

**P0-8 は probe が機械判定する。**`mount` の read-only と `heartbeat.json` の
`mount_readonly` が食い違えば `✗` を出し、**終了コードが非 0 になる。**
一致しないなら実装のバグであり、**その場で採り直しても直らない。**

| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| P0-8 | `MOUNT_MODE=ro` で `mount` が read-only、`heartbeat.json` が `mount_readonly: true`。**両者が一致する** | ⬜ 未実施 | |
| P0-8b | `MOUNT_MODE=rw` で read-write になり `mount_readonly: false` になる | ⬜ 未実施 | |
| P0-10 | 送信機 2 台の見え方。2 個なら `inventory.json` の `devices` が 2 エントリ | ⬜ 未実施 | |
| P0-11 | 録音中ファイルの mtime の進み方。`STABILITY_*` の既定が実挙動に合うか | ⬜ 未実施 | |
| P0-12 | 受信機（RX）側にもストレージが見えるか（任意。R-7 へ記録するだけ） | ⬜ 未実施 | |
| P0-13 | `_orig` 保存をオフにできるか。**denoised が生成される設定があるか**（R-28） | ⬜ 未実施 | |
| P0-14 | バッテリー交換・充電で中断したときのファイルの分かれ方（§13.4 の Block） | ⬜ 未実施 | |
| P0-15 | 64 ファイルでの ingest 1 回の所要時間。`StartInterval`（300 秒）で間に合うか | ⬜ 未実施 | |

### 10.1 `ro` での採取

⬜ 未実施。`./scripts/probe.sh` の出力を貼る。

### 10.2 `rw` での採取（P0-8b）

⬜ 未実施。`helper.conf` を `MOUNT_MODE=rw` にして再接続し、採り終えたら **`ro` へ戻す。**

> **`rw` にしても削除は起きない。**ロック 2-A（`voicedock-reaper` が未配置）が残っており、
> **削除できるプログラムが存在しない**（§14.2）。削除の有効化は #39 の運用判断である。

---

## 11. Model Runner と LLM スループット（#13 で記入）

⬜ 未実施。

```bash
docker desktop enable model-runner && docker model status
docker model pull <§18.2 のタグ>
./scripts/fetch-models.sh
make up
docker compose exec voicedock env | grep VOICEDOCK_LLM   # 注入の確認
```

| # | 項目 | 実測 |
|---|---|---|
| `docker model status` | running か | ⬜ |
| モデルタグ | 実際に pull できたタグ。**thinking 系でないこと**（§12.3） | ⬜ |
| `VOICEDOCK_LLM_URL` | **実際の形**（`/engines/v1` の有無）。Compose 5.5.1 での注入 | ⬜ |
| `response_format` | `{"type":"json_object"}` が受理されるか | ⬜ |
| 20,000 字 1 チャンク | latency（秒） | ⬜ |
| 18 チャンク換算 | **30 分以内か**（§21.2 Phase 3） | ⬜ |
| ホスト RAM | `docker stats` の実測（§4.3 の試算 18 GB の検証） | ⬜ |

**判定は `./scripts/perf-report.sh --llm` でも出せる**（`llm_completed` ログから）。

---

## 12. 文字起こし性能（#22 で記入）

⬜ 未実施。

```bash
docker compose logs voicedock | ./scripts/perf-report.sh
```

| # | 項目 | 実測 |
|---|---|---|
| `rtf` | Part ごとの real-time factor | ⬜ |
| `speech_ratio` | VAD 有効時 / 無効時 | ⬜ |
| 1 日分（32 Part） | **実 `elapsed_s` の合計と外挿。8 時間以内か**（§21.2 Phase 2） | ⬜ |
| `threads` | 4 / 6 / 7 の比較（Docker VM は 7 CPU 割当） | ⬜ |
| 採用モデル | 未達なら `large-v3-turbo-q5_0` → `medium-q5_0` → `small-q5_1` | ⬜ |
| 幻覚（R-15） | 無音区間で VAD がどの程度抑制するか（定性） | ⬜ |
