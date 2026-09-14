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
| 12 | 文字起こし性能（R-4） | §10.6, §21.2 | #22 | ✅ **PASS**（16 時間 → 6.95 時間。余裕 13%。§12.6） |

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
| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| P0-8 | `MOUNT_MODE=ro` で `mount` が read-only、`heartbeat.json` が `mount_readonly: true`。**両者が一致する** | ✅ **PASS** | §10.1。`probe.sh` が機械判定し EXIT=0。**ただし一致は間欠的に崩れていた** — §10.1.2（#107 で修正） |
| P0-8b | `MOUNT_MODE=rw` で read-write になり `mount_readonly: false` になる | ✅ **PASS** | §10.2。`probe.sh` が機械判定し EXIT=0。**削除は 2 つのロックで止まったまま** |
| P0-10 | **送信機 1 台での見え方。**`inventory.json` の `devices` が 1 エントリ | ✅ **PASS** | §10.1。`probe.sh` の P0-10 節 |
| P0-10b | 送信機 2 台の見え方 | ⬜ **実施しない**（機材なし） | §10.3。**MVP は 1 台前提**（§22 R-6） |
| P0-11 | 録音中ファイルの mtime の進み方。`STABILITY_*` の既定が実挙動に合うか | ⚠ 静止のみ | §10.1。**録音中のファイルでは未検証** |
| P0-12 | 受信機（RX）側にもストレージが見えるか（任意。R-7 へ記録するだけ） | ⬜ 未実施 | |
| P0-13 | `_orig` 保存をオフにできるか。**denoised が生成される設定があるか**（R-28） | ✅ **denoised 無し** | §10.1。2 件とも `_orig` のみ |
| P0-14 | バッテリー交換・充電で中断したときのファイルの分かれ方（§13.4 の Block） | ⬜ 未実施 | |
| P0-15 | 64 ファイルでの ingest 1 回の所要時間。`StartInterval`（300 秒）で間に合うか | ⚠ 2 件で 0.84 秒 | §10.1。64 ファイルは未検証 |

### 10.0 TCC — リムーバブルボリュームへのアクセス（**最初に踏む壁**）

**DJI を接続しても `volume_skipped reason=no DJI recordings` が出続けた。**
実際は macOS の TCC がボリュームの列挙を拒んでいた。

```text
$ mount | grep -i djimic3
/dev/disk4 on /Volumes/DJIMIC3 (msdos, local, nodev, nosuid, noowners, noatime, fskit)

$ [ -r /Volumes/DJIMIC3 ] && echo "-r: true"
-r: true                                    ← 規則 4 は通る

$ find /Volumes/DJIMIC3 -maxdepth 1 >/dev/null 2>&1; echo $?
1                                           ← 実際は列挙できない

$ ls -la /Volumes/DJIMIC3
ls: /Volumes/DJIMIC3: Operation not permitted
```

**`[ -r ]` は TCC の拒否を見抜けない**（`access(2)` は成功し `opendir(3)` で初めて `EPERM`）。
そのため規則 5 の glob が空になり、**権限が無いのに「録音が無い」と報告していた。**
v5.12（Z-1 / Z-2）で §3.4(7) を足し、理由を区別するようにした。

**ターミナルへの許可と LaunchAgent への許可は別である。**フルディスクアクセスに
ターミナルアプリを追加した後の実測:

```text
（ターミナルから手動実行）
2026-09-14T00:43:18+09:00 INFO  device_detected name=DJIMIC3
2026-09-14T00:43:19+09:00 INFO  remounted_readonly name=DJIMIC3
2026-09-14T00:43:19+09:00 INFO  scanned name=DJIMIC3 files=2 candidates=2
2026-09-14T00:43:19+09:00 INFO  copied relpath=TX_MIC001_20260912_163444/TX00_MIC001_20260912_163444_orig.wav bytes=856456
2026-09-14T00:43:19+09:00 INFO  copied relpath=TX_MIC001_20260912_163444/TX00_MIC002_20260913_233420_orig.wav bytes=2963176
2026-09-14T00:43:19+09:00 INFO  ingest_finished devices=1

（launchctl kickstart -k gui/$UID/com.voicedock.ingest）
2026-09-14T00:44:52+09:00 INFO  volume_skipped name=DJIMIC3 reason=volume not listable — macOS のプライバシー設定を確認してください（§3.4(7)）
2026-09-14T00:44:52+09:00 INFO  ingest_finished devices=0
```

> **`voicedock-ingest` はシェルスクリプトである。**TCC の許可対象としてスクリプトを
> 登録できるかは未確認。**Helper の配備方式に関わる論点**なので #95 に残す。

**副作用として `heartbeat.json` が上書きされる。**列挙できない実行でも Helper は
`mount_readonly: false` を書くため、**直前の成功した採取が 300 秒以内に消える。**
`probe.sh` を「接続したらまず叩く」と定めているのはこのためである（§10.2 の註記）。

### 10.1 `ro` での採取（P0-8 / P0-10 / P0-11 / P0-13 / P0-15）

**`./scripts/probe.sh` の出力**（2026-09-14 00:43:19 の ingest 直後。EXIT=0）:

