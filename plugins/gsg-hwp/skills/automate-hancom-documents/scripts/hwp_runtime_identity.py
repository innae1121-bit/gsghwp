from __future__ import annotations

# SIZE_OK — runtime identity, source hash, and loaded-DLL state form one response boundary.

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import ClassVar, Final, Literal

from mcp.types import Tool
from pydantic import BaseModel, ConfigDict, Field

from hwp_native_install import (
    LoadedBridgeModule,
    content_digest,
    packaged_native_bridge_digest,
    scan_loaded_native_bridges,
)


class RuntimeBuildInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    distribution: str = Field(min_length=1)
    mcp: str = Field(min_length=1)
    native_bridge: str = Field(min_length=1)
    protocol: Literal[9, 10, 11, 12, 13, 14]


class RuntimeIdentity(RuntimeBuildInfo):
    recipe_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class LoadedNativeBridge(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    process_id: int = Field(ge=1)
    process_name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    version: str | None = None
    matches_expected: bool


class NativeBridgeRuntime(BaseModel):
    """What the running Hangul processes actually mapped, not what we expect.

    ``native_bridge`` is this build's expectation. ``loaded_native_bridge`` is
    read out of the live process module lists, so the two disagreeing is the
    whole point of this model: Hangul resolves the bridge DLL once, at process
    start, and keeps it until it restarts no matter what Python installs or
    registers afterwards.

    ``loaded_native_bridge`` and ``native_bridge_state`` must never contradict
    each other inside one response. ``loaded_native_bridge`` is non-null exactly
    for ``matched`` and ``mismatched``; it is ``null`` for ``not_loaded``,
    ``not_running`` and ``unknown``. Anything an editor process is missing or
    anything unreadable is said in ``native_bridge_notice``, which is prose and
    may qualify the verdict without contradicting it.

    Under ``matched`` the field still says *what is mapped*, which is not
    necessarily a restatement of ``native_bridge``. The match is decided by the
    DLL's SHA-256 against the packaged binary, while the version string is only
    recoverable from the install layout (``.../native/<version>/...``), so a
    byte-identical bridge loaded from a path without a version reports
    ``matched`` with ``loaded_native_bridge="unknown"``.

    ``hangul_restart_required`` is true only for ``mismatched``: that is the one
    state proving a stale DLL is executing now. It is a report, not a gate --
    nothing in this plugin refuses work because of it.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    loaded_native_bridge: str | None = None
    native_bridge_state: Literal[
        "matched",
        "mismatched",
        "not_loaded",
        "not_running",
        "unknown",
    ] = "unknown"
    hangul_restart_required: bool = False
    native_bridge_notice: str = ""
    loaded_native_bridge_modules: tuple[LoadedNativeBridge, ...] = ()


class RuntimeStatus(RuntimeIdentity, NativeBridgeRuntime):
    process_id: int = Field(ge=1)
    worker_process_id: int = Field(ge=1)
    source_path: str = Field(min_length=1)
    python_executable: str = Field(min_length=1)
    tool_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_count: int = Field(ge=1)
    generation: int = Field(ge=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    loaded_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reload_required: bool
    worker_state: Literal["idle", "busy", "unknown"] = "idle"
    reload_state: Literal["not_requested", "reloaded", "deferred", "unknown"] = (
        "not_requested"
    )


class RuntimeStartupRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    event: Literal["hwp_mcp_start"] = "hwp_mcp_start"
    runtime: RuntimeIdentity


PLUGIN_ROOT: Final = Path(__file__).resolve().parents[3]
RUNTIME_WATCH_PATHS: Final = (
    Path(__file__).resolve().parent,
    PLUGIN_ROOT / "compatibility-manifest.json",
)
RUNTIME_IDENTITY: Final = RuntimeIdentity.model_validate_json(
    (PLUGIN_ROOT / "compatibility-manifest.json").read_text(encoding="utf-8")
)
RUNTIME_BUILD_INFO: Final = RuntimeBuildInfo(
    distribution=RUNTIME_IDENTITY.distribution,
    mcp=RUNTIME_IDENTITY.mcp,
    native_bridge=RUNTIME_IDENTITY.native_bridge,
    protocol=RUNTIME_IDENTITY.protocol,
)


@dataclass(frozen=True, slots=True)
class _RuntimeSourceFile:
    path: Path
    identity: bytes


@dataclass(frozen=True, slots=True)
class RuntimeSourceState:
    directories: tuple[Path, ...]
    files: tuple[_RuntimeSourceFile, ...]
    fingerprint: tuple[tuple[int, int, int], ...]
    content_hash: str


def _runtime_source_inventory(
    paths: Sequence[Path],
) -> tuple[tuple[Path, ...], tuple[_RuntimeSourceFile, ...]]:
    directories: list[Path] = []
    files: list[_RuntimeSourceFile] = []
    for root_index, root in enumerate(paths):
        if root.is_dir():
            directories.extend(path for path in root.rglob("*") if path.is_dir())
            directories.append(root)
            source_files = tuple(
                path
                for path in sorted(root.rglob("*"))
                if path.is_file() and path.suffix in {".json", ".py"}
            )
        else:
            source_files = (root,)
        for path in source_files:
            relative = path.relative_to(root) if root.is_dir() else Path(path.name)
            files.append(
                _RuntimeSourceFile(
                    path=path,
                    identity=f"{root_index}:{relative.as_posix()}".encode("utf-8"),
                )
            )
    return tuple(sorted(set(directories))), tuple(files)


def _runtime_source_fingerprint(
    directories: Sequence[Path],
    files: Sequence[_RuntimeSourceFile],
) -> tuple[tuple[int, int, int], ...]:
    paths = (*directories, *(source.path for source in files))
    return tuple(
        (status.st_size, status.st_mtime_ns, status.st_ctime_ns)
        for status in (path.stat() for path in paths)
    )


def _runtime_content_hash(files: Sequence[_RuntimeSourceFile]) -> str:
    digest = hashlib.sha256()
    for source_file in files:
        digest.update(len(source_file.identity).to_bytes(4, "big"))
        digest.update(source_file.identity)
        if source_file.path.is_file():
            with source_file.path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        else:
            digest.update(b"missing")
    return digest.hexdigest()


def runtime_source_state(paths: Sequence[Path]) -> RuntimeSourceState:
    directories, files = _runtime_source_inventory(paths)
    return RuntimeSourceState(
        directories=directories,
        files=files,
        fingerprint=_runtime_source_fingerprint(directories, files),
        content_hash=_runtime_content_hash(files),
    )


def refresh_runtime_source_state(
    state: RuntimeSourceState,
    paths: Sequence[Path],
) -> RuntimeSourceState:
    try:
        current_fingerprint = _runtime_source_fingerprint(
            state.directories,
            state.files,
        )
    except OSError:
        return runtime_source_state(paths)
    if current_fingerprint == state.fingerprint:
        return state
    return runtime_source_state(paths)


def runtime_source_hash(paths: Sequence[Path]) -> str:
    """Hash ``paths`` outright. Authoritative, and never fingerprint-gated.

    Callers that re-ask for the *same* watch roots on a cycle should use
    ``current_runtime_source_hash`` instead; this one stays uncached so the
    release tooling and the tests keep an unconditional second opinion.
    """
    return runtime_source_state(paths).content_hash


_RUNTIME_LOADED_SOURCE_STATE: Final = runtime_source_state(RUNTIME_WATCH_PATHS)
RUNTIME_LOADED_SOURCE_HASH: Final = _RUNTIME_LOADED_SOURCE_STATE.content_hash
_runtime_watch_state: RuntimeSourceState = _RUNTIME_LOADED_SOURCE_STATE


def current_runtime_source_hash() -> str:
    """The watch roots' hash now, re-reading bytes only when a stat moved.

    ``runtime_source_hash`` re-hashes 378 files / 6.8MB every time, which the
    worker paid once per cycle just to answer ``hwp_runtime_info``. The
    fingerprint (size, mtime_ns, ctime_ns over every watched directory *and*
    file) decides instead: an added or removed file moves its directory's stat,
    a deleted file raises and falls through to a full rescan, and edited bytes
    move that file's own stat -- so an unchanged fingerprint means the same file
    set and the same bytes, and the cached hash is the hash. This is the very
    gate ``HwpWorkerSupervisor`` already reloads on
    (``hwp_mcp_worker_supervisor.py`` :360, :398, :462); the reading here is not
    a weaker one, just the same one applied in the worker process.
    """
    global _runtime_watch_state
    _runtime_watch_state = refresh_runtime_source_state(
        _runtime_watch_state,
        RUNTIME_WATCH_PATHS,
    )
    return _runtime_watch_state.content_hash


def tool_schema_hash(tools: Sequence[Tool]) -> str:
    payload = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in sorted(tools, key=lambda value: value.name)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_tool_schema_hash(tool: Tool) -> str:
    """Hash one public tool independently of catalog membership or order."""
    return tool_schema_hash((tool,))


def tool_schema_hashes(tools: Sequence[Tool]) -> dict[str, str]:
    """Return stable per-tool hashes; aggregate catalog hashes are provenance."""
    return {tool.name: canonical_tool_schema_hash(tool) for tool in tools}


_RESTART_HINT: Final = "한/글을 재시작해야 새 브리지가 적용됩니다"
_REPORTED_MODULE_LIMIT: Final = 3


def _module_matches_expected(
    module: LoadedBridgeModule,
    expected_digest: str | None,
    expected_version: str,
) -> bool:
    loaded_digest = content_digest(module.path)
    if expected_digest is not None and loaded_digest is not None:
        return loaded_digest == expected_digest
    return module.version == expected_version


def _process_list(process_ids: Sequence[int]) -> str:
    return ", ".join(f"PID {process_id}" for process_id in process_ids)


def _mismatch_notice(
    mismatched: Sequence[LoadedNativeBridge],
    expected_version: str,
) -> str:
    reported = ", ".join(
        f"PID {module.process_id}={module.version or 'unknown'}({module.path})"
        for module in mismatched[:_REPORTED_MODULE_LIMIT]
    )
    overflow = len(mismatched) - _REPORTED_MODULE_LIMIT
    suffix = f" 외 {overflow}건" if overflow > 0 else ""
    return (
        f"한/글이 기대와 다른 브리지를 로드했습니다: {reported}{suffix}"
        f"; 기대 {expected_version}. {_RESTART_HINT}"
    )


def native_bridge_runtime() -> NativeBridgeRuntime:
    """Read the bridge DLL live Hangul processes mapped. Never raises."""
    expected_version = RUNTIME_IDENTITY.native_bridge
    scan = scan_loaded_native_bridges()
    if scan.failure is not None:
        return NativeBridgeRuntime(
            native_bridge_state="unknown",
            native_bridge_notice=(
                f"한/글이 로드한 브리지를 확인하지 못했습니다: {scan.failure}"
                f"; 기대 {expected_version}"
            ),
        )
    if not scan.processes:
        return NativeBridgeRuntime(
            native_bridge_state="not_running",
            native_bridge_notice=(
                "실행 중인 한/글 프로세스가 없어 로드된 브리지를 확인하지 않았습니다"
                f"; 기대 {expected_version}"
            ),
        )
    expected_digest = packaged_native_bridge_digest()
    modules = tuple(
        LoadedNativeBridge(
            process_id=module.process_id,
            process_name=module.process_name,
            path=str(module.path),
            version=module.version,
            matches_expected=_module_matches_expected(
                module,
                expected_digest,
                expected_version,
            ),
        )
        for module in scan.modules
    )
    loaded_versions = tuple(
        dict.fromkeys(module.version or "unknown" for module in modules)
    )
    loaded = ", ".join(loaded_versions) if loaded_versions else None
    unreadable = frozenset(scan.unreadable_processes)
    carrying = {module.process_id for module in modules}
    missing = tuple(
        process_id
        for process_id, _ in scan.processes
        if process_id not in carrying and process_id not in unreadable
    )
    mismatched = tuple(module for module in modules if not module.matches_expected)
    # One response, one story. ``loaded_native_bridge`` is the union of the
    # versions actually mapped and ``native_bridge_state`` used to be the worst
    # case across processes, so a single editor holding 0.5.130 next to any
    # process without it printed loaded="0.5.130" and state="not_loaded" at the
    # same time -- two answers that cannot both be read as true. The order below
    # keeps them on one axis: ``loaded`` is non-null exactly when the state is
    # ``matched`` or ``mismatched``, and null for the other three.
    unread = (
        f"; 한/글 {_process_list(sorted(unreadable))}의 모듈 목록은 읽지 못했습니다"
        if unreadable
        else ""
    )
    without = (
        f"; 한/글 {_process_list(missing)}에는 아직 로드되지 않았습니다"
        if missing
        else ""
    )
    if mismatched:
        state = "mismatched"
        notice = _mismatch_notice(mismatched, expected_version)
    elif modules:
        state = "matched"
        notice = (
            f"한/글 {_process_list(sorted(carrying))}가 실제로 로드한 브리지는"
            f" {loaded}입니다; 기대 {expected_version}; matches_expected=true"
            f"{without}{unread}"
        )
    elif missing:
        state = "not_loaded"
        notice = (
            f"한/글 {_process_list(missing)}에 한컴 브리지 DLL이 로드되지 않았습니다"
            f"; 기대 {expected_version}{unread}"
        )
    else:
        state = "unknown"
        notice = (
            f"한/글 {_process_list(sorted(unreadable))}의 모듈 목록을 읽지 못했습니다"
            f"; 기대 {expected_version}"
        )
    return NativeBridgeRuntime(
        loaded_native_bridge=loaded,
        native_bridge_state=state,
        # Only ``mismatched`` is evidence that an old DLL is executing right
        # now, and only that is worth asking a user mid-document to restart
        # for. ``not_loaded`` says a Hangul started before this bridge was ever
        # registered; the next start picks it up on its own, and until then the
        # notice says so. Reported, never enforced -- see
        # docs/distribution-security.md.
        hangul_restart_required=state == "mismatched",
        native_bridge_notice=notice,
        loaded_native_bridge_modules=modules,
    )


def runtime_status(tools: Sequence[Tool]) -> RuntimeStatus:
    current_source_hash = current_runtime_source_hash()
    process_id = os.getpid()
    bridge = native_bridge_runtime()
    return RuntimeStatus(
        distribution=RUNTIME_IDENTITY.distribution,
        mcp=RUNTIME_IDENTITY.mcp,
        native_bridge=RUNTIME_IDENTITY.native_bridge,
        protocol=RUNTIME_IDENTITY.protocol,
        recipe_bundle_hash=RUNTIME_IDENTITY.recipe_bundle_hash,
        process_id=process_id,
        worker_process_id=process_id,
        source_path=str(PLUGIN_ROOT),
        python_executable=str(Path(sys.executable).resolve()),
        tool_schema_hash=tool_schema_hash(tools),
        tool_count=len(tools),
        generation=1,
        source_hash=current_source_hash,
        loaded_source_hash=RUNTIME_LOADED_SOURCE_HASH,
        reload_required=current_source_hash != RUNTIME_LOADED_SOURCE_HASH,
        loaded_native_bridge=bridge.loaded_native_bridge,
        native_bridge_state=bridge.native_bridge_state,
        hangul_restart_required=bridge.hangul_restart_required,
        native_bridge_notice=bridge.native_bridge_notice,
        loaded_native_bridge_modules=bridge.loaded_native_bridge_modules,
    )


def startup_runtime_record() -> str:
    return RuntimeStartupRecord(runtime=RUNTIME_IDENTITY).model_dump_json()
