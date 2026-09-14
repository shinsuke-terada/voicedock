/* voicedock-ingest-launcher — LaunchAgent へ TCC の許可を届かせるためのラッパ
 *
 * **launchd がシェルスクリプトを直接起動すると、デバイスを読めない。**
 * 2026-09-14 に実機で確かめた（#95）。同じマシン・同じ 1 分の中での A/B:
 *
 *   plist の program = voicedock-ingest（スクリプト直接）
 *     → volume_skipped name=DJIMIC3 reason=volume not listable
 *   plist の program = 本プログラム（ad-hoc 署名済み）
 *     → device_detected → remounted_readonly → ingest_finished devices=1
 *
 * **なぜ効くかの機構は確定していない。**launchd のジョブ本体がスクリプトだと、
 * 実体は shebang 経由の /bin/bash（Apple 署名のプラットフォームバイナリ）になり、
 * TCC が許可の付与先を決められないため、と見ている。
 * **本コメントは推測ではなく上の測定を根拠とする。**
 *
 * **`execv` で自分自身を置き換えてはならない。**TCC の responsible process を
 * 自分のまま保つため、`posix_spawn` で子として起動して待つ。
 * 実機で通ったのはこの形である。
 */
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <script> [args...]\n", argv[0]);
        return 2;
    }
    char *args[argc + 1];
    args[0] = "/bin/bash";
    for (int i = 1; i < argc; i++) args[i] = argv[i];
    args[argc] = NULL;

    pid_t pid;
    int rc = posix_spawn(&pid, "/bin/bash", NULL, NULL, args, environ);
    if (rc != 0) {
        fprintf(stderr, "posix_spawn failed: %d\n", rc);
        return 1;
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