```text
## P0-8 マウントモードと heartbeat の一致

- `helper.conf` の `MOUNT_MODE` : `ro`
- `heartbeat.json` の `mount_readonly` : `true`
- `heartbeat.json` の `updated_at` : `2026-09-14T00:43:19+09:00`

| デバイス | `mount` | 読み取り専用 |
|---|---|---|
| `DJIMIC3` | `/dev/disk4 on /Volumes/DJIMIC3 (msdos, local, nodev, nosuid, read-only, noowners, noatime, fskit)` | true |

- `mount` からの観測 : `true`（デバイス 1 個の論理積）
- 判定: ✅ **PASS** — 一致した（ロック 2-B が実際に効いている。§14.2）

## P0-10 デバイスの見え方

- `inventory.json` の `devices` : **1 個**

| デバイス | 録音 | 空き容量 | `df -Pk` |
|---|---|---|---|
| `DJIMIC3` | 2 | 30017847296 | `/dev/disk4    29344000 29696  29314304     1%    /Volumes/DJIMIC3` |

## P0-11 安定性判定

- `STABILITY_FAST_PATH_SECONDS` : `60`
- `STABILITY_INTERVAL_SECONDS`  : `3`
- `STABILITY_CHECKS`            : `2`

対象: `/Volumes/DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC002_20260913_233420_orig.wav`

| # | size mtime |
|---|---|
| 1 | `2963176 1789310060` |
| 2 | `2963176 1789310060` |

## P0-13 `_orig` 以外の有無

- `inventory.json` の録音 : **2 件**
- うち `_orig` 以外 : **0 件**
```

**P0-8 の意味**: `voicedock-ingest` は `diskutil` の終了コードを信用せず、`mount` の出力で
読み取り専用を独立確認してから `true` を書く（`_is_mounted_readonly()`）。`probe.sh` は
同じ式を `mount` から**独立に**組み立てて突き合わせる。**一致したということは、
安全ロック 2-B が実機の上で OS レベルに効いていることの実証である**（§14.2）。

**P0-13 の意味**: 2 件とも `_orig` であり、**denoised は 1 件も生成されていない。**
#2 の P0-4（録音 2 本とも `_orig` のみ）と一致する。§5.3 で `_orig` 固定にした代償
（読まなかったファイルがデバイスに溜まり続ける）は、**この設定では現実になっていない。**
§22 R-28 は当面顕在化しない。

**P0-15**: 取り込み済み 2 件の再走査で **0.84 秒**（`candidates=0`）。
`StartInterval`（300 秒）に対しては桁で余裕がある。**ただし 64 ファイルでは未計測。**

### 10.1.1 フォルダ名の日付は中身を縛らない（**新しい発見**）

```text
TX_MIC001_20260912_163444/
  TX00_MIC001_20260912_163444_orig.wav     ← 9/12
  TX00_MIC002_20260913_233420_orig.wav     ← 9/13（同じフォルダ）
```

**フォルダ名は最初の録音の時刻であり、中身の録音日を縛らない。**
`session_key` を**フォルダ名**から導いていたら、9/13 の録音が 9/12 の Daily ノートへ
混ざるところだった。

実装は `started_at=parsed.started_at`（`device.py`）で**ファイル名**の時刻を使っており
（§5.2）、`session.group_parts()` はその `started_at` から `session_key` を作る（§10.4）。
**正しい。**§5.1 に「フォルダ名の日付は中身を代表しない」ことを明記する対象とする。

### 10.1.2 報告される保護状態が実態と合わない（**#107 で起票・修正**）

2026-09-14 18:08 に 6 ファイル（1.4 GB）を追加取り込みした際に判明した。

**デバイスは 18:15:34 以降ずっと `read-only` のままだったが、報告値だけが変動した。**

```
18:15:34  remounted_readonly        -> heartbeat mount_readonly=true
18:20:35  remounted_readonly        -> heartbeat mount_readonly=true
18:25:36  remount_readonly_failed   -> heartbeat mount_readonly=FALSE   ← デバイスは ro
18:30:47  remount_readonly_failed   -> heartbeat mount_readonly=FALSE   ← デバイスは ro
18:35:49  remounted_readonly        -> heartbeat mount_readonly=true
```

18:35:06 の突き合わせ:

```
mount           : /dev/disk4 on /Volumes/DJIMIC3 (msdos, ..., read-only, ..., fskit)
heartbeat.json  : "mount_readonly": false
inventory.json  : "mount_readonly": false
```

**約 10 分間、§14.2 のロック 2-B が読む値が偽だった。**

macOS の unified log に 2 種類の失敗が出ており、**どちらも同じ 1 行に潰れていた。**

```
18:08:22  unable to unmount /dev/disk4 (status code 0x00000010)   ← EBUSY。デバイスは rw のまま
18:30:37  disk unmount approval, dissented, status = 0xF8DA0008   ← 拒否。デバイスは ro のまま
```

原因は `remount_readonly()` が**「既に読み取り専用か」を見ずに毎回 unmount していた**こと。
読み取り専用のボリュームは unmount を拒否されるため、**成功した直後の実行が必ず「失敗」を
報告する。**呼び手が**試行の成否から** `mount_readonly` を決めていたため、そのまま state
ファイルへ入った。今日 1 日で 34 サイクル、うち 3 回が失敗。

> **`probe.sh` は 18:11 の時点では ✅ PASS と答えた。**そのときデバイスは本当に rw で、
> heartbeat も `false` だったので**一致していた**。`probe.sh` が見るのは一致であり、
> 「`MOUNT_MODE=ro` が効いたか」は別の問いである。**2 つの問いを混ぜてはならない。**

修正は v5.18（#107）。**観測から書く。試行の成否からではない。**

### 10.2 `rw` での採取（P0-8b）

✅ **PASS**（2026-09-14 22:41）。`helper.conf` を `MOUNT_MODE=rw` にし、**取り出して挿し直した。**

> **挿し直しが要る。**`MOUNT_MODE=rw` は「再マウントしない」だけなので、**既に読み取り専用に
> なっているデバイスはそのまま**である。前の `ro` の状態が残ったままでは測定にならない。

`ingest.log`（**`remount_readonly*` の行が無く、スキャンが 1 秒で終わっている**）:

