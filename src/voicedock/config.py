"""設定の読み込みと検証（SPEC §7）。

**設定の出所は `config/config.yaml` だけである。**§7 は「環境変数による個別キーの上書き機構は
提供しない」と規定している。設定の出所が二重になると、どちらが効いているか追えなくなる。
このモジュールが読む環境変数は **`VOICEDOCK_CONFIG`（設定ファイルの位置）ただ 1 つ**であり、
`tests/unit/test_config.py::test_no_env_override_mechanism` がそれを AST で固定している。

**全フィールドを必須にし、既定値を持たせない。**既定値の出所は SPEC §7.2 と、その完全な写しである
`config/config.example.yaml` だけである。コード側に既定値を置くと二重管理になり、必ずどちらかが
古くなる — v4.0 の `device:` がまさにそれで、実際に両者の既定値が食い違っていた
（§7.4 の注記）。

**§7.3 に無い制約は足さない。**`Literal` / `ge` / `le` は V 規則が要求するものだけに付ける。
`config.py` が SPEC より厳しくなると、SPEC が許す設定を拒否することになる。

`RULES` の並びは **SPEC §7.3 の表と同じ順**に保つこと。`tests/unit/test_config.py` が SPEC を
parse して過不足と並びを検出する。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from voicedock.errors import ErrorCode
from voicedock.heartbeat import DEFAULT_STATE_ROOT, Heartbeat, read_heartbeat
from voicedock.log import LogFormat, Logger, LogLevel, get_logger

DEFAULT_CONFIG_PATH: Final = Path("/app/config/config.yaml")
CONFIG_PATH_ENV: Final = "VOICEDOCK_CONFIG"

NO_RULE: Final = "-"
"""§7.3 に規則が無い設定エラー（キー欠落・型違い）を表す規則 ID。"""

TEMPLATE_PLACEHOLDERS: Final = frozenset({"yyyymmdd", "date", "time", "part"})
"""V-13 が許すプレースホルダ。`{title}` は含めない（V-14 が別に扱う）。"""

_PLACEHOLDER = re.compile(r"\{([^}]*)\}")


# --- 報告の型 ------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """設定違反 1 件。`rule` が `NO_RULE` のときは §7.3 に対応する規則が無い。"""

    rule: str
    code: ErrorCode
    key: str
    message: str

    def render(self) -> str:
        """doctor と起動時の stderr で使う 1 行表記。"""
        return f"{self.rule}  {self.code}  {self.key}: {self.message}"


@dataclass(frozen=True)
class Notice:
    """起動を止めない警告（V-26 / V-30 の不明ケース）。"""

    rule: str
    message: str


class ConfigError(Exception):
    """設定を採用できない。呼び出し側は終了コード 2 を返す（§17.3）。"""

    def __init__(self, path: Path, violations: tuple[Violation, ...]) -> None:
        self.path = path
        self.violations = violations
        super().__init__(f"{path}: {len(violations)} 件の設定違反")


class ConfigUnreadable(ConfigError):
    """設定ファイルが無い / YAML として壊れている。個別の規則違反ではない。"""


# --- 規則の登録（SPEC §7.3 の並び順） -------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    key: str
    """検出器がキーを特定しないときに報告するキー。変換の入力には使わない（文書と整合テスト用）。"""

    code: ErrorCode


_IV: Final = ErrorCode.CONFIG_INVALID_VALUE

RULES: Final[tuple[Rule, ...]] = (
    Rule("V-1", "<未知のキー>", ErrorCode.CONFIG_UNKNOWN_KEY),
    Rule("V-2", "audio.transcribe_variant", _IV),
    Rule("V-3", "audio.target_sample_rate", _IV),
    Rule("V-4", "device.poll_interval_seconds", _IV),
    Rule("V-7", "session.group_by", _IV),
    Rule("V-8", "session.block_gap_seconds", _IV),
    Rule("V-9", "retry.backoff_seconds", _IV),
    Rule("V-10", "llm.max_chars_per_request", _IV),
    Rule("V-11", "obsidian.raw.folder_template", _IV),
    Rule("V-12", "obsidian.wiki.folder_template", _IV),
    Rule("V-13", "obsidian.raw.folder_template", _IV),
    Rule("V-14", "obsidian.wiki.filename_template", _IV),
    Rule("V-15", "obsidian.raw.granularity", _IV),
    Rule("V-16", "obsidian.max_title_bytes", _IV),
    Rule("V-17", "llm.analysis.order", _IV),
    Rule("V-18", "llm.analysis.sections.summary.enabled", _IV),
    Rule("V-19", "llm.analysis.sections.summary.heading", _IV),
    Rule("V-20", "llm.prompts.analyze", _IV),
    Rule("V-21", "cleanup.retain_transcript_days", _IV),
    Rule("V-22", "import.staging_max_bytes", _IV),
    Rule("V-23", "transcription.model", ErrorCode.WHISPER_MODEL_MISSING),
    Rule("V-24", "transcription.executable", ErrorCode.WHISPER_EXEC_MISSING),
    Rule("V-25", "transcription.vad.model", ErrorCode.WHISPER_MODEL_MISSING),
    Rule("V-26", "cleanup.delete_source_audio", _IV),
    Rule("V-27", "obsidian.raw.filename_template", _IV),
    Rule("V-29", "import.inbox_retain", _IV),
    Rule("V-30", "cleanup.delete_source_audio", ErrorCode.CONFIG_LOCK_MISMATCH),
    Rule("V-31", "import.helper_heartbeat_max_age_seconds", _IV),
    Rule("V-32", "timezone", _IV),
)

RULE_BY_ID: Final[Mapping[str, Rule]] = {r.id: r for r in RULES}
IMPLEMENTED_RULES: Final[frozenset[str]] = frozenset(r.id for r in RULES)

# 制約（Literal / ge / le）で検出される規則だけを持つ表。
# validator が投げる規則はメッセージが自分で規則 ID を名乗るため、ここには入れない。
_RULE_BY_LOCATION: Final[Mapping[tuple[str | int, ...], str]] = {
    ("audio", "transcribe_variant"): "V-2",
    ("audio", "target_sample_rate"): "V-3",
    ("audio", "target_channels"): "V-3",
    ("audio", "target_codec"): "V-3",
    ("device", "poll_interval_seconds"): "V-4",
    ("session", "group_by"): "V-7",
    ("session", "block_gap_seconds"): "V-8",
    ("obsidian", "raw", "granularity"): "V-15",
    ("obsidian", "max_title_bytes"): "V-16",
    ("cleanup", "retain_transcript_days"): "V-21",
    ("import", "inbox_retain"): "V-29",
    ("import", "helper_heartbeat_max_age_seconds"): "V-31",
}

# validator は必ず "V-n: <ドット区切りのキー>: <説明>" の形で投げる。
# pydantic は "Value error, " を前置するため search で取り出す。
_VALIDATOR_MESSAGE = re.compile(r"\b(V-\d+): ([A-Za-z0-9_.]+): (.*)\Z", re.S)


# --- 設定モデル（SPEC §7.2 の階層をそのまま写す） -------------------------


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeviceConfig(_Section):
    root: Path
    poll_interval_seconds: int = Field(ge=1)  # V-4


class AudioConfig(_Section):
    transcribe_variant: Literal["denoised", "orig"]  # V-2
    ffmpeg: Path
    ffprobe: Path
    target_sample_rate: Literal[16000]  # V-3
    target_channels: Literal[1]  # V-3
    target_codec: Literal["pcm_s16le"]  # V-3
    ffprobe_timeout_seconds: int
    ffmpeg_timeout_factor: float
    ffmpeg_min_timeout_seconds: int
    duration_tolerance_seconds: float


class ImportConfig(_Section):
    inbox_root: Path
    staging_root: Path
    free_space_multiplier: float
    free_space_margin_bytes: int
    staging_max_bytes: int
    hash_chunk_bytes: int
    inbox_retain: Literal["normalized", "raw_saved"]  # V-29
    helper_heartbeat_max_age_seconds: int = Field(ge=60)  # V-31

    @model_validator(mode="after")
    def _check(self) -> ImportConfig:
        if self.staging_max_bytes <= self.free_space_margin_bytes:
            raise ValueError(
                "V-22: import.staging_max_bytes: free_space_margin_bytes より大きいこと"
                f"（{self.staging_max_bytes} <= {self.free_space_margin_bytes}）"
            )
        return self


class SessionConfig(_Section):
    group_by: Literal["day"]  # V-7
    block_gap_seconds: int = Field(ge=0)  # V-8
    idle_close_seconds: int
    allow_reopen: bool
    max_parts: int
    max_duration_seconds: int


class VadConfig(_Section):
    enabled: bool
    model: Path
    threshold: float
    min_speech_duration_ms: int
    min_silence_duration_ms: int
    speech_pad_ms: int


class TranscriptionConfig(_Section):
    engine: str
    executable: Path
    model: Path
    language: str
    threads: int
    timeout_factor: float
    min_timeout_seconds: int
    max_timeout_seconds: int
    min_chars: int
    vad: VadConfig


class PromptsConfig(_Section):
    analyze: Path
    map: Path
    reduce: Path
    repair: Path


class SectionConfig(_Section):
    """`llm.analysis.sections.*` の 1 件。

    `heading` と `max_items` だけは省略を許す。§7.2 で `summary` / `timeline` は `max_items` を
    持たず、`tags` は `heading` を持たないためである（既定値ではなく「無い」を表す）。
    """

    enabled: bool
    heading: str | None = None
    max_items: int | None = None


class AnalysisConfig(_Section):
    sections: dict[str, SectionConfig]
    order: tuple[str, ...]
    custom_instructions: str

    @model_validator(mode="after")
    def _check(self) -> AnalysisConfig:
        key = "llm.analysis"
        unknown = [name for name in self.order if name not in self.sections]
        if unknown:
            raise ValueError(f"V-17: {key}.order: sections に無い項目 {unknown}")
        if len(set(self.order)) != len(self.order):
            raise ValueError(f"V-17: {key}.order: 重複がある {list(self.order)}")

        summary = self.sections.get("summary")
        if summary is None or not summary.enabled:
            raise ValueError(
                f"V-18: {key}.sections.summary.enabled: true でなければならない（要約の中核）"
            )

        for name in self.order:
            heading = self.sections[name].heading
            hkey = f"{key}.sections.{name}.heading"
            if heading is None:
                raise ValueError(f"V-19: {hkey}: order に載っている項目は heading を持つこと")
            if not heading.startswith("#") or "\n" in heading:
                raise ValueError(f"V-19: {hkey}: '#' で始まる 1 行であること（{heading!r}）")
        return self


class LlmConfig(_Section):
    endpoint_env: str
    model_env: str
    temperature: float
    top_p: float
    max_output_tokens: int
    request_timeout_seconds: int
    max_chars_per_request: int
    max_seconds_per_request: int
    chunk_overlap_chars: int
    repair_attempts: int
    prompts: PromptsConfig
    analysis: AnalysisConfig

    @model_validator(mode="after")
    def _check(self) -> LlmConfig:
        if self.max_chars_per_request <= self.chunk_overlap_chars * 2:
            raise ValueError(
                "V-10: llm.max_chars_per_request: chunk_overlap_chars の 2 倍より大きいこと"
                f"（{self.max_chars_per_request} <= {self.chunk_overlap_chars * 2}）"
            )
        return self


class RawConfig(_Section):
    folder_template: str
    granularity: Literal["day", "part"]  # V-15
    filename_template: str
    timestamp_interval_seconds: int
    part_boundary_heading: bool

    @model_validator(mode="after")
    def _check(self) -> RawConfig:
        key = "obsidian.raw.filename_template"
        _check_placeholders(key, self.filename_template)  # V-13
        if self.granularity == "part" and "{part}" not in self.filename_template:
            raise ValueError(
                f"V-27: {key}: granularity が part のときは {{part}} を含むこと"
                "（含まないと Part ごとのファイルが同名衝突する）"
            )
        return self


class WikiConfig(_Section):
    folder_template: str
    filename_template: str
    include_transcript: bool
    timeline: bool
    link_raw: bool
    link_daily_note: bool
    link_adjacent_days: bool
    link_tags: bool
    link_only_existing: bool
    vault_index_cache_seconds: int
    max_links: int

    @model_validator(mode="after")
    def _check(self) -> WikiConfig:
        # V-14 を V-13 より先に見る。{title} は「既知だがここには置けない」ため、
        # 順序を逆にすると未知プレースホルダ（V-13）として報告されてしまう。
        key = "obsidian.wiki.filename_template"
        if "{title}" in self.filename_template:
            raise ValueError(
                f"V-14: {key}: {{title}} を含んではならない（再生成のたびにファイルが増殖する）"
            )
        _check_placeholders(key, self.filename_template)  # V-13
        return self


class ObsidianConfig(_Section):
    root: Path
    max_title_bytes: int = Field(ge=1, le=255)  # V-16
    default_tags: tuple[str, ...]
    raw: RawConfig
    wiki: WikiConfig

    @model_validator(mode="after")
    def _check(self) -> ObsidianConfig:
        for key, template in (
            ("obsidian.raw.folder_template", self.raw.folder_template),
            ("obsidian.wiki.folder_template", self.wiki.folder_template),
        ):
            _check_relative(key, template)  # V-11
            _check_placeholders(key, template)  # V-13
        if self.raw.folder_template == self.wiki.folder_template:
            raise ValueError(
                "V-12: obsidian.wiki.folder_template: raw.folder_template と同一にできない"
                f"（{self.wiki.folder_template!r}）"
            )
        return self


class CleanupConfig(_Section):
    delete_source_audio: bool
    delete_normalized_after_transcribe: bool
    retain_transcript_days: Literal[0]  # V-21
    delete_evaluation_backoff_seconds: tuple[int, ...]
    queue_root: Path
    delete_result_timeout_seconds: int


class RetryConfig(_Section):
    max_attempts: int
    backoff_seconds: tuple[int, ...]
    auto_retry_failed_after_hours: int
    auto_retry_max_rounds: int

    @model_validator(mode="after")
    def _check(self) -> RetryConfig:
        if len(self.backoff_seconds) < self.max_attempts:
            raise ValueError(
                "V-9: retry.backoff_seconds: max_attempts と同数以上の要素が必要"
                f"（{len(self.backoff_seconds)} < {self.max_attempts}）"
            )
        return self


class LoggingConfig(_Section):
    # log.py の Literal をそのまま使う。型が 2 箇所に分かれないようにする
    level: LogLevel
    format: LogFormat
    unsafe_log_content: bool


class DatabaseConfig(_Section):
    path: Path
    busy_timeout_ms: int


class Config(_Section):
    """`config/config.yaml` 全体（SPEC §7.2）。"""

    timezone: str
    device: DeviceConfig
    audio: AudioConfig
    # `import` は Python の予約語。エイリアスで受け、属性名は import_ にする。
    # populate_by_name は既定（False）のまま — `import_:` と書いた設定は V-1 で弾く
    import_: ImportConfig = Field(alias="import")
    session: SessionConfig
    transcription: TranscriptionConfig
    llm: LlmConfig
    obsidian: ObsidianConfig
    cleanup: CleanupConfig
    retry: RetryConfig
    logging: LoggingConfig
    database: DatabaseConfig

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"V-32: timezone: 解決できないタイムゾーン {value!r}（{e}）") from e
        return value

    @property
    def tz(self) -> ZoneInfo:
        """`timezone` の tzinfo（§10.4 の `cfg.tz`）。V-32 を通っているので必ず解決する。"""
        return ZoneInfo(self.timezone)


# --- テンプレートの検査（V-11 / V-13） ------------------------------------


def _check_relative(key: str, template: str) -> None:
    """V-11: 相対パスで `..` を含まないこと。"""
    parts = PurePosixPath(template).parts
    if PurePosixPath(template).is_absolute():
        raise ValueError(f"V-11: {key}: 相対パスであること（{template!r}）")
    if ".." in parts:
        raise ValueError(f"V-11: {key}: '..' を含んではならない（{template!r}）")


def _check_placeholders(key: str, template: str) -> None:
    """V-13: 未知のプレースホルダが無いこと。"""
    for name in _PLACEHOLDER.findall(template):
        if name not in TEMPLATE_PLACEHOLDERS:
            allowed = " ".join(sorted(TEMPLATE_PLACEHOLDERS))
            raise ValueError(
                f"V-13: {key}: 未知のプレースホルダ {{{name}}}（使えるのは {allowed}）"
            )


# --- 読み込み ------------------------------------------------------------


def config_path() -> Path:
    """設定ファイルの位置。`VOICEDOCK_CONFIG` があればそれを使う。

    **これがこのモジュールで唯一読む環境変数である。**個別キーの上書き機構は提供しない（§7）。
    """
    override = os.environ.get(CONFIG_PATH_ENV)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_document(path: Path) -> dict[str, Any]:
    """YAML を読んでトップレベルの辞書を返す。読めなければ `ConfigUnreadable`。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigUnreadable(
            path,
            (
                Violation(
                    NO_RULE, ErrorCode.CONFIG_INVALID_VALUE, "<file>", "設定ファイルがありません"
                ),
            ),
        ) from e
    except OSError as e:
        raise ConfigUnreadable(
            path,
            (Violation(NO_RULE, ErrorCode.CONFIG_INVALID_VALUE, "<file>", f"読み取れません: {e}"),),
        ) from e
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigUnreadable(
            path,
            (
                Violation(
                    NO_RULE, ErrorCode.CONFIG_INVALID_VALUE, "<file>", f"YAML として不正: {e}"
                ),
            ),
        ) from e
    if not isinstance(document, dict):
        raise ConfigUnreadable(
            path,
            (
                Violation(
                    NO_RULE,
                    ErrorCode.CONFIG_INVALID_VALUE,
                    "<file>",
                    "トップレベルがマッピングでありません",
                ),
            ),
        )
    return document