```text
2026-09-14T22:41:47+09:00 INFO  device_detected name=DJIMIC3
2026-09-14T22:41:47+09:00 INFO  volume_skipped name=Macintosh HD reason=in EXCLUDE_VOLUMES
2026-09-14T22:41:48+09:00 INFO  scanned name=DJIMIC3 files=11 candidates=0
2026-09-14T22:41:48+09:00 INFO  ingest_finished devices=1
```

`probe.sh` の P0-8 節（**要約せずこのまま**）:

```text
## P0-8 マウントモードと heartbeat の一致

- `helper.conf` の `MOUNT_MODE` : `rw`
- `heartbeat.json` の `mount_readonly` : `false`
- `heartbeat.json` の `updated_at` : `2026-09-14T22:41:48+09:00`

| デバイス | `mount` | 読み取り専用 |
|---|---|---|
| `DJIMIC3` | `/dev/disk4 on /Volumes/DJIMIC3 (msdos, local, nodev, nosuid, noowners, noatime, fskit)` | false |

- `mount` からの観測 : `false`（デバイス 1 個の論理積）
- 判定: ✅ **PASS** — `mount` と `heartbeat.json` が一致した
- ロック 2-B: **外れている**（`MOUNT_MODE=rw` の意図どおり。Phase 7 の状態）
```

`doctor` の表示:

```text
[!] Source deletion      DISABLED
                             lock 1  : config.yaml=false, helper.conf=false
                             lock 2-A: voicedock-reaper is NOT installed  <- deletion is impossible
                             lock 2-B: MOUNT_MODE=rw (device mounted read-write)
```

**ロック 2-B を外しても削除は起きない。**残る 2 つが独立に止めている —
とくに**ロック 2-A は「削除できるプログラムが `~/VoiceDock/bin/` に存在しない」**である
（§14.2）。削除の有効化は #39 の運用判断である。

採取後は **`ro` へ戻した。**

#### `probe.sh` の判定文を直した

**一致していることと「ロック 2-B が効いていること」は別の問いである。**
v5.19 までは一致しさえすれば `ロック 2-B が実際に効いている` と書いていた。

**2026-09-14 18:11 の採取では、`MOUNT_MODE=ro` なのにデバイスが書き込み可能なまま
✅ PASS と表示していた**（§10.1.2）。**保護されていないことを保護されていると読ませていた。**

いまは 3 つを区別する:

| 観測 | `MOUNT_MODE` | 表示 |
|---|---|---|
| 読み取り専用 | — | ロック 2-B: **効いている** |
| 書き込み可能 | `rw` | ロック 2-B: **外れている**（意図どおり。Phase 7 の状態） |
| 書き込み可能 | `ro` | ロック 2-B: ⚠ **かかっていない** — 再マウントが成功していない |

### 10.3 送信機 2 台（P0-10b）— **実施しない**

⬜ **2 台目の機材を用意できないため実施しない**（2026-09-14 の判断）。
**MVP は送信機 1 台を前提とする**（§22 R-6）。

**実装は複数台を扱えるように書いてある。**自動テストが担保している:

| テスト | 内容 |
|---|---|
| `tests/unit/test_helper_ingest.py::test_multiple_devices` | 2 ボリュームを置くと `inventory.json` の `devices` が 2 エントリになる |
| `tests/unit/test_probe.py::test_two_devices_are_counted_separately` | probe が 2 デバイスを別々に数える |

**担保されていないのは実機での見え方だけである** — DJI Mic 3 の送信機を 2 台繋いだとき、
**Volume が 2 個に見えるのか 1 個へ統合されるのか**が分からない。

- **2 個に見えるなら**: `session_key` は `device_id` + 日付なのでセッションが分かれ、
  **Daily ノートが 2 枚**になる（設計どおり）
- **1 個へ統合されるなら**: 1 つの Volume の中に両方の録音が入る。`partkey` はファイル名まで
  含むので取り違えは起きないが、**Daily ノートは 1 枚になる**

どちらでも録音が失われることはない。**2 台運用を始めるときに P0-10b を実施すること。**

## 11. Model Runner と LLM スループット（#13 で記入）

✅ **完了。**`models` 長構文での注入、20,000 字でのスループット、`response_format` の
受理、ホスト RAM をすべて実測した。**Phase 3 の 30 分要件は 11.4 分で通る。**

### 11.0 モデルの取得

```text
$ docker desktop enable model-runner
$ docker model status
Docker Model Runner is running
BACKEND    STATUS         DETAILS
llama.cpp  Running        llama.cpp b9879-metal (sha256:b70706f4...)
diffusers  Not Installed
mlx        Not Installed
vllm       Not Installed

$ docker model pull ai/qwen3:30b-a3b-instruct-2507-q4_K_M
Downloaded 18.56GB of 18.56GB
Model pulled successfully
```

**バックエンドは `llama.cpp`（Metal）である。**`mlx` は未導入。§23 の Mac ネイティブ化を
検討するときの出発点になる。

`./scripts/fetch-models.sh`:

```text
fetched: /models/whisper/ggml-large-v3-turbo-q5_0.bin
fetched: /models/whisper/ggml-silero-v5.1.2.bin
-rw-r--r-- 1 1000 1000 574041195 ggml-large-v3-turbo-q5_0.bin
-rw-r--r-- 1 1000 1000    885098 ggml-silero-v5.1.2.bin
394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2  ggml-large-v3-turbo-q5_0.bin
29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf  ggml-silero-v5.1.2.bin
```

**uid 1000 所有で 2 ファイル。**受け入れ条件を満たす。

> **ただし named volume の名前が食い違っていた**（§12.0(1)）。`fetch-models.sh` は
> `voicedock-models` へ書き、Compose は `voicedock_voicedock-models` をマウントしていた。
> v5.13（AA-1）で `name:` を明示して解決。

### 11.1 エンドポイントの実際の形

**コンテナ内から 2 通りとも到達できた。**

```text
$ docker compose exec voicedock python -c "import httpx; print(httpx.get(URL + '/models').text)"

http://model-runner.docker.internal/engines/v1/models   → 200
http://host.docker.internal:12434/engines/v1/models     → 200

{"object":"list","data":[{"id":"docker.io/ai/qwen3:30b-a3b-instruct-2507-q4_K_M",
 "object":"model","created":0,"owned_by":"docker","dmr":{}}]}
```

| 記録事項 | 実測（`/models` を直接叩いた場合） |
|---|---|
| URL の形 | `/engines/v1` を含む形で 200 が返る |
| ホスト名 | `model-runner.docker.internal`（ポート指定なし）と `host.docker.internal:12434` の両方 |
| モデル id | `/models` の一覧は **`docker.io/` 接頭辞つき**で返す（`docker.io/ai/qwen3:...`） |

### 11.1.1 `models` 長構文が実際に注入する値（**上と違う**）

`compose.yaml` に §18.2 の長構文を書いて `make up` した結果:

```text
$ docker compose exec voicedock env | grep VOICEDOCK_LLM
VOICEDOCK_LLM_URL=http://model-runner.docker.internal/v1/
VOICEDOCK_LLM_MODEL=ai/qwen3:30b-a3b-instruct-2507-q4_K_M
```

| | 手で与えていた値 | **Compose 5.5.1 が注入する値** |
|---|---|---|
| URL | `http://model-runner.docker.internal/engines/v1` | **`http://model-runner.docker.internal/v1/`** |
| モデル id | `docker.io/ai/qwen3:30b-a3b-instruct-2507-q4_K_M` | **`ai/qwen3:30b-a3b-instruct-2507-q4_K_M`** |

**どちらも違った。**`/engines/v1` ではなく `/v1/`（**末尾スラッシュつき**）であり、
モデル id に `docker.io/` 接頭辞は**付かない**。`/models` の一覧が接頭辞つきで返すのとは別である。

**両方ともそのまま通る:**

```text
endpoint_of: Endpoint(base_url='http://model-runner.docker.internal/v1/',
                      model='ai/qwen3:30b-a3b-instruct-2507-q4_K_M')
chat_url   : http://model-runner.docker.internal/v1/chat/completions
status: 200
body  : {"choices":[{"message":{"role":"assistant","content":"2"}}], ...}
```

> **末尾スラッシュを吸収しているのは `Endpoint.chat_url` である**（§12.1）。
> `base_url.rstrip("/")` を挟んでおり、註記に「`.../v1` と `.../v1/` の両方が注入されうる」
> と書いてあった。**その想定が当たっていた** — `httpx` の URL 結合に任せていたら
> `urljoin` の規則で `v1` が消えていた。

**§22 R-21（Compose 5 系での `models` 長構文の実挙動）はこれで閉じる。**

コンテナ側 doctor が初めて全項目通った:

```text
[✓] LLM endpoint         http://model-runner.docker.internal/v1/
[✓] LLM response         ai/qwen3:30b-a3b-instruct-2507-q4_K_M  (0.3s, 30 tokens)
────────────────────────────────────────────────────────
12 checks passed, 0 failed, 1 notices
```

（1 notices は `Source deletion DISABLED` ＝ 三重ロックが掛かっている表示である。）

### 11.2 実音声での LLM 実行（**Daily ノートが出た**）

```text
2026-09-14T01:52:20+09:00 INFO  service_started version=0.1.0 schema_version=1
2026-09-14T01:52:42+09:00 INFO  llm_completed session_key=DJIMIC3:20260912 chunks=1 elapsed_s=21.8
2026-09-14T01:52:42+09:00 INFO  obsidian_saved session_key=DJIMIC3:20260912 path="Daily/Voice/Wiki/20260912/2026-09-12 Voice.md" bytes=1504
2026-09-14T01:52:50+09:00 INFO  llm_completed session_key=DJIMIC3:20260913 chunks=1 elapsed_s=8.0
2026-09-14T01:52:50+09:00 INFO  obsidian_saved session_key=DJIMIC3:20260913 path="Daily/Voice/Wiki/20260913/2026-09-13 Voice.md" bytes=2700
```

**Phase 1〜6 が実音声で端から端まで通った。**取り込み → 変換 → Whisper → Raw ノート →
統合 → LLM → Daily ノート → 保存検証 → `COMPLETED`。

| # | 実測 |
|---|---|
| 1 チャンク（初回。モデル読み込み込み） | **21.8 秒** |
| 1 チャンク（2 回目。暖機後） | **8.0 秒** |

**この数字では判定できない。**測ったチャンクは **11 文字と 79 文字**であり、
§21.2 Phase 3 が想定する **20,000 文字**とは桁が 3 つ違う。20,000 字での実測は §11.2.1。

### 11.2.1 20,000 字 1 チャンクの実測（**Phase 3 の判定**）

**毎回ちがう文章**を生成して 3 本（同じ文章を投げるとプロンプトキャッシュが効いて
1.6 秒になり、実態を表さない）:

```text
1 本目:   37.4 秒  status=200  prompt_tokens=12939 completion_tokens=177
2 本目:   38.6 秒  status=200  prompt_tokens=12950 completion_tokens=186
3 本目:   37.3 秒  status=200  prompt_tokens=12994 completion_tokens=177
```

| 項目 | 実測 |
|---|---|
| 20,000 字 1 チャンク | **37.3〜38.6 秒**（約 12,950 prompt tokens） |
| 日本語のトークン密度 | 約 **1.55 文字/トークン** |
| **18 チャンク換算** | **約 11.4 分** |
| §21.2 Phase 3 の上限 | 30 分 |
| **判定** | ✅ **PASS**（2.6 倍の余裕） |