def parse_config(document: Mapping[str, Any]) -> tuple[Config | None, list[Violation]]:
    """辞書を検証する。違反があれば `(None, violations)` を返す（例外にしない）。"""
    try:
        return Config.model_validate(document), []
    except ValidationError as e:
        return None, [_violation_from(err) for err in e.errors()]


def _violation_from(err: Mapping[str, Any]) -> Violation:
    message = str(err["msg"])
    loc: tuple[str | int, ...] = tuple(err["loc"])

    matched = _VALIDATOR_MESSAGE.search(message)
    if matched:
        rule, key, detail = matched.group(1), matched.group(2), matched.group(3)
        return Violation(rule, _code_for(rule), key, detail)

    rule = "V-1" if err["type"] == "extra_forbidden" else _RULE_BY_LOCATION.get(loc, NO_RULE)
    key = ".".join(str(p) for p in loc) or "<root>"
    return Violation(rule, _code_for(rule), key, message)


def _code_for(rule: str) -> ErrorCode:
    found = RULE_BY_ID.get(rule)
    return found.code if found else ErrorCode.CONFIG_INVALID_VALUE


def check_files(cfg: Config) -> list[Violation]:
    """ファイル実在検査（V-20 / V-23 / V-24 / V-25）。"""
    violations: list[Violation] = []

    # getattr で回さない。mypy strict が型を失うため、明示的に並べる
    prompts = (
        ("analyze", cfg.llm.prompts.analyze),
        ("map", cfg.llm.prompts.map),
        ("reduce", cfg.llm.prompts.reduce),
        ("repair", cfg.llm.prompts.repair),
    )
    for name, target in prompts:
        if not target.is_file():
            violations.append(
                _make("V-20", f"llm.prompts.{name}", f"ファイルがありません: {target}")
            )

    model = cfg.transcription.model
    if not model.is_file():
        violations.append(_make("V-23", "transcription.model", f"ファイルがありません: {model}"))

    executable = cfg.transcription.executable
    if not executable.is_file():
        violations.append(
            _make("V-24", "transcription.executable", f"ファイルがありません: {executable}")
        )
    elif not os.access(executable, os.X_OK):
        violations.append(
            _make("V-24", "transcription.executable", f"実行できません: {executable}")
        )

    if cfg.transcription.vad.enabled:
        vad_model = cfg.transcription.vad.model
        if not vad_model.is_file():
            violations.append(
                _make("V-25", "transcription.vad.model", f"ファイルがありません: {vad_model}")
            )

    return violations