**モデルを落とす必要はない。**`ai/qwen3:30b-a3b-instruct-2507-q4_K_M` のまま進める。

> **プロンプトキャッシュが効く。**同じ入力を再投入すると 38 秒 → **1.6 秒**になった。
> **セッションの作り直し（`regenerated_count`）は安い**ということである。

### 11.2.2 `response_format` の受理

```text
--- response_format あり ---
status  : 200
出力先頭: {"summary": "朝からVoiceDockの削除条件を整理し、…
```

**`{"type":"json_object"}` は受理される。**ただし本番経路は §12.3 の独自抽出を使うので、
**受理されなくても動く**設計は変えない（モデルを差し替えたときに前提が崩れないため）。

出力に `<think>` は現れなかった（`instruct` タグであり thinking 系ではない。§12.3）。

### 11.3 品質の問題（**#98 で起票**）

**20 秒・79 文字の文字起こしから `Key Points` が 19 項目生成された。**
「参加者の表情は明るかった」など、**文字起こしに存在しない内容**である。

| 項目 | `config.yaml` | 実際 |
|---|---|---|
| `key_points` | `max_items: 20` | **19** |
| `tags` | `max_items: 15` | **15**（`default_tags` の 2 件を除く） |

**`tags` は上限にぴったり一致している。**JSON スキーマの `maxItems` がモデルへ渡り、
**埋めるべき数**として解釈されている。`prompts/analyze_ja.txt` は
「発言に存在しない事実を追加しないでください」「推測や補完を行わないでください」と
明示しているが、**散文の制約よりスキーマの数字のほうが強く効いた。**

§1.3 の優先順位 1 は記録の保護である。**要約に言っていないことが混ざるのは記録の破壊と同じ**
であり、しかも Daily は Raw より読まれる。

**原因を A/B で特定した**（同じ文字起こし、同じモデル、`temperature: 0.1`）。
プロンプトの `{schema_block}` から「（最大 N 件）」を外すだけで変わる。

| | `key_points` | `ideas` | `tags` |
|---|---|---|---|
| 「（最大 N 件）」を見せる | **19**（上限 20） | **15** | **15**（上限 15） |
| 見せない | **5** | **3** | **7** |
| 見せる（2 回目） | **19** | **15** | **15** |
| 見せない（2 回目） | **5** | **3** | **6** |

**2 回とも再現した。**`tags` が上限 15 にぴったり一致していたのが決め手で、
モデルは `maxItems` を**埋めるべき目標**として読んでいる。

`prompts/analyze_ja.txt` は

```text
- 発言に存在しない事実を追加しないでください。
- 推測や補完を行わないでください。判断できない項目は空配列にしてください。
```

と明示しているが、**散文の制約よりスキーマの数字のほうが強く効いた。**

**v5.14 までは逆を信じていた。**実装の註記は「上限を明示する（モデルが守りやすくなる）」、
テストの docstring は**「出さないと守られない」**だった。実測がそれを否定した。

修正後（出荷コードのまま 1 回。v5.15 / AC-1）:

```text
--- schema_block に件数の記載: False ---
件数: {'key_points': 6, 'tasks': 0, 'decisions': 0, 'ideas': 3, 'tags': 8}

key_points:
  『魔法少女まどかまぎか』のオーディションが行われた
  斉藤飛鳥が最速打ち上げを実施
  ドローミードライブ終了後に通路でプチ打ち上げ
  参加者全員がほろ酔い状態になり、本音が語られた
  雑談や環境音、笑い声が混ざる
  具体的なタスクや期限は明示されていない
```

**19 → 6。**内容も文字起こしに根ざしたものになった。
残る 2 件（「雑談や環境音…」「具体的なタスクや期限は…」）はプロンプトの前置きと
空配列の説明に引きずられたもので、**録音の内容をでっち上げてはいない。**

> **上限そのものは捨てていない。**`max_items` は `max_length` として pydantic が
> 検証し（§12.2）、超過したら §12.3 の修復が走る。修復プロンプトは `{errors}` を
> 渡すので、**そこで初めて件数の上限が伝わる。**
>
> **文字数の上限（`title` / `summary`）はそのまま見せている。**1 つの文字列の長さで
> あって「埋めるべき個数」ではないため。**ただし同じ偏りが無いかは未測定である。**

### 11.4 ホスト側の RAM

**モデルは Docker VM の中ではなく macOS 側で動く**（Model Runner は `llama-server` を
ホストで直接起動する）。したがって `docker stats` には出ない。

```text
$ ps aux | grep llama-server
20.45 GB  /Users/terada/.docker/bin/inference/llama-server -ngl 999 --metrics --no-mmap --threads 7 --model ...

$ docker stats --no-stream
voicedock-voicedock-1   0.07%   49.12MiB / 23.43GiB

$ sysctl -n hw.memsize
物理メモリ: 64 GB
```

| 項目 | §4.3 の試算 | 実測 |
|---|---|---|
| LLM | 18 GB | **20.45 GB**（`llama-server` の RSS） |
| Docker VM | 24 GB | **23.43 GiB**（割当） |
| 合計 | < 64 GB | **約 44 GB / 64 GB** |

**試算はほぼ当たっていた**（LLM が 2.5 GB ほど多い）。**VoiceDock のコンテナ自体は
49 MiB しか使っていない** — 重いのは Whisper と LLM であり、どちらも外側で動く。

> **`-ngl 999` は全層を Metal へ載せている。**`--threads 7` は Docker VM の割当と同じ数で、
> **両者が同じ CPU を奪い合う**。§22 に「文字起こしと要約を同時に走らせない」根拠が要る場合は
> ここを引く。

---

## 12. 文字起こし性能（#22 で記入）

⚠ **ほぼ完了。**`threads` の最適値と VAD の効果は実測で確定した。
**1 日分の総所要時間の判定だけが保留**（測った音声が 1 日分の 5.84% で、下限 10% に届かない）。
外挿は **1.70 時間**で、上限 8 時間に対して 4.7 倍の余裕がある。

```bash
docker compose logs voicedock | ./scripts/perf-report.sh --asr
```

### 12.0 起動までに踏んだ 2 件

**(1) named volume の名前が食い違っていた。**`scripts/fetch-models.sh` は
`voicedock-models` へ 574 MB を書き込むが、`compose.yaml` は `volumes:` に
`voicedock-models:` と書いただけだったので、**Compose がプロジェクト名を前置して
`voicedock_voicedock-models` を作り、空の volume をマウントしていた。**

```text
$ docker volume ls | grep voicedock
local     voicedock-models              ← fetch-models.sh が入れた（574 MB）
local     voicedock_voicedock-models    ← compose が作ってマウントした（空）
```

**症状は「取得したのに D-8 がモデルを見つけない」であり、取得の失敗と区別がつかない。**
v5.13（AA-1）で `name:` を明示した。

**(2) `config.yaml` が v5.0 以前のままだった。**V-1（設定検証）が起動を中止した。

```text
voicedock: 設定エラー（SPEC §7.3）。起動を中止します: /app/config/config.yaml
  V-1  CONFIG_UNKNOWN_KEY  audio.transcribe_variant
  V-1  CONFIG_UNKNOWN_KEY  retry.auto_retry_failed_after_hours
  V-1  CONFIG_UNKNOWN_KEY  retry.auto_retry_max_rounds
```

3 つとも v5.0 で削除したキーである（§5.3 の `_orig` 固定、§15.2 の無条件再評価）。
**これは製品の不具合ではない。**未知キーを拒んで起動を止める V-1 が設計どおり働き、
**何が古いかを名指しした。**`config.example.yaml` から作り直して解決。

### 12.1 実音声での通し（E2E-01 の前半）

```text
2026-09-14T00:59:33+09:00 INFO  service_started version=0.1.0 schema_version=1
2026-09-14T00:59:33+09:00 INFO  part_discovered recording_key=DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC001_20260912_163444_orig.wav tx=TX00 mic=1 started_at=2026-09-12T16:34:44+09:00 duration=5.72
2026-09-14T00:59:33+09:00 INFO  part_discovered recording_key=DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC002_20260913_233420_orig.wav tx=TX00 mic=2 started_at=2026-09-13T23:34:20+09:00 duration=20.35
2026-09-14T00:59:33+09:00 INFO  normalize_completed recording_key=.../TX00_MIC001_..._orig.wav in_bytes=856456 out_bytes=183176 elapsed_s=0.0
2026-09-14T01:00:00+09:00 INFO  transcription_completed recording_key=.../TX00_MIC001_..._orig.wav elapsed_s=27.2 chars=11 rtf=4.76 speech_ratio=0.708
2026-09-14T01:00:00+09:00 INFO  raw_note_saved session_key=DJIMIC3:20260912 parts=1 bytes=390
2026-09-14T01:00:00+09:00 INFO  normalize_completed recording_key=.../TX00_MIC002_..._orig.wav in_bytes=2963176 out_bytes=651336 elapsed_s=0.0
2026-09-14T01:00:29+09:00 INFO  transcription_completed recording_key=.../TX00_MIC002_..._orig.wav elapsed_s=29.0 chars=79 rtf=1.423 speech_ratio=0.765
2026-09-14T01:00:29+09:00 INFO  raw_note_saved session_key=DJIMIC3:20260913 parts=1 bytes=596
2026-09-14T01:00:29+09:00 INFO  session_merged session_key=DJIMIC3:20260912 parts=1 excluded=0 chars=11
2026-09-14T01:00:29+09:00 ERROR llm_failed session_key=DJIMIC3:20260912 error_code=LLM_UNAVAILABLE
```

**確かめられたこと:**

| | |
|---|---|
| ffmpeg 変換 | 48 kHz/24 bit → 16 kHz/16 bit（856,456 → 183,176 B）。実機の Broadcast Wave を読めた |
| Whisper | 日本語の transcript を生成。**VAD も動作**（`speech_ratio` が出ている） |
| Raw ノート | Vault に 2 枚（`Daily/Voice/Raw/20260912/`・`20260913/`）。frontmatter に自然キー・Timeline 見出し |
| **§10.1.1 の裏づけ** | **同じフォルダの 2 件が `DJIMIC3:20260912` と `DJIMIC3:20260913` の別セッションになった。**フォルダ名（`20260912`）ではなく**ファイル名**の時刻で分組されている |
| LLM 未接続時の振る舞い | `llm_failed error_code=LLM_UNAVAILABLE`。**クラッシュせず名前の付いた誤りとして残る**（§15.1） |

Raw ノート（実物）:

```markdown
---
type: "voice-raw"
voicedock_session_key: "DJIMIC3:20260913"
voicedock_recording_keys:
  - "DJIMIC3/TX_MIC001_20260912_163444/TX00_MIC002_20260913_233420_orig.wav"
date: "2026-09-13"
parts: 1
source: "DJI Mic 3"
---

# 2026-09-13 の文字起こし（生データ）

> 自動文字起こしの生データ。未編集。

## 23:34–23:34

### 23:34:21

（文字起こし本文）
```

### 12.1.1 Obsidian での表示（目視確認）

Obsidian で開いて確認した（2026-09-14）。

| | |
|---|---|
| 配置 | `Daily / Voice / Raw / 20260913 / 2026-09-13 raw` — §13.2 の階層どおり |
| frontmatter の型 | `date` はカレンダー型、`parts` は数値型、残りはテキスト型として解釈された |
| Timeline | `23:34–23:34` の下に `23:34:21` が入れ子で表示された（§10.8） |