def check_locks(
    cfg: Config, *, state_root: Path = DEFAULT_STATE_ROOT, now: datetime | None = None
) -> list[Violation]:
    """V-30: 削除ロックの**確定した**食い違いだけを違反にする（§7.3 / §14.2）。

    不明（heartbeat が無い / 古い / フィールドが無い）は違反にしない。`startup_notices` が
    警告する。Helper が復帰するまでコンテナをクラッシュループさせないためである。
    """
    if not cfg.cleanup.delete_source_audio:
        return []
    state = _helper_delete_flag(cfg, state_root=state_root, now=now)
    if state is False:
        return [
            _make(
                "V-30",
                "cleanup.delete_source_audio",
                "helper.conf 側（heartbeat.json の delete_source_audio）が false。"
                "片方だけの解除は事故のため起動しない",
            )
        ]
    return []


def startup_notices(
    cfg: Config, *, state_root: Path = DEFAULT_STATE_ROOT, now: datetime | None = None
) -> list[Notice]:
    """起動を止めない警告（V-26 と、V-30 の不明ケース）。"""
    if not cfg.cleanup.delete_source_audio:
        return []
    notices = [
        Notice(
            "V-26",
            "元音声の削除が有効（ロック 1 の config 側）。"
            "保存検証に成功した録音はデバイスから削除される",
        )
    ]
    if _helper_delete_flag(cfg, state_root=state_root, now=now) is None:
        notices.append(
            Notice(
                "V-30",
                "helper.conf 側の値を確認できない（heartbeat.json が無い / 古い / "
                "delete_source_audio を報告していない）。削除は要求しない",
            )
        )
    return notices