> **`voicedock_session_key` が外部リンクとして描画される。**`DJIMIC3:20260913` の
> `<名前>:<値>` という形が URI スキームに見えるためで、クリックすると `DJIMIC3:` を
> 開こうとする。**YAML の値は変わらないので、保存検証（W-6）にも §14.1 の削除条件にも
> 影響しない。**見た目だけの問題であり、当面は手を入れない。
>
> 気にするなら `voicedock_session_key` の値を `DJIMIC3/20260913` のように
> 区切り文字を変えることになるが、**§8.1 の自然キーを表示の都合で変えるのは筋が悪い**
> （ログ・DB・ノートで識別子が 2 系統になる。§16.2 が短い別名を作らないと決めたのと同じ理由）。

### 12.2 文字起こし性能（#22 の第一報。**判定は保留**）

| # | 音声 | elapsed | rtf | speech_ratio |
|---|---|---|---|---|
| 1 | 5.7 s | 27.2 s | 4.76 | 0.708 |
| 2 | 20.4 s | 29.0 s | 1.423 | 0.765 |

**この 2 点から固定費と可変費を分離できる。**

```text
音声の差   20.38 - 5.71 = 14.67 s
所要の差   29.0  - 27.2 =  1.8  s
→ 限界レート   1.8 / 14.67 = 0.123 s/s
→ 1 回の固定費 27.2 - 0.123 × 5.71 ≈ 26.5 s
```

**固定費の正体は whisper-cli が毎回 574 MB のモデルを読むことである。**
30 分 Part なら 1 本あたり `26.5 + 0.123 × 1800 ≈ 247 s`、32 本で **約 2.2 時間**。
§21.2 Phase 2 の 8 時間には収まる見込みだが、**26 秒の実測からの推定にすぎない。**

> **`perf-report.sh` はここで一度嘘をついた。**Part 数で外挿していたため
> 「1 Part = 13 秒」として 32 倍し、**0.25 時間で ✅ PASS** と答えた。
> 音声長で外挿すると固定費が効いて **34.46 時間で ✗ FAIL** になる。**どちらも当てにならない。**
> 1 日分の 10% に満たない実測では**判定しない**（`⬜ 保留`、終了コード 8）ように直した（#95）。
> **偽の PASS を掴むくらいなら「足りない」と言う。**

**判定には 30 分程度の Part を数本流す必要がある**（1 日分の 10% ＝ 約 1.6 時間の音声）。

### 12.2.1 30 分級の Part での実測（**本命**）

2026-09-14、実機で 1 時間弱を録音して通した。**デバイスが 30 分で自動分割した。**

| Part | 音声 | `elapsed_s` | `rtf` | `speech_ratio` | 文字数 |
|---|---|---|---|---|---|
| 1 | 30.1 分 | **153.3** | **0.085** | 0.317 | 688 |
| 2 | 26.0 分 | **204.5** | **0.131** | 0.686 | 667 |

```text
docker compose logs voicedock | ./scripts/perf-report.sh --asr

- 測った音声の長さ : 3364.6 秒（1 日分 16 時間の 5.84%）
- 1 日分（16 時間）への外挿 : 1.70 時間
- 判定: ⬜ 保留 — 測った音声が 1 日分の 10% に満たない（5.84%）
```

**`rtf` が `speech_ratio` に連動している。**VAD が無音を飛ばすので、
**喋っている割合がそのまま処理時間になる。**

**外挿は 1.70 時間**で、§21.2 Phase 2 の上限 8 時間に対して **4.7 倍の余裕**がある。
ただし `perf-report.sh` の下限（1 日分の 10%）に届かないので**判定は保留のまま**である。
**基準を下げない** — 26 秒の実測で偽の PASS を掴んだのを防ぐために入れた規則である。

### 12.3 `threads` の比較（§10.6）

同じ音声（part2 / 26.0 分 / VAD 有効）で `-t` だけを変えた。

| `threads` | `elapsed_s` | `rtf` | 相対 |
|---|---|---|---|
| 4 | 303.1 | 0.194 | 1.00 |
| 6 | 209.6 | 0.134 | 1.45 |
| **7** | **176.0** | **0.113** | **1.72** |

**頭打ちが無い。**4 → 7（1.75 倍）で 1.72 倍なので**ほぼ線形**である。
Docker VM の割当が 7 CPU なので、そこが上限になっている。

**既定を `threads: 0`（自動）にした。**`min(nproc, 8)` に解決し、この機械では 7 になる。
**機械固有の数字を焼き込まない**ためである。

### 12.4 VAD の有無（**破滅的な差**）

| Part | VAD | `elapsed_s` | `rtf` | 文字数 | 区間 / ユニーク |
|---|---|---|---|---|---|
| 1（喋り 31.7%） | on | 151.2 | 0.084 | 688 | — |
| 1 | **off** | **2417.5** | **1.340** | **1918** | — |
| 2（喋り 68.6%） | on | 209.6 | 0.134 | 667 | 40 / 39 |
| 2 | **off** | **2819.3** | **1.806** | **2274** | **178 / 11** |

**13〜16 倍遅くなる。**音声量の比は 1.5 倍（喋り 68.6% → 100%）しかないので、
差の大部分は別の要因である。

**文字数が 3 倍に増える。**音声は同じなのに 667 → 2274。増えた分の正体はこれである:

```text
--- VAD on  --- 区間 40 / ユニーク 39
    2 回  'で'
    1 回  '（実発話 1 件。内容は伏せ字）'

--- VAD off --- 区間 178 / ユニーク 11
   74 回  'はい、ご視聴ありがとうございました。'
   73 回  'はい、どうぞ'
   23 回  'ご視聴ありがとうございました'
```