def _helper_delete_flag(cfg: Config, *, state_root: Path, now: datetime | None) -> bool | None:
    """Helper が報告する `delete_source_audio`。不明なら `None`。"""
    beat: Heartbeat | None = read_heartbeat(state_root)
    if beat is None:
        return None
    moment = now if now is not None else datetime.now(cfg.tz)
    if beat.is_stale(moment, cfg.import_.helper_heartbeat_max_age_seconds):
        return None
    return beat.delete_source_audio


def _make(rule: str, key: str, message: str) -> Violation:
    return Violation(rule, _code_for(rule), key, message)


def load_config(
    path: Path | None = None,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
    check_filesystem: bool = True,
) -> Config:
    """設定を読み、§7.3 の全規則を検査する。違反があれば `ConfigError`。"""
    target = path if path is not None else config_path()
    document = load_document(target)

    cfg, violations = parse_config(document)
    if cfg is None:
        raise ConfigError(target, tuple(violations))

    if check_filesystem:
        violations += check_files(cfg)
    violations += check_locks(cfg, state_root=state_root, now=now)
    if violations:
        raise ConfigError(target, tuple(violations))
    return cfg


def key_count(document: Mapping[str, Any]) -> int:
    """設定の葉の数（doctor の `N keys`）。

    マッピング以外の値を 1 と数える。**リストは 1 と数える**（要素数を足さない）。
    """

    def leaves(node: Any) -> int:
        if isinstance(node, Mapping):
            return sum(leaves(value) for value in node.values())
        return 1

    return leaves(document)


def logger_for(cfg: Config) -> Logger:
    """設定から構造化ログを組み立てる（§16.2）。"""
    return get_logger(
        fmt=cfg.logging.format,
        level=cfg.logging.level,
        tz=cfg.tz,
        unsafe_log_content=cfg.logging.unsafe_log_content,
    )