**178 区間のうち 170 が幻覚である。**無音区間で YouTube の定型句を生成し続けており、
**時間を食っていたのもこれ**である。

| | |
|---|---|
| §22 R-15 | 「無音区間の幻覚」を**品質**のリスクとして挙げていた。**性能としてのほうが遥かに深刻**だと分かった |
| §21.2 Phase 2 | `rtf 1.806` では **16 時間の録音に 29 時間**かかる。**8 時間要件をまったく満たせない** |
| §1.3 の優先順位 1 | **嘘の記録が Vault に残る。**「遅い」より重い |

**VAD は「あると良い機能」ではなく、このシステムが成立するための前提である。**
`vad.enabled: false` は設定としては許すが、**doctor の D-9 が警告する**ようにした
（v5.16 までは黙って skip していた）。

### 12.5 2.9 時間の追加取り込みでの確定測定（**#22 の判定**）

2026-09-14 18:08 に 30 分級 6 本（2.86 時間）を追加取り込みした。
**1 日分（16 時間）の 17.83%** を測れたので、§21.2 Phase 2 の判定条件を満たす。

`docker compose logs voicedock | ./scripts/perf-report.sh --asr` の出力（**要約せずそのまま**）:

```text
| # | Part | 音声 s | elapsed_s | chars | rtf | speech_ratio |
|---|---|---|---|---|---|---|
| 1 | `TX00_MIC005_20260914_150918_orig.wav` | 1799.7 | 547.1 | 2049 | 0.304 | 0.856 |
| 2 | `TX00_MIC006_20260914_153918_orig.wav` | 1798.1 | 656.3 | 2301 | 0.365 | 0.865 |
| 3 | `TX00_MIC007_20260914_160918_orig.wav` | 1799.1 | 953.5 | 2535 | 0.53 | 0.995 |
| 4 | `TX00_MIC008_20260914_163918_orig.wav` | 1800.4 | 1303.5 | 5663 | 0.724 | 0.987 |
| 5 | `TX00_MIC009_20260914_170919_orig.wav` | 1798.4 | 571.9 | 2668 | 0.318 | 0.995 |
| 6 | `TX00_MIC010_20260914_173919_orig.wav` | 1273.8 | 428.0 | 2108 | 0.336 | 0.977 |

- Part 数 : **6**
- 実 `elapsed_s` 合計 : **1.24 時間**（4460.3 秒）
- 文字数合計 : 17324
- 平均 `rtf` : 0.429
- 平均 `speech_ratio` : 0.946
- 測った音声の長さ : **10269.5 秒**（1 日分 16 時間の **17.83%**）
- **1 日分（16 時間）への外挿 : 6.95 時間**（音声長あたりの所要で換算）
- 判定: ✅ **PASS** — 8 時間以内
```

#### `rtf` のばらつきが大きい（**平均だけで語れない**）

| | `rtf` | 16 時間への換算 | 上限 8 時間に対して |
|---|---|---|---|
| 最良（MIC005） | 0.304 | 4.86 時間 | 余裕 39% |
| **集計（外挿に使う値）** | **0.434** | **6.95 時間** | **余裕 13%** |
| **最悪（MIC008）** | **0.724** | **11.58 時間** | **✗ 超過** |

**`rtf` の許容上限は 0.5 である**（16 時間を 8 時間で処理する）。**6 本中 2 本が単独では
これを割っている。**1 日ぶんが MIC008 のような素材で埋まれば Phase 2 を満たせない。

**ばらつきの原因は `speech_ratio` ではない。**MIC007 と MIC009 はどちらも `0.995` だが
`rtf` は `0.530` と `0.318` である。文字数とも一致しない（MIC008 は 5663 字で最遅だが、
MIC007 は 2535 字で 2 番目に遅い）。**素材の難しさが効いていると見られるが、特定できていない。**

> **先週の `rtf 0.113`（§12.2.1）は発話の薄い素材でたまたま出た数字だった。**
> `speech_ratio 0.317` / `0.686` の 2 本しか無く、**今回の 0.856〜0.995 の帯を含んでいない。**
> 5.84% のカバレッジで判定を保留にしたのは正しかった。

#### 性能の余地

コンテナの `nproc` は **7**（`threads: 0` → 7）だが、**ホストは 14 コア**（perf 10 / eff 4）である。
Docker Desktop の CPU 割り当てを増やせば伸びる見込みがある — §12.3 のスイープは
`4 → 6 → 7` でほぼ線形（4→7 で 1.72 倍）で、**頭打ちが見えていない。**
ただし §7.2 の `threads: 0` は `min(nproc, 8)` に解決するので、**8 で頭打ちになる。**
**この上限を上げるかは設定の判断であり、必要になってから決める。**

### 12.6 判定

| 項目 | 結果 |
|---|---|
| `rtf` / `speech_ratio` の実測 | ✅ §12.2.1 / §12.5 |
| **1 日分の総所要時間** | ✅ **6.95 時間**（上限 8 時間）。カバレッジ 17.83% |
| `threads` の最適値 | ✅ **`0`（自動 ＝ この機械では 7）** |
| VAD の効果を数値で示す | ✅ **13〜16 倍。文字数 3 倍**（§12.4） |
| 無音区間の幻覚（R-15） | ✅ **178 区間中 170 が幻覚**（§12.4） |
| 採用モデル | **`large-v3-turbo-q5_0` のまま。**落とす必要は無い |

**モデルを落とす検討は不要である。**ただし **余裕は 13% しかなく、最悪の Part は単独で
上限を超えている**（§12.5）。**「PASS だが余裕は小さい」が正確な記述である。**

**次にやるべきこと**: 終日運用（#35 / E2E-06）で 16 時間ぶんを実際に流し、
外挿ではなく実測で確かめる。外挿はばらつきを平均で潰すため、**1 日の中の偏りを映さない。**

