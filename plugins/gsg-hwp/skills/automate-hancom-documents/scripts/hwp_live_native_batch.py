from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
import ctypes
from base64 import b64decode
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import struct
import uuid
from functools import wraps
from enum import IntEnum
from importlib import import_module
from threading import Lock, Thread, local
from time import monotonic, perf_counter_ns, time_ns
from typing import (
    Callable,
    Final,
    cast,
    final,
    Never,
    ParamSpec,
    Protocol,
    TypeVar,
    runtime_checkable,
)
from weakref import ReferenceType, ref

from pywintypes import com_error

from hwp_checkpoint_signature import checkpoint_signature_is_complete
from hwp_content_signature_refusal_store import (
    PersistedRefusal,
    drop_signature_refusals,
    load_signature_refusal,
    store_signature_refusal,
)
from hwp_errors import HwpLiveError, HwpTextPatchPreflightError
from hwp_live_native_action_contract import (
    decode_action_result,
    decode_detailed_inspection,
    decode_page_inspection,
    decode_page_inspection_batch,
    decode_paragraph_style_scan,
    decode_snapshot,
    encode_action_request,
    encode_paragraph_style_request,
)
from hwp_live_native_action_models import (
    NativeActionRequest,
    NativeActionResult,
    NativeDetailedInspection,
    NativePageInspection,
    NativeParagraphStyleScan,
    NativeSnapshot,
    NativeCharacterFormat,
    NativePosition,
    NativeSelection,
    PreparedTextPatchTarget,
    TextPatchCommand,
)
from hwp_live_native_batch_contract import (
    NATIVE_INSPECT_PAGES_MAX,
    NATIVE_PARAGRAPH_STYLE_SCAN_MAX,
    NativeBatchRequest,
    NativeBatchResult,
    NativeLifecycleResult,
    NativeSaveResult,
    decode_batch_result,
    decode_lifecycle_result,
    decode_save_result,
    encode_batch_request,
)
from _hwp_native_graph_primitive import (
    PrimitiveRecordFacts,
    consume_primitive_stream as consume_primitive_graph_stream,
)
from _hwp_native_graph_stream import (
    AuthenticatedGraphFrame,
    GraphStreamDecoder,
)
from _hwp_native_graph_wire import (
    GraphVersion as WireGraphVersion,
    MessageKind,
    count_graph_fragments,
    decode_frame,
    validate_supported_frame,
)
from hwp_native_graph_models import (
    NativeBlobChunkPayload,
    NativeBlobPackPayload,
    NativeBlobSlice,
    NativeCapabilitiesPayload,
    NativeDigest,
    NativeErrorPayload,
    NativeFrameVersion,
    NativeGraphQuery,
    NativeIdentifier,
    NativeInteger,
    NativeOpaque,
    NativeRawUtf16,
    RecordKind,
    SupportedNativeFrame,
    TypedNativeRecord,
)
from hwp_live_native_protocol_bundle import (
    NativeBundleReceipt,
    NativeBundleRequest,
    decode_bundle_receipt,
    encode_bundle_request,
)


class _Moniker(Protocol):
    def GetDisplayName(self, context: object, moniker: _Moniker) -> str: ...


class _Rot(Protocol):
    def EnumRunning(self) -> tuple[_Moniker, ...]: ...

    def GetObject(self, moniker: _Moniker) -> _DispatchSource: ...


@runtime_checkable
class _PythonCom(Protocol):
    IID_IDispatch: object

    def CoInitialize(self) -> None: ...

    def CoUninitialize(self) -> None: ...

    def CreateBindCtx(self, reserved: int) -> object: ...

    def GetRunningObjectTable(self) -> _Rot: ...


class _DispatchSource(Protocol):
    def QueryInterface(self, interface_id: object) -> object: ...


_NATIVE_DISPATCH_CACHE_TTL_SECONDS: Final = 0.5
_NATIVE_DISPATCH_CACHE_MAX_ENTRIES: Final = 16
_document_routes: dict[int, int] = {}
_document_route_generations: dict[int, int] = {}
# The window whose route was selected last, which is this worker's answer to
# "which document is the connected one" for callers that hold no handle of their
# own. Every live controller in this process routes through
# ``select_native_document_route`` when it makes a session current, so this
# follows the connected document across controllers, lanes and Hangul
# processes -- which the individual controllers cannot do, since each knows only
# the sessions it opened itself.
_current_document_route: int | None = None
_document_route_lock = Lock()
# The addon retains a capability session per (route, session uuid) until the
# route is invalidated; GraphClose is unsupported and never forgets it. A
# second NegotiateCapabilities for a bound session therefore returns
# ERROR_ALREADY_EXISTS. Remember what this process negotiated so repeated cold
# captures of the same content-derived session reuse the live capability
# session instead of re-handshaking it.
_negotiated_capability_sessions: dict[tuple[int, str], int] = {}
_negotiated_capability_lock = Lock()
_ERROR_ALREADY_EXISTS_HRESULT: Final = -2147024713
_NativeBoundaryParams = ParamSpec("_NativeBoundaryParams")
_NativeBoundaryResult = TypeVar("_NativeBoundaryResult")


@dataclass(frozen=True, slots=True)
class NativeDispatchCacheMetrics:
    cache_hits: int
    cache_misses: int
    rot_scans: int
    dispatch_creations: int
    invalidations: int
    lookup_nanoseconds: int


@dataclass(frozen=True, slots=True)
class NativeHistoryStepResult:
    applied: int
    elapsed_microseconds: int
    before_content_signature: str
    after_content_signature: str


@dataclass(frozen=True, slots=True)
class NativeTextPatchPreparation:
    content_revision: str
    target_count: int
    elapsed_microseconds: int
    targets: tuple[PreparedTextPatchTarget, ...] = ()


class NativeActivationCallError(HwpLiveError):
    pass


@dataclass(frozen=True, slots=True)
class _NativeDispatchCacheEntry:
    batch_ref: ReferenceType[_BatchDispatch]
    expires_at: float
    protocol_version: int


@dataclass(frozen=True, slots=True)
class _NativeDispatchOperationEntry:
    batch: _BatchDispatch
    protocol_version: int
    target_document_id: int


class _NativeDispatchThreadState(local):
    entries: dict[tuple[int, int, int | None, int], _NativeDispatchCacheEntry]
    operation_depth: int
    operation_entries: dict[
        tuple[int, int, int | None, int],
        _NativeDispatchOperationEntry,
    ]

    def __init__(self) -> None:
        self.entries = {}
        self.operation_depth = 0
        self.operation_entries = {}


_native_dispatch_state = _NativeDispatchThreadState()
_native_dispatch_metrics_lock = Lock()
_content_signature_lock = Lock()
_content_signature_cache: dict[int, str] = {}
_content_revision_cache: dict[int, str] = {}
# window -> (session tag, page count, control count, text length) as they stood
# when the engine last refused to serialise this document.
#
# This is deliberately NOT dropped by ``forget_cached_content_signatures``. The
# signature cache answers "what is this document's signature", which stops being
# true the moment anything is written; this answers "can the engine serialise a
# document this size at all", which a write does not change unless the document
# got smaller. Folding the two together is what made a refusing document pay the
# full serialisation attempt again after every single write.
_content_signature_refusals: dict[int, tuple[int, int, int, int]] = {}
# The windows whose durable refusal this process has already looked at. Adoption
# is per window and not per process: a worker that later binds a second window
# has not yet read that window's entry, and must be allowed to. Guarded by
# ``_content_signature_lock``; ``load_persisted_refusals_once`` is the only place
# that adds to it.
_persisted_refusal_windows: set[int] = set()
_window_freshness_lock = Lock()
# Engine change events observed per window during this worker's lifetime. The
# event hook thread bumps these, so writes stay one dictionary assignment.
_window_change_event_counts: dict[int, int] = {}
# (session tag, window) -> counter floor recovered from disk, loaded at most
# once per worker lifetime so quiet rebinds never touch the filesystem.
_window_freshness_floors: dict[tuple[int, int], int] = {}
# (session tag, window) -> highest counter value this worker has persisted.
_window_freshness_watermarks: dict[tuple[int, int], int] = {}
# The C++ side of the revision token, restated exactly. `[0-9]` and not `\d`:
# `\d` matches full-width and Arabic-Indic digits, which a wistringstream in the
# classic locale does not. The whitespace set is C's six characters and not
# Python's Unicode one, which additionally counts FS/GS/RS/US as separators.
_ASCII_DIGITS = re.compile(r"[0-9]+")
_C_WHITESPACE_CHARACTERS = " \t\n\v\f\r"
_C_WHITESPACE = re.compile(f"[{_C_WHITESPACE_CHARACTERS}]+")
# LONG is 32-bit signed in a Win32 build; the hash, length and counter fields
# are uint64. Past either edge the stream extractor sets failbit and the C++
# rule refuses, so this side has to refuse at the same place.
_LONG_MAX = 2**31 - 1
_UINT64_MAX = 2**64 - 1
_native_dispatch_cache_hits = 0
_native_dispatch_cache_misses = 0
_client_progress_start_ns = perf_counter_ns()


def _record_client_progress(stage: str, first: int = 0, second: int = 0) -> None:
    """Append bounded transfer telemetry without serializing document content."""
    path = os.environ.get("TODO18_CLIENT_PROGRESS_PATH")
    if not path:
        return
    now = perf_counter_ns()
    line = (
        f"{(now - _client_progress_start_ns) // 1_000_000}\t{now}\t"
        f"{stage}\t{first}\t{second}\r\n"
    ).encode("ascii")
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        # Telemetry is append-only diagnostic evidence, not publication state;
        # one bounded kernel write avoids putting an fsync in the transfer loop.
        _ = os.write(descriptor, line)
    finally:
        os.close(descriptor)


_native_dispatch_rot_scans = 0
_native_dispatch_creations = 0
_native_dispatch_invalidations = 0
_native_dispatch_lookup_nanoseconds = 0


def _record_native_dispatch_metrics(
    *,
    cache_hit: bool | None = None,
    rot_scan: bool = False,
    dispatch_creation: bool = False,
    invalidation: bool = False,
    lookup_nanoseconds: int = 0,
) -> None:
    global _native_dispatch_cache_hits
    global _native_dispatch_cache_misses
    global _native_dispatch_creations
    global _native_dispatch_invalidations
    global _native_dispatch_lookup_nanoseconds
    global _native_dispatch_rot_scans
    with _native_dispatch_metrics_lock:
        if cache_hit is True:
            _native_dispatch_cache_hits += 1
        elif cache_hit is False:
            _native_dispatch_cache_misses += 1
        if rot_scan:
            _native_dispatch_rot_scans += 1
        if dispatch_creation:
            _native_dispatch_creations += 1
        if invalidation:
            _native_dispatch_invalidations += 1
        _native_dispatch_lookup_nanoseconds += lookup_nanoseconds


def native_dispatch_cache_metrics() -> NativeDispatchCacheMetrics:
    with _native_dispatch_metrics_lock:
        return NativeDispatchCacheMetrics(
            cache_hits=_native_dispatch_cache_hits,
            cache_misses=_native_dispatch_cache_misses,
            rot_scans=_native_dispatch_rot_scans,
            dispatch_creations=_native_dispatch_creations,
            invalidations=_native_dispatch_invalidations,
            lookup_nanoseconds=_native_dispatch_lookup_nanoseconds,
        )


def reset_native_dispatch_cache_metrics() -> None:
    global _native_dispatch_cache_hits
    global _native_dispatch_cache_misses
    global _native_dispatch_creations
    global _native_dispatch_invalidations
    global _native_dispatch_lookup_nanoseconds
    global _native_dispatch_rot_scans
    with _native_dispatch_metrics_lock:
        _native_dispatch_cache_hits = 0
        _native_dispatch_cache_misses = 0
        _native_dispatch_rot_scans = 0
        _native_dispatch_creations = 0
        _native_dispatch_invalidations = 0
        _native_dispatch_lookup_nanoseconds = 0


def _route_state(window_handle: int) -> tuple[int | None, int]:
    with _document_route_lock:
        return (
            _document_routes.get(window_handle),
            _document_route_generations.get(window_handle, 0),
        )


def _discard_native_dispatch_entries(window_handle: int) -> None:
    entries = _native_dispatch_state.entries
    _native_dispatch_state.entries = {
        key: entry for key, entry in entries.items() if key[1] != window_handle
    }
    operation_entries = _native_dispatch_state.operation_entries
    _native_dispatch_state.operation_entries = {
        key: entry
        for key, entry in operation_entries.items()
        if key[1] != window_handle
    }


def forget_negotiated_capability_sessions(window_handle: int | None = None) -> None:
    """Drop remembered capability sessions for one window, or every window."""
    with _negotiated_capability_lock:
        if window_handle is None:
            _negotiated_capability_sessions.clear()
            return
        for key in [
            key for key in _negotiated_capability_sessions if key[0] == window_handle
        ]:
            del _negotiated_capability_sessions[key]


def _remembered_capability_session(window_handle: int, session: str) -> bool:
    with _negotiated_capability_lock:
        return (window_handle, session) in _negotiated_capability_sessions


def _remember_capability_session(
    window_handle: int, session: str, negotiated_bits: int
) -> None:
    with _negotiated_capability_lock:
        _negotiated_capability_sessions[(window_handle, session)] = negotiated_bits


def forget_cached_content_signatures(window_handle: int | None = None) -> None:
    """Drop reused whole-document signatures and revisions after a write.

    Both caches are dropped together on purpose. Every call site -- native
    write, reconnect, dispatch invalidation, and the engine change event a
    user's own typing raises -- means the same thing to both: what this worker
    remembers about the document is no longer known to be true. The event hook
    thread reaches this through ``note_native_window_content_change``, so it
    must stay two dictionary pops and nothing else.
    """
    with _content_signature_lock:
        if window_handle is None:
            _content_signature_cache.clear()
            _content_revision_cache.clear()
            return
        _ = _content_signature_cache.pop(window_handle, None)
        _ = _content_revision_cache.pop(window_handle, None)


def note_native_window_content_change(window_handle: int) -> None:
    """What the engine change event does to this worker, in one place.

    Two effects, both constant-time, because this runs on the event hook
    thread: the cached signature and revision token are dropped (a text edit
    must recapture them), and the window's freshness counter moves (a
    formatting-only edit leaves the recaptured token bit-identical, so the
    drop alone would let a graph generation published before the edit answer
    after it -- the counter is what ``bind_graph_query_content_freshness``
    folds into the epoch to prevent exactly that). Persisting the counter is
    deliberately NOT done here; the disk write waits for the next bind, which
    keeps keystrokes off the filesystem.
    """
    forget_cached_content_signatures(window_handle)
    with _window_freshness_lock:
        _window_change_event_counts[window_handle] = (
            _window_change_event_counts.get(window_handle, 0) + 1
        )


def _window_freshness_state_path(session_tag: int, window_handle: int) -> Path:
    root = Path(
        os.environ.get(
            "HWP_GRAPH_FRESHNESS_STATE_ROOT",
            str(
                Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                / "GSG-HWP"
                / "graph-freshness"
            ),
        )
    )
    # The session tag is the Hangul process id, so a restarted Hangul never
    # reads a watermark stamped by its predecessor; a recycled pid only costs
    # one republish. Window handles alone would collide across that restart.
    return root / f"window-{session_tag}-{window_handle}.watermark"


def _load_window_freshness_floor(path: Path) -> int:
    """Where the counter resumes after a worker restart.

    Events raised while no worker was alive are gone, so resuming AT the
    persisted watermark could re-issue an epoch bound before the restart with
    edits missing in between. Resuming one past it makes the first bind after
    a restart publish fresh, once -- a republish is cheaper than a stale
    answer. A file that exists but does not parse gets the same treatment at
    a distance: the true watermark is unknowable, and wall-clock nanoseconds
    dwarf any event count this counter can reach while still moving forward
    across repeated corruption.
    """
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeDecodeError):
        return time_ns()
    stripped = raw.strip()
    if not _ASCII_DIGITS.fullmatch(stripped):
        return time_ns()
    return int(stripped) + 1


def _persist_window_freshness_watermark(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as stream:
            _ = stream.write(f"{value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bind_window_content_freshness(session_tag: int, window_handle: int) -> int | None:
    """The counter value an epoch may bind, durably, or None if it cannot.

    The floor is read from disk at most once per worker lifetime and the
    watermark is rewritten only when the value advances, so a quiet stream of
    rebinds costs dictionary reads and nothing else. A watermark that cannot
    be written is a bind that must not be reused: the very next worker restart
    would revert the key and resurrect whatever was published against it, so
    the caller is told None and stays on the republish-every-time path --
    loud and slow beats silently stale.
    """
    key = (session_tag, window_handle)
    with _window_freshness_lock:
        observed = _window_change_event_counts.get(window_handle, 0)
        floor = _window_freshness_floors.get(key)
    if floor is None:
        loaded = _load_window_freshness_floor(
            _window_freshness_state_path(session_tag, window_handle)
        )
        with _window_freshness_lock:
            floor = _window_freshness_floors.setdefault(key, loaded)
            observed = _window_change_event_counts.get(window_handle, 0)
    effective = floor + observed
    with _window_freshness_lock:
        persisted = _window_freshness_watermarks.get(key)
    if persisted is None or persisted < effective:
        try:
            _persist_window_freshness_watermark(
                _window_freshness_state_path(session_tag, window_handle),
                effective,
            )
        except OSError:
            return None
        with _window_freshness_lock:
            current = _window_freshness_watermarks.get(key)
            if current is None or current < effective:
                _window_freshness_watermarks[key] = effective
    return effective


def _remember_content_signature(window_handle: int, signature: str) -> None:
    with _content_signature_lock:
        _content_signature_cache[window_handle] = signature


def _cached_content_signature(window_handle: int) -> str | None:
    with _content_signature_lock:
        return _content_signature_cache.get(window_handle)


def peek_cached_content_signature(window_handle: int) -> str | None:
    """Return a complete cached ``SIG ...`` without calling the engine.

    ``None`` means the cache is empty or the remembered answer was incomplete.
    This never starts ``ContentSignature`` serialisation.
    """
    cached = _cached_content_signature(window_handle)
    if cached is None or not checkpoint_signature_is_complete(cached):
        return None
    return cached


def forget_content_signature_refusals(window_handle: int | None = None) -> None:
    """Forget that the engine could not serialise this document.

    Called where the worker stops knowing which document a window holds -- a
    rebind. Ordinary writes deliberately do NOT reach here; that is the whole
    difference between this and ``forget_cached_content_signatures``.

    The durable copy goes with it, so a rebind means the same thing to the next
    worker as it does to this one. The single exception is a whole-memory clear
    issued before this process has adopted any durable copy: the first
    activation of a worker is not a rebind, because there is nothing it has
    stopped knowing, and a memory this process has never read is not a memory it
    may invalidate. ``load_persisted_refusals_once`` runs immediately after that
    clear and is what records the window, so every later clear -- every actual
    rebind -- drops disk and memory together.

    The rebind caller is ``forget_content_signature_refusals_on_rebind``, which
    narrows the durable half to the window being rebound; this whole-ledger form
    stays for the callers that mean the whole ledger.
    """
    with _content_signature_lock:
        if window_handle is None:
            _content_signature_refusals.clear()
            drop_durable = bool(_persisted_refusal_windows)
        else:
            _ = _content_signature_refusals.pop(window_handle, None)
            drop_durable = True
    if drop_durable:
        drop_signature_refusals(window_handle)


def forget_content_signature_refusals_on_rebind(window_handle: int) -> None:
    """A rebind of one window: clear memory, drop **that window's** entry only.

    The memory half is unchanged -- the whole in-process dictionary goes, as it
    always did on an activation. Forgetting too much in memory only costs this
    worker a repeated engine attempt on a window it is no longer bound to.

    The durable half is narrowed to the window being rebound. The ledger on disk
    is shared by every worker on this machine, so the entries in it describe
    windows of 한/글 instances this process may have nothing to do with -- a
    second MCP server, a QA harness, a worker that restarted beside this one.
    Sweeping the whole ledger from here deleted their memory as a side effect of
    our rebind, and the next thing they did was buy the 17.2s serialisation
    attempt again. A rebind is a statement about one window; this now says only
    that.

    The entry is dropped only if this process had adopted it (the window is in
    ``_persisted_refusal_windows``). A first activation is not a rebind, so it
    must leave the entry alone for ``load_persisted_refusals_once`` to read.
    """
    with _content_signature_lock:
        _content_signature_refusals.clear()
        drop_durable = window_handle in _persisted_refusal_windows
    if drop_durable:
        drop_signature_refusals(window_handle)


def load_persisted_refusals_once(window_handle: int) -> bool:
    """Adopt a previous worker's refusal for this window, at most once.

    True means this call was the first for this window and the ledger entry was
    consulted; False means some earlier call already did it and nothing was
    read. The caller is ``_activate_connection``, right after its rebind clear,
    passing the window it is activating, so exactly one activation per window
    pays one file read.

    Only that window's entry is touched -- read, judged, and dropped on a
    mismatch. Why the ledger may not be walked from here is on
    ``_adopt_persisted_refusal``.

    A durable entry is adopted only onto the document it was measured on. The
    entry carries the session tag and the revision token's two hashes; this
    reads the token once for the activating window and keeps the entry only when
    all three still match. Anything else -- a different document in that
    window, 한/글 restarted, an edit since the refusal, a token that cannot be
    read -- drops the entry from disk and adopts nothing, which leaves the
    caller exactly where this repair found it: no memory, so the engine is
    asked, once.

    That gate is the whole correctness argument, and it is not the same one the
    size rule makes. What the engine gives up on is embedded byte volume
    (OfficialApiState.cpp :803-805, "HWPML serialisation is proportional to what
    was embedded" -- an 82MB save signs, a 317MB one does not), and no field of
    the revision token measures that. Page count, control count and text length
    do not even correlate with it in the right direction: an image-heavy 317MB
    document that refuses can carry *fewer* pages, controls and characters than
    a 3MB text report that signs. So a refusal may never be extrapolated onto a
    document that is merely no smaller by those three -- doing so answers "this
    document cannot be signed" about a document the engine would have signed,
    and hands the caller a wrong notice with it. Within one binding the size
    rule is still sound, because there the document's identity is what the
    rebind clear guarantees; across a restart, identity has to be proved, and
    the hashes are the proof.

    Reading the token is a native call, so it is done under the same best
    effort as everything else here: a window whose token cannot be read is
    dropped rather than adopted, and nothing raises out of this function --
    an activation must not fail because a cache could not be warmed.

    Existing memory wins over the disk. A refusal this process observed itself
    was measured against the document as it stands now; the durable one only
    describes what a previous process saw.
    """
    with _content_signature_lock:
        if window_handle in _persisted_refusal_windows:
            return False
        _persisted_refusal_windows.add(window_handle)
    try:
        persisted = load_signature_refusal(window_handle)
        if persisted is not None:
            _adopt_persisted_refusal(window_handle, persisted)
    except Exception:  # noqa: BLE001 - 캐시 예열이 연결을 깨뜨리면 안 된다
        return True
    return True


def _adopt_persisted_refusal(window_handle: int, persisted: PersistedRefusal) -> None:
    """Keep this window's durable entry if its document is still the one in it.

    One window, because the ledger is shared and the dispatch is not owned. The
    entries on disk are keyed by window handle across every worker on this
    machine, while ``_batch_for_window`` (:1615) resolves a handle to a process
    with ``GetWindowThreadProcessId`` and will happily build a dispatch into
    *any* live 한/글 -- including an instance this process never bound. Judging
    the whole ledger from one activation therefore sent ``ContentRevision`` (a
    full control walk plus text extraction, ~6s of frozen UI on a 317MB
    document) into other people's 한/글, and deleted their entry when the answer
    did not match ours. The window being activated is the only one this process
    has anything to say about, so it is the only one judged here; every other
    entry is judged by the process that services its instance, on its own
    activation, by this same rule. The rule itself is unchanged, so the answer
    for any given window is unchanged with it.
    """
    measures, identity = persisted
    try:
        revision = read_native_content_revision(window_handle)
    except (HwpLiveError, OSError, ValueError):
        revision = None
    if (
        revision is None
        or not content_revision_is_complete(revision)
        or _revision_size_measures(revision)[0] != measures[0]
        or _revision_content_identity(revision) != identity
    ):
        drop_signature_refusals(window_handle)
        return
    with _content_signature_lock:
        _ = _content_signature_refusals.setdefault(window_handle, measures)


def _revision_size_measures(revision: str) -> tuple[int, int, int, int]:
    """(session tag, page count, control count, text length) out of a `REV`.

    Only complete tokens reach here; ``content_revision_is_complete`` has
    already refused everything this would have to guess about.
    """
    parts = _C_WHITESPACE.split(revision.strip(_C_WHITESPACE_CHARACTERS))
    return int(parts[6]), int(parts[1]), int(parts[2]), int(parts[5])


def _revision_content_identity(revision: str) -> tuple[int, int]:
    """(control hash, text hash) out of a `REV` -- which document this is.

    The two fields ``_revision_size_measures`` throws away. Sizes bound how big
    a document is; these two say *which* document it is, which is what a
    remembered refusal has to be pinned to before it may be reused across a
    worker restart. Same field order as ``content_revision_is_complete``, and
    the same precondition: only complete tokens reach here.
    """
    parts = _C_WHITESPACE.split(revision.strip(_C_WHITESPACE_CHARACTERS))
    return int(parts[3]), int(parts[4])


def _remember_signature_refusal(window_handle: int, revision: str | None) -> None:
    """Record how big the document was when the engine refused it.

    Without a usable revision token there is nothing to compare a later
    document against, so nothing is remembered and the next caller pays the
    engine again. That is the honest outcome: a refusal this code cannot
    describe is a refusal it must not extrapolate from.

    The same measures go to disk so the next worker starts where this one
    ended, and the token's two hashes go with them: the durable copy may only
    be reused on the document it was measured on, and those hashes are what
    says so (``load_persisted_refusals_once``). Remembering is best effort in
    both places: the store swallows its own I/O failures and answers False, and
    a False here costs one repeated serialisation attempt after a restart --
    never this edit.
    """
    if revision is None or not content_revision_is_complete(revision):
        forget_content_signature_refusals(window_handle)
        return
    measures = _revision_size_measures(revision)
    with _content_signature_lock:
        _content_signature_refusals[window_handle] = measures
    _ = store_signature_refusal(
        window_handle, measures, _revision_content_identity(revision)
    )


def _engine_still_refuses(window_handle: int) -> bool:
    """Whether a remembered refusal still applies, without asking the engine.

    The expensive answer is ``ContentSignature``: the engine serialises the
    whole document and, past its own memory ceiling, spends the entire attempt
    to return S_OK with an empty string -- 17.2s measured on a 317MB document,
    on the 한/글 UI thread. ``forget_cached_content_signatures`` drops the
    remembered refusal along with the signature, so before this every write
    made the next caller buy that same verdict again.

    The cheap answer is the revision token (measured at 0.024s on the same
    document), which carries three sizes the refusal can be judged against:
    page count, control count and text length. The rule is one-directional on
    purpose. A document that has not shrunk by any of the three cannot have
    become easier to serialise, so the refusal stands and is not re-bought. If
    any of them dropped -- or the token cannot be read, or 한/글 restarted and
    the session tag moved -- the memory is thrown away and the engine is asked
    again.

    Being wrong costs one missing MCP undo entry, which the callers already
    announce, and it self-heals on the next call where a measure drops. Being
    absent costs 17.2s of a frozen 한/글 per write. That asymmetry is why the
    skip is allowed to be approximate while the retry is allowed to be eager.
    """
    with _content_signature_lock:
        remembered = _content_signature_refusals.get(window_handle)
    if remembered is None:
        return False
    revision = read_native_content_revision(window_handle)
    if revision is None or not content_revision_is_complete(revision):
        forget_content_signature_refusals(window_handle)
        return False
    measured = _revision_size_measures(revision)
    if measured[0] != remembered[0] or any(
        now < then for now, then in zip(measured[1:], remembered[1:], strict=True)
    ):
        forget_content_signature_refusals(window_handle)
        return False
    return True


def engine_refuses_content_signature(window_handle: int) -> bool:
    """Whether this document is already known to be one the engine cannot sign.

    True is a fact this worker paid for once and may reuse; False is "not
    known to refuse", never "will succeed". Nothing here starts a
    ``ContentSignature`` serialisation, which is the whole point: the callers
    are deciding whether to *begin* work whose only product is a signature, so
    an answer that costs the serialisation would defeat the question.

    The cached signature is consulted before the refusal memory because it is
    the stronger statement of the two -- it describes this exact document
    state, where the refusal memory only bounds the sizes at which the engine
    gave up -- and because reading it costs a dictionary lookup.

    A revision token that cannot be read answers False rather than raising.
    The callers ask this *before* an edit, to decide whether to skip work; an
    exception here would stop the edit itself, which is the very failure this
    question exists to prevent. Not knowing costs a checkpoint attempt that
    may be discarded, and that path already handles being discarded.
    """
    with _content_signature_lock:
        cached = _content_signature_cache.get(window_handle)
    if cached is not None:
        return not checkpoint_signature_is_complete(cached)
    try:
        return _engine_still_refuses(window_handle)
    except (HwpLiveError, OSError, ValueError):
        return False


def remembered_content_signature_refusal(
    window_handle: int,
) -> tuple[int, int, int, int] | None:
    """The refusal this worker already holds for a window, without any I/O.

    One dictionary lookup under the lock every other reader takes, and nothing
    else -- no revision token, no ``ContentSignature``, no disk. That is the
    whole reason it exists next to ``engine_refuses_content_signature``, which
    answers a related question but cannot answer it for free: on a cache miss
    that function falls into ``_engine_still_refuses`` (:761), which reads
    ``ContentRevision`` -- a native call measured at about 6s on a 317MB
    document. A caller that already carries its own, cheaper proof that a
    remembered refusal still describes the document needs the memory itself,
    not a re-derivation of it.

    ``None`` means nothing is remembered, never "the engine would sign this".
    The tuple is exactly what ``_remember_signature_refusal`` (:734) stored, in
    the order ``_revision_size_measures`` (:711) produces it: session tag, page
    count, control count, text length.
    """
    with _content_signature_lock:
        return _content_signature_refusals.get(window_handle)


def remember_content_signature_refusal(window_handle: int) -> None:
    """Record a refusal this worker observed somewhere other than here.

    The document checkpoint capture asks the bridge for the very same
    signature inside the native call (ActionLifecycle.cpp,
    ``CaptureDocumentFileCheckpoint``), so on a document past the engine's
    memory ceiling it spends the whole serialisation attempt and hands back a
    checkpoint whose meta line has no usable ``SIG``. That verdict never
    reached ``read_native_content_signature``, so the memory it maintains never
    heard about it and the next edit bought the same attempt again -- through
    the capture, once more per checkpoint.

    The revision token is read first, for the reason spelled out in
    ``read_native_content_signature``: a bridge too old to answer
    ``ContentRevision`` invalidates the native dispatch, and that invalidation
    drops this window's signature cache. It is also the guard against
    extrapolating from a checkpoint that is unsigned for some *other* reason --
    an older bridge's legacy block carries no signature at all, and such a
    bridge has no revision token either, so nothing is remembered and the next
    caller asks the engine directly.

    Remembering is best effort. The callers run this between a capture and an
    edit, so a token read that fails must cost one repeated capture attempt and
    never the edit itself.
    """
    try:
        revision = read_native_content_revision(window_handle)
    except (HwpLiveError, OSError, ValueError):
        forget_content_signature_refusals(window_handle)
        return
    _remember_content_signature(window_handle, "")
    _remember_signature_refusal(window_handle, revision)


def _remember_content_revision(window_handle: int, revision: str) -> None:
    with _content_signature_lock:
        _content_revision_cache[window_handle] = revision


def _cached_content_revision(window_handle: int) -> str | None:
    with _content_signature_lock:
        return _content_revision_cache.get(window_handle)


def content_revision_is_complete(raw: str) -> bool:
    """Whether a `REV` token carries every component the bridge can observe.

    Mirrors ``DocumentContentRevisionIsComplete`` in OfficialApiState.cpp, and
    the numeric lexing has to mirror it too. ``int()`` and ``str.split()`` are
    both far more generous than a C++ ``wistringstream`` in the classic locale,
    and every place they disagreed accepted a token the bridge would refuse:
    ``1_0`` (digit-group underscores), values past the LONG or uint64 range that
    the stream extractor rejects by setting failbit, full-width and Arabic-Indic
    digits, ``-0`` (which ``int`` folds to a non-negative zero), and separators
    such as FS that Python calls whitespace and C does not. So fields are matched
    as bare ASCII digits and range-checked against the exact C++ field widths,
    and the split uses the C whitespace set rather than the Unicode one.

    A zero text length is refused rather than read as an empty document: the
    bridge cannot tell an empty document from a text read that failed without
    the whole-document scan this token exists to avoid, so it declines and the
    caller recomputes. A zero session tag means the bridge never stamped the
    token, which no live capture does.

    One deliberate asymmetry remains, in the safe direction: a field written
    ``+7`` is refused here, where the stream extractor's strtoul-style sign
    handling would take it. The bridge formats with ``std::to_wstring`` and can
    never emit one, so refusing costs a recomputation and risks nothing.
    """
    parts = _C_WHITESPACE.split(raw.strip(_C_WHITESPACE_CHARACTERS))
    if len(parts) != 8 or parts[0] != "REV":
        return False
    if not all(_ASCII_DIGITS.fullmatch(part) for part in parts[1:]):
        return False
    (
        page_count,
        control_count,
        control_hash,
        text_hash,
        text_length,
        session_tag,
        write_epoch,
    ) = (int(part) for part in parts[1:])
    # The two LONG fields and the five uint64 fields, at exactly the widths the
    # C++ stream extractor accepts. Nothing needs a `>= 0` guard: the digit
    # match above already refused every sign.
    if page_count > _LONG_MAX or control_count > _LONG_MAX:
        return False
    if any(
        value > _UINT64_MAX
        for value in (control_hash, text_hash, text_length, session_tag, write_epoch)
    ):
        return False
    return page_count >= 1 and text_length > 0 and session_tag > 0


def _graph_query_store_epoch(signature: str) -> str:
    if len(signature) <= 64:
        return signature
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def bind_graph_query_content_freshness(
    window_handle: int,
    request: NativeGraphQuery,
) -> tuple[NativeGraphQuery, bool]:
    """Bind a complete revision token into store_epoch for cache freshness.

    Every graph tool this worker serves takes this path. ``hwp_mcp.main``
    builds the server with the deferred production graph client, which resolves
    the connected window per query and goes through
    ``production_native_graph_client``; that client binds here before it asks
    the cache anything. So ``hwp_get_graph_manifest``, ``hwp_query_graph``,
    ``hwp_get_graph_node``, ``hwp_get_graph_property`` and
    ``hwp_get_graph_asset`` all decide reuse on a token read. The paths that
    must prove two documents identical rather than date one -- undo/redo
    preconditions, checkpoint restore, rollback evidence -- keep calling
    ``read_native_content_signature`` and are untouched by this.

    This asks how old the document is, not what is in it, so it reads the cheap
    revision token rather than the whole-document signature the checkpoint and
    history paths still require. Reading a signature here cost 6.2s on a 34-page
    82MB document and 12.9s on a 102-page one, every time, because a write
    empties the cache -- and the answer was thrown away except as a cache key.

    An unusable token is not a reason to reuse anything: the caller is told the
    generation is not reusable and republishes, exactly as it did when a
    signature was unavailable.

    What the token alone cannot see, and what covers it. The signature carried
    a normalized HWPML document hash; the token does not, and that one field is
    the whole of the difference. A change the token is blind to -- character,
    paragraph and table formatting including borders and shading, control
    geometry and ordering, an image replaced in place, style definitions, and
    page setup that leaves the page count alone -- moves the token's write
    epoch only when the write goes through the bridge's Invoke. When it goes
    through Hangul's own automation instead, which ``HAction.Execute``/
    ``HAction.Run`` callers such as hwp_live_table_format.py:52,
    hwp_page_setup.py:123 and hwp_live_caption_edit.py:21 do, or when a person
    edits by hand, it reaches this worker only as an engine change event. That
    event bumps the window's freshness counter
    (``note_native_window_content_change``), and the epoch binds the counter
    next to the token, so the recaptured-but-bit-identical token no longer
    re-issues the pre-edit key. Two dependencies remain and are accepted:
    events arrive only while HancomEventBridge is alive, and events raised
    while no worker was alive are gone -- the persisted watermark answers the
    second by resuming the counter past everything ever bound, at the price of
    one republish per worker restart. A caller that must not trust the event
    channel at all wants ``read_native_content_signature``.

    In this worker that gap is narrower than the list suggests, and the reason
    is worth stating exactly rather than hoping. The token's write epoch --
    ``gWriteEpoch`` in BatchAutomation.cpp -- is bumped around every member
    ``MemberWritesDocument`` names, and the production formatting tools all
    reach the document through one of them: ``hwp_format_text``,
    ``hwp_format_table``, ``hwp_merge_table_cells``, ``hwp_split_table_cell``
    and ``hwp_apply_style`` run the native format contract in
    hwp_live_native_format_recipe.py, which writes via ``execute_native_actions``
    -> ``ExecuteActions``. A formatting-only write issued by this worker
    therefore does move the token.

    The ``HAction.Execute``/``HAction.Run`` callers that bypass the bridge are
    hwp_live_table_format.py, hwp_page_setup.py, hwp_live_caption_edit.py and
    hwp_assembly.py. None of them is a formatting-only write the worker can
    serve stale: an AST import closure over hwp_mcp (352 local modules, 314
    reachable) puts hwp_page_setup and hwp_assembly outside the worker entirely
    -- they are reachable only from the hwp_automation CLI -- and the other two
    are reachable only inside ``insert_table``, which adds a table and its text
    and so moves the token's control count, control hash, text hash and text
    length regardless.

    What is left uncovered is a change this process never issued: a person
    editing by hand, or a sibling worker driving the same Hangul outside the
    bridge. This is a real hole, not a narrowed one. The engine change event
    drops this worker's cached token, but the token it then re-reads is
    byte-identical when the edit was formatting-only, so a generation published
    before the edit stays reusable; and the event path depends on
    HancomEventBridge being alive at all. Closing it needs either a normalized
    HWPML hash in the token (a C++ change this lane did not make) or an
    event-driven epoch salt that survives a worker restart (a persistence layer
    this lane did not build). Nothing here pretends otherwise.

    ``read_native_content_signature`` is not a general escape from that hole.
    On documents large enough to matter the engine answers ``S_OK`` with an
    empty string -- measured at 17.4s and no usable signature on a 38-page
    document -- so a caller that switches to it to catch a manual formatting
    change gets neither the change nor an error.

    The C++ comments at BatchAutomation.cpp:98-106 and OfficialApiState.h:84-90
    still say a caller who needs to notice a formatting change needs a
    signature, without the qualification above. They are stale in that respect
    and were left alone: this lane made no C++ change.

    The graph route stays audit-only regardless. Structure reads keep the legacy
    path -- a warm graph check measured 518 to 739 times slower -- and this
    freshness bind is what makes the audit route cheap enough to re-ask, not an
    invitation to move reads onto it.

    The epoch is the token and nothing else, deliberately. Anything mixed in
    from this process's memory -- a route generation, a write counter -- returns
    to its starting value when the worker restarts while Hangul keeps running,
    and the epoch then returns to a key some earlier generation was already
    published under. The token cannot do that: its session tag is the Hangul
    process id, so it changes when Hangul restarts and is stable when it does
    not.
    """
    revision = read_native_content_revision(window_handle)
    if revision is None or not content_revision_is_complete(revision):
        return request, False
    session_tag = int(_C_WHITESPACE.split(revision.strip(_C_WHITESPACE_CHARACTERS))[6])
    freshness = _bind_window_content_freshness(session_tag, window_handle)
    if freshness is None:
        return request, False
    epoch = _graph_query_store_epoch(f"{revision} FRESH {freshness}")
    if request.store_epoch == epoch:
        return request, True
    return request.model_copy(update={"store_epoch": epoch}), True


def _invalidate_native_dispatch(window_handle: int) -> None:
    with _document_route_lock:
        _document_route_generations[window_handle] = (
            _document_route_generations.get(window_handle, 0) + 1
        )
    _discard_native_dispatch_entries(window_handle)
    forget_cached_content_signatures(window_handle)
    # The addon erases capability sessions with the route, so this process must
    # forget them too and re-negotiate on the next cold open.
    forget_negotiated_capability_sessions(window_handle)
    _record_native_dispatch_metrics(invalidation=True)


def _invalidate_native_dispatch_on_exception(
    function: Callable[_NativeBoundaryParams, _NativeBoundaryResult],
) -> Callable[_NativeBoundaryParams, _NativeBoundaryResult]:
    @wraps(function)
    def guarded(
        *args: _NativeBoundaryParams.args,
        **kwargs: _NativeBoundaryParams.kwargs,
    ) -> _NativeBoundaryResult:
        if not args and "window_handle" not in kwargs:
            return function(*args, **kwargs)
        window_handle = args[0] if args else kwargs["window_handle"]
        if not isinstance(window_handle, int):
            return function(*args, **kwargs)
        _, starting_generation = _route_state(window_handle)
        try:
            return function(*args, **kwargs)
        except Exception:
            _, current_generation = _route_state(window_handle)
            if current_generation == starting_generation:
                _invalidate_native_dispatch(window_handle)
            else:
                _discard_native_dispatch_entries(window_handle)
            raise

    return guarded


def _raise_native_call_error(
    window_handle: int,
    message: str,
    error: BaseException,
    failure_type: type[HwpLiveError] = HwpLiveError,
) -> Never:
    _invalidate_native_dispatch(window_handle)
    detail = ""
    if isinstance(error, com_error) and error.args and isinstance(error.args[0], int):
        detail = f" (HRESULT 0x{error.args[0] & 0xFFFFFFFF:08X})"
    elif str(error):
        detail = f" ({type(error).__name__}: {str(error)[:500]})"
    raise failure_type(message + detail) from error


def current_native_document_window() -> int | None:
    """The window handle of the document this worker last made current.

    ``None`` before any session is connected and after the last one is
    released. Callers that hold their own handle should keep using it; this
    exists for the ones that legitimately have none -- the graph tools, whose
    MCP arguments name a store and a capture session but never a window.
    """
    with _document_route_lock:
        return _current_document_route


def select_native_document_route(window_handle: int, document_id: int) -> None:
    global _current_document_route
    if window_handle <= 0 or document_id <= 0:
        raise ValueError("한컴 네이티브 문서 라우팅 값은 양수여야 합니다")
    route_changed = False
    with _document_route_lock:
        previous = _document_routes.get(window_handle)
        _document_routes[window_handle] = document_id
        _current_document_route = window_handle
        if previous != document_id:
            route_changed = True
            _document_route_generations[window_handle] = (
                _document_route_generations.get(window_handle, 0) + 1
            )
    if route_changed:
        _discard_native_dispatch_entries(window_handle)
        _release_pinned_graph_transfer(window_handle)


def clear_native_document_route(window_handle: int) -> None:
    global _current_document_route
    with _document_route_lock:
        _ = _document_routes.pop(window_handle, None)
        _document_route_generations[window_handle] = (
            _document_route_generations.get(window_handle, 0) + 1
        )
        if _current_document_route == window_handle:
            _current_document_route = None
    _discard_native_dispatch_entries(window_handle)
    _release_pinned_graph_transfer(window_handle)


class _GraphInstrumentation(Protocol):
    def count_call(self, name: str, count: int = 1) -> None: ...
    def observe_frame(self, raw: bytes) -> bool: ...
    def observe_decoded_frame(
        self, frame: SupportedNativeFrame, byte_count: int
    ) -> bool: ...
    def observe_authenticated_graph_chunk(
        self, byte_count: int, fragment_count: int
    ) -> bool: ...
    def observe_authenticated_graph_terminal(
        self,
        byte_count: int,
        record_count: int,
        logical_bytes: int,
        stream_digest: bytes,
    ) -> None: ...
    def observe_blob(self, blob: NativeBlobSlice, value: bytes) -> None: ...
    def cursor_closed(self, *, cancelled: bool = False) -> None: ...


@runtime_checkable
class _TypedOleDispatch(Protocol):
    def InvokeTypes(
        self,
        dispid: int,
        lcid: int,
        flags: int,
        return_type: tuple[int, int],
        argument_types: tuple[tuple[int, int], ...],
        *args: object,
    ) -> object: ...


class _GraphPropertyDispatch(Protocol):
    _oleobj_: _TypedOleDispatch

    @property
    def GraphProtocolVersion(self) -> int: ...

    @property
    def GraphCapabilities(self) -> object: ...


class GraphComMember(IntEnum):
    PROTOCOL_VERSION = 25
    CAPABILITIES = 26
    GRAPH_OPEN = 27
    GRAPH_NEXT = 28
    GRAPH_CANCEL = 29
    GRAPH_CLOSE = 30
    PATCH_BEGIN = 31
    PATCH_CHUNK = 32
    PATCH_COMMIT = 33
    PATCH_ABORT = 34
    BLOB_READ = 37


_CAPABILITY_GRAPH_READ: Final = 0x01
_CAPABILITY_ASSET_READ: Final = 0x08
_BLOB_READ_CAPABILITIES: Final = _CAPABILITY_GRAPH_READ | _CAPABILITY_ASSET_READ
_GRAPH_BYTE_ARRAY_VARTYPE: Final = 0x2000 | 17
_GRAPH_DISPATCH_METHOD: Final = 1
_GRAPH_PARAM_IN: Final = 1


def _immutable_graph_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        view = memoryview(value)
    elif isinstance(value, memoryview):
        view = cast("memoryview[int]", value)
    else:
        raise HwpLiveError("한컴 네이티브 그래프 응답이 byte SAFEARRAY가 아닙니다")
    if view.ndim != 1 or view.itemsize != 1 or not view.c_contiguous:
        raise HwpLiveError(
            "한컴 네이티브 그래프 응답 SAFEARRAY 모양이 올바르지 않습니다"
        )
    return view.tobytes()


@final
class GraphComInvocationAdapter:
    """Typed protocol-15 IDispatch ABI; no dynamic member lookup is used."""

    __slots__ = ("_dispatch", "_ole")

    def __init__(self, dispatch: object) -> None:
        try:
            raw = cast(object, object.__getattribute__(dispatch, "_oleobj_"))
        except AttributeError as error:
            raise HwpLiveError(
                "한컴 네이티브 그래프 typed IDispatch를 찾을 수 없습니다"
            ) from error
        if not isinstance(raw, _TypedOleDispatch):
            raise HwpLiveError("한컴 네이티브 그래프 typed IDispatch 계약이 다릅니다")
        self._dispatch = cast(_GraphPropertyDispatch, dispatch)
        self._ole = raw

    def protocol_version(self) -> int:
        value = cast(object, self._dispatch.GraphProtocolVersion)
        if isinstance(value, bool) or not isinstance(value, int):
            raise HwpLiveError("한컴 네이티브 그래프 프로토콜 버전 형식이 다릅니다")
        return value

    def capabilities(self) -> bytes:
        return _immutable_graph_bytes(self._dispatch.GraphCapabilities)

    def negotiate(self, session_id: str, requested_bits: int) -> bytes:
        return _immutable_graph_bytes(
            self._ole.InvokeTypes(
                GraphComMember.CAPABILITIES,
                0,
                _GRAPH_DISPATCH_METHOD,
                (_GRAPH_BYTE_ARRAY_VARTYPE, 0),
                ((8, _GRAPH_PARAM_IN), (21, _GRAPH_PARAM_IN)),
                session_id,
                requested_bits,
            )
        )

    def request(self, member: GraphComMember, request: bytes) -> bytes:
        if member < GraphComMember.GRAPH_OPEN or member > GraphComMember.BLOB_READ:
            raise HwpLiveError("한컴 네이티브 그래프 request DISPID가 아닙니다")
        return _immutable_graph_bytes(
            self._ole.InvokeTypes(
                member,
                0,
                _GRAPH_DISPATCH_METHOD,
                (_GRAPH_BYTE_ARRAY_VARTYPE, 0),
                ((_GRAPH_BYTE_ARRAY_VARTYPE, _GRAPH_PARAM_IN),),
                request,
            )
        )


class _BatchDispatch(Protocol):
    @property
    def ProtocolVersion(self) -> int: ...

    @property
    def TargetDocumentID(self) -> int: ...

    @property
    def GraphProtocolVersion(self) -> int: ...

    def GraphCapabilities(self, session_id: str, requested_bits: int) -> bytes: ...

    def GraphOpen(self, request: bytes) -> bytes: ...

    def GraphNext(self, request: bytes) -> bytes: ...

    def GraphCancel(self, request: bytes) -> bytes: ...

    def GraphClose(self, request: bytes) -> bytes: ...

    def BlobRead(self, request: bytes) -> bytes: ...

    def Execute(self, payload: str) -> str: ...

    def ExecuteProtocolBundle(self, payload: str) -> str: ...

    def ExecuteActions(self, payload: str) -> str: ...

    def ExecuteActionsChecked(
        self,
        payload: str,
        expected_content_signature: str,
    ) -> str: ...

    def PrepareTextPatches(self, payload: str) -> str: ...

    def PrepareLiteralTextPatches(self, payload: str) -> str: ...

    def ExecutePreparedTextPatches(
        self,
        payload: str,
        expected_content_revision: str,
    ) -> str: ...

    def ExecuteHistory(
        self,
        direction: str,
        expected_content_signature: str,
    ) -> str: ...

    def Snapshot(self) -> str: ...

    def ContentSignature(self) -> str: ...

    def ContentRevision(self) -> str: ...

    def InspectPage(self, page: int) -> str: ...

    def InspectPageV3(self, page: int) -> str: ...

    def InspectPageSummary(self, page: int) -> str: ...

    def InspectPagesV3(self, pages: str) -> str: ...

    def InspectRoutingContext(self, page_hint: int) -> str: ...

    def InspectStructure(self, page: int) -> str: ...

    def InspectParagraphStyles(self, scan: str) -> str: ...

    def ProbeOfficialApi(self, payload: str) -> str: ...

    def SaveReopenVerify(self) -> str: ...

    def SaveVerify(self) -> str: ...

    def ActivateDocument(self, document_id: int) -> str: ...

    def ActivationStatus(self, document_id: int) -> int: ...

    def BeginForegroundGuard(self) -> str: ...

    def EndForegroundGuard(self) -> str: ...


def enter_native_dispatch_operation_scope() -> None:
    state = _native_dispatch_state
    if state.operation_depth == 0:
        state.operation_entries.clear()
    state.operation_depth += 1


def exit_native_dispatch_operation_scope() -> None:
    state = _native_dispatch_state
    if state.operation_depth < 1:
        raise RuntimeError("네이티브 dispatch 작업 scope 깊이가 일치하지 않습니다")
    state.operation_depth -= 1
    if state.operation_depth == 0:
        state.operation_entries.clear()


@contextmanager
def native_dispatch_operation_scope() -> Generator[None, None, None]:
    enter_native_dispatch_operation_scope()
    try:
        yield
    finally:
        exit_native_dispatch_operation_scope()


class _EventHandle(Protocol):
    def Close(self) -> None: ...


@runtime_checkable
class _Win32Event(Protocol):
    WAIT_OBJECT_0: int
    WAIT_TIMEOUT: int

    def OpenEvent(
        self,
        desired_access: int,
        inherit_handle: bool,
        name: str,
    ) -> _EventHandle: ...

    def WaitForMultipleObjects(
        self,
        handles: tuple[_EventHandle, ...],
        wait_all: bool,
        milliseconds: int,
    ) -> int: ...


@runtime_checkable
class _CaptureEvent(Protocol):
    WAIT_OBJECT_0: int

    def CreateEvent(
        self,
        security_attributes: object | None,
        manual_reset: bool,
        initial_state: bool,
        name: str,
    ) -> _EventHandle: ...

    def SetEvent(self, handle: _EventHandle) -> None: ...

    def WaitForSingleObject(
        self,
        handle: _EventHandle,
        milliseconds: int,
    ) -> int: ...


@runtime_checkable
class _Win32Client(Protocol):
    def Dispatch(self, source: object) -> _BatchDispatch: ...


@runtime_checkable
class _Win32Process(Protocol):
    def GetWindowThreadProcessId(self, window_handle: int) -> tuple[int, int]: ...


def _modules() -> tuple[_PythonCom, _Win32Client, _Win32Process]:
    pythoncom = import_module("pythoncom")
    client = import_module("win32com.client")
    process = import_module("win32process")
    if not isinstance(pythoncom, _PythonCom):
        raise HwpLiveError("pythoncom 모듈 계약이 올바르지 않습니다")
    if not isinstance(client, _Win32Client):
        raise HwpLiveError("win32com.client 모듈 계약이 올바르지 않습니다")
    if not isinstance(process, _Win32Process):
        raise HwpLiveError("win32process 모듈 계약이 올바르지 않습니다")
    return pythoncom, client, process


def _com_context_initialized() -> bool:
    token = ctypes.c_size_t()
    status = cast(int, ctypes.windll.ole32.CoGetContextToken(ctypes.byref(token)))
    return status >= 0


@contextmanager
def _com_apartment() -> Generator[
    tuple[_PythonCom, _Win32Client, _Win32Process],
    None,
    None,
]:
    modules = _modules()
    pythoncom, _, _ = modules
    initialized_here = not _com_context_initialized()
    if initialized_here:
        pythoncom.CoInitialize()
    try:
        yield modules
    finally:
        if initialized_here:
            pythoncom.CoUninitialize()


def _source_for_window(
    window_handle: int,
    pythoncom: _PythonCom,
    process: _Win32Process,
) -> _DispatchSource | None:
    try:
        _, process_id = process.GetWindowThreadProcessId(window_handle)
    except OSError:
        _invalidate_native_dispatch(window_handle)
        return None
    if process_id <= 0:
        _invalidate_native_dispatch(window_handle)
        return None
    document_id, _ = _route_state(window_handle)
    return _source_for_route(
        window_handle,
        process_id,
        document_id,
        pythoncom,
    )


def _source_for_route(
    window_handle: int,
    process_id: int,
    document_id: int | None,
    pythoncom: _PythonCom,
) -> _DispatchSource | None:
    document_name = (
        None
        if document_id is None
        else f"!HancomLiveBatch.{process_id}.{window_handle}.{document_id}"
    )
    window_name = f"!HancomLiveBatch.{process_id}.{window_handle}"
    legacy_name = f"!HancomLiveBatch.{process_id}"
    context = pythoncom.CreateBindCtx(0)
    rot = pythoncom.GetRunningObjectTable()
    window_moniker: _Moniker | None = None
    legacy_moniker: _Moniker | None = None
    for moniker in rot.EnumRunning():
        display_name = moniker.GetDisplayName(context, moniker)
        if document_name is not None and display_name == document_name:
            return rot.GetObject(moniker)
        if display_name == window_name:
            window_moniker = moniker
        if display_name == legacy_name:
            legacy_moniker = moniker
    if window_moniker is not None:
        return rot.GetObject(window_moniker)
    if legacy_moniker is not None:
        return rot.GetObject(legacy_moniker)
    return None


@_invalidate_native_dispatch_on_exception
def native_batch_available(window_handle: int) -> bool:
    with _com_apartment() as (pythoncom, _, process):
        return _source_for_window(window_handle, pythoncom, process) is not None


@_invalidate_native_dispatch_on_exception
def activate_native_document(window_handle: int, document_id: int) -> bool:
    select_native_document_route(window_handle, document_id)
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 12, *modules)
        if batch is None:
            return False
        try:
            token = str(batch.ActivateDocument(document_id))
            if not token:
                return False
            event_module = import_module("win32event")
            if not isinstance(event_module, _Win32Event):
                raise HwpLiveError("Windows 이벤트 대기 런타임을 찾을 수 없습니다")
            success = event_module.OpenEvent(
                0x00100000,
                False,
                token + ".Success",
            )
            failure = event_module.OpenEvent(
                0x00100000,
                False,
                token + ".Failure",
            )
            try:
                waited = event_module.WaitForMultipleObjects(
                    (success, failure),
                    False,
                    45_000,
                )
                if waited == event_module.WAIT_OBJECT_0:
                    return True
                if waited == event_module.WAIT_OBJECT_0 + 1:
                    activation = int(batch.ActivationStatus(document_id))
                    raise HwpLiveError(
                        "한컴 네이티브 문서 탭 전환이 실패했습니다"
                        + f" (HRESULT 0x{activation & 0xFFFFFFFF:08X})"
                    )
                if waited == event_module.WAIT_TIMEOUT:
                    raise HwpLiveError(
                        "한컴 네이티브 문서 탭 전환 제한시간을 초과했습니다"
                        + "; worker_isolation_required=true"
                        + "; reconcile_required=false; retry_safe=true"
                    )
                raise HwpLiveError(
                    f"한컴 네이티브 문서 탭 전환 대기 결과가 올바르지 않습니다 ({waited})"
                )
            finally:
                failure.Close()
                success.Close()
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            com_error,
        ) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 문서 탭 전환에 실패했습니다",
                error,
                NativeActivationCallError,
            )
        finally:
            batch = None


@_invalidate_native_dispatch_on_exception
def _batch_for_window(
    window_handle: int,
    minimum_version: int,
    pythoncom: _PythonCom,
    client: _Win32Client,
    process: _Win32Process,
) -> _BatchDispatch | None:
    lookup_started = perf_counter_ns()
    try:
        _, process_id = process.GetWindowThreadProcessId(window_handle)
    except OSError:
        _invalidate_native_dispatch(window_handle)
        return None
    if process_id <= 0:
        _invalidate_native_dispatch(window_handle)
        return None
    document_id, route_generation = _route_state(window_handle)
    key = (process_id, window_handle, document_id, route_generation)
    now = monotonic()
    entries = _native_dispatch_state.entries
    entries = {
        cached_key: entry
        for cached_key, entry in entries.items()
        if entry.expires_at > now
        and not (cached_key[1] == window_handle and cached_key[3] != route_generation)
    }
    _native_dispatch_state.entries = entries
    entry = entries.get(key)
    operation_entries = _native_dispatch_state.operation_entries
    operation_entries = {
        cached_key: cached_entry
        for cached_key, cached_entry in operation_entries.items()
        if not (cached_key[1] == window_handle and cached_key[3] != route_generation)
    }
    _native_dispatch_state.operation_entries = operation_entries
    operation_entry = (
        operation_entries.get(key)
        if _native_dispatch_state.operation_depth > 0
        else None
    )
    cached_batch = None if operation_entry is None else operation_entry.batch
    if cached_batch is None:
        cached_batch = None if entry is None else entry.batch_ref()
    if entry is not None and cached_batch is None:
        _ = entries.pop(key, None)
        entry = None
    batch: _BatchDispatch
    if cached_batch is None:
        _record_native_dispatch_metrics(cache_hit=False, rot_scan=True)
        source = _source_for_route(
            window_handle,
            process_id,
            document_id,
            pythoncom,
        )
        if source is None:
            _record_native_dispatch_metrics(
                lookup_nanoseconds=perf_counter_ns() - lookup_started
            )
            return None
        try:
            batch = client.Dispatch(source.QueryInterface(pythoncom.IID_IDispatch))
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _invalidate_native_dispatch(window_handle)
            raise HwpLiveError("한컴 네이티브 실시간 연결에 실패했습니다") from error
        _record_native_dispatch_metrics(dispatch_creation=True)
    else:
        batch = cached_batch
        _record_native_dispatch_metrics(cache_hit=True)
    try:
        protocol_version = (
            operation_entry.protocol_version
            if operation_entry is not None
            else entry.protocol_version
            if entry is not None
            else int(batch.ProtocolVersion)
        )
        if protocol_version < minimum_version:
            _invalidate_native_dispatch(window_handle)
            raise HwpLiveError("한컴 네이티브 실시간 프로토콜 버전이 낮습니다")
        target_document_id = (
            operation_entry.target_document_id
            if operation_entry is not None
            else int(batch.TargetDocumentID)
        )
        if document_id is not None and target_document_id not in (0, document_id):
            _invalidate_native_dispatch(window_handle)
            raise HwpLiveError("한컴 네이티브 라우팅 문서 ID가 대상과 다릅니다")
    except HwpLiveError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
        _invalidate_native_dispatch(window_handle)
        raise HwpLiveError("한컴 네이티브 실시간 연결에 실패했습니다") from error
    if cached_batch is None:
        if len(entries) >= _NATIVE_DISPATCH_CACHE_MAX_ENTRIES:
            oldest_key = min(
                entries,
                key=lambda cached_key: entries[cached_key].expires_at,
            )
            _ = entries.pop(oldest_key, None)
        try:
            batch_ref = ref(batch)
        except TypeError:
            batch_ref = None
        if batch_ref is not None:
            entries[key] = _NativeDispatchCacheEntry(
                batch_ref=batch_ref,
                expires_at=now + _NATIVE_DISPATCH_CACHE_TTL_SECONDS,
                protocol_version=protocol_version,
            )
    if _native_dispatch_state.operation_depth > 0 and operation_entry is None:
        _native_dispatch_state.operation_entries[key] = _NativeDispatchOperationEntry(
            batch=batch,
            protocol_version=protocol_version,
            target_document_id=target_document_id,
        )
    _record_native_dispatch_metrics(
        lookup_nanoseconds=perf_counter_ns() - lookup_started
    )
    return batch


_GRAPH_ZERO_ID: Final = bytes(16)
_GRAPH_ZERO_DIGEST: Final = bytes(32)


def _graph_field(
    tag: int,
    scalar: int,
    value: bytes,
    *,
    flags: int = 1,
    count: int = 1,
) -> bytes:
    return struct.pack("<HHHHQQ", tag, flags, scalar, 0, count, len(value)) + value


def _graph_frame(
    message: int,
    payload: bytes,
    *,
    session: bytes,
    cursor: bytes = _GRAPH_ZERO_ID,
    sequence: int = 0,
    previous: bytes = _GRAPH_ZERO_DIGEST,
    version: NativeFrameVersion | None = None,
    profile_bits: int = 0,
) -> bytes:
    header = bytearray(320)
    header[:4] = b"HGN1"
    struct.pack_into(
        "<HHIIQQQQ", header, 4, 1, message, 0, 320, len(payload), sequence, 0, 0
    )
    header[48:64] = session
    flags = 0
    if version is not None:
        serialized = version.serialized
        header[64:80] = serialized[32:48]
        header[80:88] = serialized[2:10]
        header[88:112] = serialized[48:72]
        header[112:208] = serialized[72:168]
        flags = (32 if version.semantic_certified else 0) | (
            1 if version.layout_present else 0
        )
    else:
        header[80:88] = profile_bits.to_bytes(8, "little")
    struct.pack_into("<I", header, 8, flags)
    header[208:224] = cursor
    header[224:256] = previous
    chunk_header = bytes(header[:256]) + bytes(64)
    chunk = hashlib.sha256(b"HWPGRAPH\0CHUNK\0V1" + chunk_header + payload).digest()
    header[256:288] = chunk
    header[288:320] = hashlib.sha256(
        b"HWPGRAPH\0CHAIN\0V1" + previous + chunk + sequence.to_bytes(8, "little")
    ).digest()
    return bytes(header) + payload


def encode_graph_open_request(
    session: bytes,
    projection_bits: int,
    expected_version: NativeFrameVersion | None = None,
) -> bytes:
    profiles = 0
    if projection_bits & ((1 << 0) | (1 << 10) | (1 << 11)):
        profiles |= 1
    if projection_bits & ((1 << 1) | (1 << 2) | (1 << 3)):
        profiles |= 3
    if projection_bits & sum(1 << bit for bit in range(4, 8)):
        profiles |= 5
    if projection_bits & (1 << 8):
        profiles |= 17
    if projection_bits & (1 << 9):
        profiles |= 9
    if profiles == 0:
        profiles = 1
        projection_bits |= 1
    empty_predicates = struct.pack("<HHIQ", 11, 0, 0, 0)
    query = b"".join(
        (
            _graph_field(2, 14, b"\x03"),
            _graph_field(3, 1, (0).to_bytes(8, "little")),
            _graph_field(4, 1, (0).to_bytes(8, "little")),
            _graph_field(7, 11, empty_predicates, flags=3, count=0),
            _graph_field(8, 1, projection_bits.to_bytes(8, "little")),
            _graph_field(9, 14, b"\x00"),
        )
    )
    payload_fields = [_graph_field(1, 9, session)]
    if expected_version is not None:
        payload_fields.append(_graph_field(2, 11, expected_version.serialized, flags=0))
    payload_fields.extend(
        (
            _graph_field(3, 1, profiles.to_bytes(8, "little")),
            _graph_field(4, 11, query),
            _graph_field(5, 1, (4_194_304).to_bytes(8, "little")),
        )
    )
    return _graph_frame(
        101,
        b"".join(payload_fields),
        session=session,
        profile_bits=profiles,
        version=expected_version,
    )


def validate_graph_capabilities(raw: bytes) -> int:
    frame = validate_supported_frame(raw)
    if int(frame.message) != int(MessageKind.CAPABILITIES):
        raise HwpLiveError("한컴 네이티브 GraphCapabilities 응답이 올바르지 않습니다")
    payload = cast(NativeCapabilitiesPayload, frame.parsed_payload)
    offered = next(
        (
            field.value.value
            for field in payload.fields
            if field.tag == 6 and isinstance(field.value, NativeInteger)
        ),
        None,
    )
    if offered not in {0x11, 0x31, 0x39}:
        raise HwpLiveError("한컴 네이티브 GraphCapabilities 비트가 호환되지 않습니다")
    return offered


class _GraphRequestAdapter(Protocol):
    def request(self, member: GraphComMember, request: bytes) -> bytes: ...


@final
class NativeGraphResponseError(HwpLiveError):
    """Lossless typed native ERROR response without rendering its detail."""

    __slots__ = (
        "detail",
        "error_code",
        "hresult",
        "raw_frame",
        "raw_frame_bytes",
        "raw_frame_sha256",
        "required_bytes",
        "response_kind",
    )

    def __init__(
        self,
        *,
        response_kind: int,
        error_code: int,
        hresult: int,
        required_bytes: int,
        detail: str,
        raw_frame: bytes,
    ) -> None:
        super().__init__(
            f"native graph error response kind={response_kind} code={error_code}"
        )
        self.response_kind = response_kind
        self.error_code = error_code
        self.hresult = hresult
        self.required_bytes = required_bytes
        self.detail = detail
        self.raw_frame = raw_frame
        self.raw_frame_bytes = len(raw_frame)
        self.raw_frame_sha256 = hashlib.sha256(raw_frame).hexdigest()


@final
class NativeGraphUnexpectedResponseError(HwpLiveError):
    """Typed wrong-kind response retaining the observed wire kind and size."""

    __slots__ = ("expected_kind", "response_bytes", "response_kind")

    def __init__(
        self, *, expected_kind: int, response_kind: int, response_bytes: int
    ) -> None:
        reason = " ".join(
            (
                "native graph unexpected response",
                f"expected={expected_kind}",
                f"observed={response_kind}",
                f"bytes={response_bytes}",
            )
        )
        super().__init__(reason)
        self.expected_kind = expected_kind
        self.response_kind = response_kind
        self.response_bytes = response_bytes


def _require_graph_response_kind(
    response: SupportedNativeFrame, raw: bytes, expected: MessageKind
) -> None:
    observed = int(response.message)
    if observed == int(MessageKind.ERROR):
        payload = cast(NativeErrorPayload, response.parsed_payload)
        fields = {field.tag: field.value for field in payload.fields}
        code = fields[1]
        hresult = fields[2]
        required = fields[3]
        detail = fields[4]
        if not (
            isinstance(code, NativeInteger)
            and isinstance(hresult, NativeInteger)
            and isinstance(required, NativeInteger)
            and isinstance(detail, NativeRawUtf16)
        ):
            raise HwpLiveError("native graph ERROR payload shape is invalid")
        detail_raw = detail.raw
        if not isinstance(detail_raw, bytes):
            detail_raw = b"".join(detail_raw.chunks())
        raise NativeGraphResponseError(
            response_kind=observed,
            error_code=code.value,
            hresult=hresult.value & 0xFFFF_FFFF,
            required_bytes=required.value,
            detail=detail_raw.decode("utf-16-le", errors="surrogatepass"),
            raw_frame=raw,
        )
    if observed != int(expected):
        raise NativeGraphUnexpectedResponseError(
            expected_kind=int(expected),
            response_kind=observed,
            response_bytes=len(raw),
        )


@final
class NativeGraphTransfer:
    """One protocol-15 graph cursor retained through authenticated blob reads."""

    _adapter: _GraphRequestAdapter

    def __init__(
        self,
        window_handle: int,
        request: NativeGraphQuery,
        instrumentation: _GraphInstrumentation | None = None,
        expected_version: NativeFrameVersion | None = None,
    ) -> None:
        self._context = _com_apartment()
        modules = self._context.__enter__()
        self._closed = False
        self._cancelled = False
        self._frames_started = False
        self._terminal = False
        self._instrumentation = instrumentation
        try:
            batch = _batch_for_window(window_handle, 14, *modules)
            if batch is None:
                raise HwpLiveError("한컴 네이티브 그래프 연결을 찾을 수 없습니다")
            adapter = GraphComInvocationAdapter(batch)
            if adapter.protocol_version() != 15:
                raise HwpLiveError("한컴 네이티브 그래프 프로토콜 버전이 다릅니다")
            session_uuid = (
                uuid.UUID(request.session_id)
                if request.session_id
                else uuid.UUID(bytes=expected_version.session)
                if expected_version is not None
                else uuid.uuid4()
            )
            self._session = session_uuid.bytes
            self._adapter = adapter
            if self._instrumentation is not None:
                self._instrumentation.count_call("graph_capabilities")
            offered = validate_graph_capabilities(adapter.capabilities())
            self._blob_pack_available = bool(offered & _CAPABILITY_ASSET_READ)
            if expected_version is None and not _remembered_capability_session(
                window_handle, str(session_uuid)
            ):
                if self._instrumentation is not None:
                    self._instrumentation.count_call("graph_capabilities")
                requested = (
                    _BLOB_READ_CAPABILITIES
                    if self._blob_pack_available
                    else _CAPABILITY_GRAPH_READ
                )
                try:
                    negotiated = validate_graph_capabilities(
                        adapter.negotiate(str(session_uuid), requested)
                    )
                except com_error as error:
                    # The addon still holds this capability session from an
                    # earlier cold open on this route. Reuse it; GraphClose is
                    # unsupported so no close can precede this open.
                    if (
                        not error.args
                        or not isinstance(error.args[0], int)
                        or error.args[0] != _ERROR_ALREADY_EXISTS_HRESULT
                    ):
                        raise
                    negotiated = requested
                _remember_capability_session(
                    window_handle, str(session_uuid), negotiated
                )
            if self._instrumentation is not None:
                self._instrumentation.count_call("graph_open")
            open_request = encode_graph_open_request(
                self._session, request.projection_bits, expected_version
            )
            _record_client_progress("GraphOpenInvokeStart", len(open_request), 0)
            open_raw = adapter.request(GraphComMember.GRAPH_OPEN, open_request)
            _record_client_progress("GraphOpenInvokeEnd", len(open_raw), 0)
            _record_client_progress("OpenReceiptDecodeStart", len(open_raw), 0)
            opened = validate_supported_frame(open_raw)
            _record_client_progress(
                "OpenReceiptDecodeEnd",
                int(opened.message),
                len(opened.parsed_payload.fields),
            )
            if int(opened.message) == int(MessageKind.ERROR):
                error_code = next(
                    (
                        field.value.value
                        for field in opened.parsed_payload.fields
                        if field.tag == 1 and isinstance(field.value, NativeInteger)
                    ),
                    None,
                )
                if error_code == 16:
                    from hwp_native_graph_performance import CaptureCancelled

                    raise CaptureCancelled("native GraphOpen capture checkpoint")
                detail = f"({error_code}: {opened.parsed_payload.fields!r})"
                raise HwpLiveError(
                    f"한컴 네이티브 GraphOpen 오류 코드가 반환되었습니다 {detail}"
                )
            if int(opened.message) != int(MessageKind.OPEN_RECEIPT):
                raise HwpLiveError("한컴 네이티브 GraphOpen 응답이 올바르지 않습니다")
            self._open_raw = open_raw
            self._opened = opened
            self._version = opened.version
            self._cursor = opened.cursor
            self._previous = opened.chain_digest
        except BaseException:
            self._closed = True
            _ = self._context.__exit__(None, None, None)
            raise

    def frames(self) -> Iterable[bytes]:
        if self._closed or self._frames_started:
            raise HwpLiveError("한컴 네이티브 그래프 프레임 수명이 올바르지 않습니다")
        self._frames_started = True
        if self._instrumentation is not None:
            _ = self._instrumentation.observe_decoded_frame(
                self._opened, len(self._open_raw)
            )
        yield self._open_raw
        sequence = 1
        transferred_bytes = len(self._open_raw)
        while True:
            next_payload = b"".join(
                (
                    _graph_field(1, 9, self._cursor),
                    _graph_field(2, 11, self._version.serialized),
                    _graph_field(3, 1, (4_194_304).to_bytes(8, "little")),
                    _graph_field(4, 1, sequence.to_bytes(8, "little")),
                    _graph_field(5, 10, self._previous),
                )
            )
            next_request = _graph_frame(
                102,
                next_payload,
                session=self._session,
                cursor=self._cursor,
                sequence=sequence,
                previous=self._previous,
                version=self._version,
            )
            _record_client_progress("GraphNextInvokeStart", sequence, len(next_request))
            next_raw = self._adapter.request(GraphComMember.GRAPH_NEXT, next_request)
            _record_client_progress("GraphNextInvokeEnd", sequence, len(next_raw))
            _record_client_progress("FrameDecodeStart", sequence, len(next_raw))
            # Authenticate raw transport once. Graph chunks retain compact
            # offsets only; the cache decoder validates every fragment/value
            # from these exact bytes before publication.
            authenticated = decode_frame(next_raw, validate_graph_payload=False)
            transferred_bytes += len(next_raw)
            _record_client_progress(
                "FrameDecodeEnd", sequence, int(authenticated.message)
            )
            _record_client_progress("FragmentLoop", sequence, transferred_bytes)
            if authenticated.message is MessageKind.GRAPH_CHUNK:
                fragment_count = count_graph_fragments(authenticated.payload)
                if (
                    self._instrumentation is not None
                    and self._instrumentation.observe_authenticated_graph_chunk(
                        len(next_raw), fragment_count
                    )
                ):
                    self.cancel()
                    from hwp_native_graph_performance import CaptureCancelled

                    raise CaptureCancelled("deterministic graph chunk checkpoint")
                yield next_raw
                self._previous = authenticated.chain_digest
                sequence += 1
                continue
            response = validate_supported_frame(next_raw)
            if self._instrumentation is not None:
                _ = self._instrumentation.observe_decoded_frame(response, len(next_raw))
            yield next_raw
            if int(response.message) == int(MessageKind.GRAPH_TERMINAL):
                self._terminal = True
                return
            raise HwpLiveError("한컴 네이티브 GraphNext 응답이 올바르지 않습니다")

    def consume_authenticated_stream(
        self,
        on_frame: Callable[[bytes], None],
        on_record: Callable[[TypedNativeRecord], None],
        on_record_range: Callable[[RecordKind, int, int, int], None],
        on_logical_bytes: Callable[[bytes], None] | None = None,
    ) -> tuple[NativeFrameVersion, int, int, int, int, bytes]:
        """Consume one live cursor with one frame authentication/descriptor walk."""
        if self._closed or self._frames_started:
            raise HwpLiveError("한컴 네이티브 그래프 프레임 수명이 올바르지 않습니다")
        self._frames_started = True

        def observe_fragments(frame_bytes: int, fragments: int) -> None:
            if (
                self._instrumentation is not None
                and self._instrumentation.observe_authenticated_graph_chunk(
                    frame_bytes, fragments
                )
            ):
                self.cancel()
                from hwp_native_graph_performance import CaptureCancelled

                raise CaptureCancelled("deterministic graph chunk checkpoint")

        terminal_frame_bytes = 0
        wire_version = WireGraphVersion(
            session=self._version.session,
            graph=self._version.graph,
            profile_bits=self._version.profile_bits,
            semantic_revision=self._version.semantic_revision,
            layout_revision=self._version.layout_revision,
            locator_epoch=self._version.locator_epoch,
            semantic_root=self._version.semantic_root,
            layout_root=self._version.layout_root,
            capture_root=self._version.capture_root,
            semantic_certified=self._version.semantic_certified,
            layout_present=self._version.layout_present,
        )

        def authenticated_frames() -> Iterable[AuthenticatedGraphFrame]:
            nonlocal terminal_frame_bytes
            if self._instrumentation is not None:
                _ = self._instrumentation.observe_decoded_frame(
                    self._opened, len(self._open_raw)
                )
            on_frame(self._open_raw)
            yield AuthenticatedGraphFrame(self._open_raw, self._opened, wire_version)
            sequence = 1
            while True:
                next_payload = b"".join(
                    (
                        _graph_field(1, 9, self._cursor),
                        _graph_field(2, 11, self._version.serialized),
                        _graph_field(3, 1, (4_194_304).to_bytes(8, "little")),
                        _graph_field(4, 1, sequence.to_bytes(8, "little")),
                        _graph_field(5, 10, self._previous),
                    )
                )
                next_request = _graph_frame(
                    102,
                    next_payload,
                    session=self._session,
                    cursor=self._cursor,
                    sequence=sequence,
                    previous=self._previous,
                    version=self._version,
                )
                _record_client_progress(
                    "GraphNextInvokeStart", sequence, len(next_request)
                )
                raw = self._adapter.request(GraphComMember.GRAPH_NEXT, next_request)
                _record_client_progress("GraphNextInvokeEnd", sequence, len(raw))
                frame = decode_frame(raw, validate_graph_payload=False)
                on_frame(raw)
                if frame.message is MessageKind.GRAPH_CHUNK:
                    yield AuthenticatedGraphFrame(raw, frame, frame.version)
                    self._previous = frame.chain_digest
                    sequence += 1
                    continue
                yield AuthenticatedGraphFrame(raw, frame, frame.version)
                if frame.message is MessageKind.GRAPH_TERMINAL:
                    terminal_frame_bytes = len(raw)
                    self._terminal = True
                    return
                raise HwpLiveError("한컴 네이티브 GraphNext 응답이 올바르지 않습니다")

        decoder = GraphStreamDecoder(
            authenticated_frames(),
            fragment_observer=observe_fragments,
            record_observer=on_record_range,
            logical_record_observer=on_logical_bytes,
        )
        try:
            with decoder.records() as records:
                for record in records:
                    on_record(record)
            if decoder.version is None:
                raise HwpLiveError("한컴 네이티브 그래프 스트림이 비었습니다")
            if self._instrumentation is not None:
                self._instrumentation.observe_authenticated_graph_terminal(
                    terminal_frame_bytes,
                    decoder.record_count,
                    decoder.logical_bytes,
                    decoder.stream_digest,
                )
            return (
                self._version,
                decoder.record_count,
                decoder.logical_bytes,
                decoder.fragment_count,
                decoder.field_count,
                decoder.stream_digest,
            )
        finally:
            decoder.close()

    def consume_primitive_stream(
        self,
        on_frame: Callable[[bytes], None],
        on_record: Callable[[PrimitiveRecordFacts], None],
        on_logical_bytes: Callable[[bytes], None],
    ) -> tuple[WireGraphVersion, int, int, int, int, bytes]:
        """Publish a live cursor without constructing public record models."""
        if self._closed or self._frames_started:
            raise HwpLiveError("한컴 네이티브 그래프 프레임 수명이 올바르지 않습니다")
        self._frames_started = True
        terminal_frame_bytes = 0
        wire_version = WireGraphVersion(
            session=self._version.session,
            graph=self._version.graph,
            profile_bits=self._version.profile_bits,
            semantic_revision=self._version.semantic_revision,
            layout_revision=self._version.layout_revision,
            locator_epoch=self._version.locator_epoch,
            semantic_root=self._version.semantic_root,
            layout_root=self._version.layout_root,
            capture_root=self._version.capture_root,
            semantic_certified=self._version.semantic_certified,
            layout_present=self._version.layout_present,
        )

        def authenticated_frames() -> Iterable[AuthenticatedGraphFrame]:
            nonlocal terminal_frame_bytes
            if self._instrumentation is not None:
                _ = self._instrumentation.observe_decoded_frame(
                    self._opened, len(self._open_raw)
                )
            on_frame(self._open_raw)
            yield AuthenticatedGraphFrame(self._open_raw, self._opened, wire_version)
            sequence = 1
            while True:
                next_payload = b"".join(
                    (
                        _graph_field(1, 9, self._cursor),
                        _graph_field(2, 11, self._version.serialized),
                        _graph_field(3, 1, (4_194_304).to_bytes(8, "little")),
                        _graph_field(4, 1, sequence.to_bytes(8, "little")),
                        _graph_field(5, 10, self._previous),
                    )
                )
                request = _graph_frame(
                    102,
                    next_payload,
                    session=self._session,
                    cursor=self._cursor,
                    sequence=sequence,
                    previous=self._previous,
                    version=self._version,
                )
                raw = self._adapter.request(GraphComMember.GRAPH_NEXT, request)
                frame = decode_frame(raw, validate_graph_payload=False)
                on_frame(raw)
                yield AuthenticatedGraphFrame(raw, frame, frame.version)
                if frame.message is MessageKind.GRAPH_CHUNK:
                    self._previous = frame.chain_digest
                    sequence += 1
                    continue
                if frame.message is MessageKind.GRAPH_TERMINAL:
                    terminal_frame_bytes = len(raw)
                    self._terminal = True
                    return
                raise HwpLiveError("한컴 네이티브 GraphNext 응답이 올바르지 않습니다")

        def observe_fragments(frame_bytes: int, fragments: int) -> None:
            if (
                self._instrumentation is not None
                and self._instrumentation.observe_authenticated_graph_chunk(
                    frame_bytes, fragments
                )
            ):
                self.cancel()
                from hwp_native_graph_performance import CaptureCancelled

                raise CaptureCancelled("deterministic graph chunk checkpoint")

        seal = consume_primitive_graph_stream(
            authenticated_frames(),
            on_logical_bytes=on_logical_bytes,
            on_record=on_record,
            on_fragments=observe_fragments,
        )
        if self._instrumentation is not None:
            self._instrumentation.observe_authenticated_graph_terminal(
                terminal_frame_bytes,
                seal.record_count,
                seal.logical_bytes,
                seal.stream_digest,
            )
        return (
            seal.version,
            seal.record_count,
            seal.logical_bytes,
            seal.fragment_count,
            seal.field_count,
            seal.stream_digest,
        )

    def read_blobs(
        self, blobs: tuple[NativeBlobSlice, ...]
    ) -> dict[NativeBlobSlice, bytes]:
        if self._closed or not self._terminal:
            raise HwpLiveError("그래프 터미널 전에 blob pack을 읽을 수 없습니다")
        if not blobs:
            return {}
        if not self._blob_pack_available:
            return {blob: self.read_blob(blob) for blob in blobs}
        ordered = tuple(
            sorted(
                blobs,
                key=lambda item: (
                    item.content_id,
                    item.offset,
                    item.length,
                    item.digest,
                ),
            )
        )
        if len(set(ordered)) != len(ordered):
            raise HwpLiveError("BlobPack tuple이 중복되었습니다")
        encoded = len(ordered).to_bytes(8, "little") + b"".join(
            blob.content_id
            + blob.offset.to_bytes(8, "little")
            + blob.length.to_bytes(8, "little")
            + blob.digest
            for blob in ordered
        )
        transfer_id = uuid.uuid4().bytes
        payload = b"".join(
            (
                _graph_field(1, 9, self._cursor),
                _graph_field(2, 11, self._version.serialized),
                _graph_field(3, 9, transfer_id),
                _graph_field(4, 13, len(encoded).to_bytes(8, "little") + encoded),
                _graph_field(5, 1, (4_193_984).to_bytes(8, "little")),
            )
        )
        request = _graph_frame(
            112,
            payload,
            session=self._session,
            cursor=self._cursor,
            version=self._version,
        )
        if self._instrumentation is not None:
            self._instrumentation.count_call("blob_read")
        raw = self._adapter.request(GraphComMember.BLOB_READ, request)
        response = validate_supported_frame(raw)
        try:
            _require_graph_response_kind(response, raw, MessageKind.BLOB_PACK)
        except NativeGraphResponseError as error:
            if (
                error.error_code == 0
                and error.hresult == 0x80070057
                and error.required_bytes == 0
                and error.detail == "BlobPack response budget exceeded"
            ):
                return {blob: self.read_blob(blob) for blob in ordered}
            raise
        parsed = cast(NativeBlobPackPayload, response.parsed_payload)
        fields = {field.tag: field.value for field in parsed.fields}
        identity = fields.get(1)
        count = fields.get(2)
        content = fields.get(3)
        digest = fields.get(4)
        if not (
            isinstance(identity, NativeIdentifier)
            and identity.value == transfer_id
            and isinstance(count, NativeInteger)
            and count.value == len(ordered)
            and isinstance(content, NativeOpaque)
            and isinstance(content.raw, bytes)
            and len(content.raw) >= 8
            and isinstance(digest, NativeDigest)
        ):
            raise HwpLiveError("한컴 네이티브 BlobPack echo가 다릅니다")
        packed_length = int.from_bytes(content.raw[:8], "little")
        packed = content.raw[8:]
        if (
            packed_length != len(packed)
            or hashlib.sha256(packed).digest() != digest.value
        ):
            raise HwpLiveError("한컴 네이티브 BlobPack 지문이 다릅니다")
        if len(packed) < 8 or int.from_bytes(packed[:8], "little") != len(ordered):
            raise HwpLiveError("한컴 네이티브 BlobPack 개수가 다릅니다")
        at = 8
        result: dict[NativeBlobSlice, bytes] = {}
        for expected in ordered:
            if at + 72 > len(packed):
                raise HwpLiveError("한컴 네이티브 BlobPack이 잘렸습니다")
            observed_content_id = packed[at : at + 16]
            observed_offset = int.from_bytes(packed[at + 16 : at + 24], "little")
            observed_length = int.from_bytes(packed[at + 24 : at + 32], "little")
            observed_digest = packed[at + 32 : at + 64]
            value_length = int.from_bytes(packed[at + 64 : at + 72], "little")
            at += 72
            value = packed[at : at + value_length]
            at += value_length
            if (
                observed_content_id != expected.content_id
                or observed_offset != expected.offset
                or observed_length != expected.length
                or observed_digest != expected.digest
                or value_length != expected.length
                or len(value) != value_length
                or hashlib.sha256(value).digest() != expected.digest
            ):
                raise HwpLiveError("한컴 네이티브 BlobPack tuple이 다릅니다")
            result[expected] = value
            if self._instrumentation is not None:
                self._instrumentation.observe_blob(expected, value)
        if at != len(packed):
            raise HwpLiveError("한컴 네이티브 BlobPack 후행 데이터가 있습니다")
        return result

    def read_blob(self, blob: NativeBlobSlice) -> bytes:
        if self._closed or not self._terminal:
            raise HwpLiveError("그래프 터미널 전에 blob을 읽을 수 없습니다")
        result = bytearray()
        while len(result) < blob.length or (blob.length == 0 and not result):
            relative = len(result)
            payload = b"".join(
                (
                    _graph_field(1, 9, self._cursor),
                    _graph_field(2, 11, self._version.serialized),
                    _graph_field(3, 9, blob.content_id),
                    _graph_field(4, 1, blob.offset.to_bytes(8, "little")),
                    _graph_field(5, 1, blob.length.to_bytes(8, "little")),
                    _graph_field(6, 10, blob.digest),
                    _graph_field(7, 1, relative.to_bytes(8, "little")),
                    _graph_field(8, 1, (32768).to_bytes(8, "little")),
                )
            )
            if self._instrumentation is not None:
                self._instrumentation.count_call("blob_read")
            blob_request = _graph_frame(
                111,
                payload,
                session=self._session,
                cursor=self._cursor,
                version=self._version,
            )
            raw = self._adapter.request(GraphComMember.BLOB_READ, blob_request)
            response = validate_supported_frame(raw)
            _require_graph_response_kind(response, raw, MessageKind.BLOB_CHUNK)
            parsed = cast(NativeBlobChunkPayload, response.parsed_payload)
            fields = {field.tag: field.value for field in parsed.fields}
            identity = fields.get(1)
            source = fields.get(2)
            length = fields.get(3)
            digest = fields.get(4)
            echoed_relative = fields.get(5)
            content = fields.get(6)
            if not (
                isinstance(identity, NativeIdentifier)
                and identity.value == blob.content_id
                and isinstance(source, NativeInteger)
                and source.value == blob.offset
                and isinstance(length, NativeInteger)
                and length.value == blob.length
                and isinstance(digest, NativeDigest)
                and digest.value == blob.digest
                and isinstance(echoed_relative, NativeInteger)
                and echoed_relative.value == relative
                and isinstance(content, NativeOpaque)
                and isinstance(content.raw, bytes)
                and len(content.raw) >= 8
            ):
                raise HwpLiveError("한컴 네이티브 BlobChunk echo가 다릅니다")
            count = int.from_bytes(content.raw[:8], "little")
            chunk = content.raw[8:]
            if count != len(chunk) or len(chunk) > 32768:
                raise HwpLiveError("한컴 네이티브 BlobChunk 길이가 다릅니다")
            result.extend(chunk)
            if blob.length == 0 or len(result) == blob.length:
                break
            if not chunk:
                raise HwpLiveError("한컴 네이티브 BlobChunk가 진행하지 않습니다")
        value = bytes(result)
        if len(value) != blob.length or hashlib.sha256(value).digest() != blob.digest:
            raise HwpLiveError("한컴 네이티브 blob 최종 지문이 다릅니다")
        if self._instrumentation is not None:
            self._instrumentation.observe_blob(blob, value)
        return value

    def _dispose(self, *, cancel: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancelled = cancel
        try:
            close_payload = _graph_field(1, 9, self._cursor) + _graph_field(
                2, 9, self._session
            )
            close_request = _graph_frame(
                103 if cancel else 104,
                close_payload,
                session=self._session,
                cursor=self._cursor,
            )
            _ = self._adapter.request(
                GraphComMember.GRAPH_CANCEL if cancel else GraphComMember.GRAPH_CLOSE,
                close_request,
            )
            if self._instrumentation is not None:
                self._instrumentation.cursor_closed(cancelled=cancel)
        finally:
            _ = self._context.__exit__(None, None, None)

    def open_warm(
        self,
        window_handle: int,
        request: NativeGraphQuery,
        instrumentation: _GraphInstrumentation | None = None,
    ) -> "NativeGraphTransfer":
        if self._closed:
            raise HwpLiveError("warm query requires an open pinned transfer")
        bound = request.model_copy(
            update={"session_id": str(uuid.UUID(bytes=self._session))}
        )
        return NativeGraphTransfer(
            window_handle,
            bound,
            instrumentation,
            expected_version=self._version,
        )

    def cancel(self) -> None:
        self._dispose(cancel=True)

    def close(self) -> None:
        self._dispose(cancel=False)


_pinned_graph_transfers: dict[int, tuple[NativeGraphTransfer, str]] = {}
_pinned_graph_window_locks: dict[int, Lock] = {}
_pinned_graph_registry_lock = Lock()


def _pinned_graph_window_lock(window_handle: int) -> Lock:
    with _pinned_graph_registry_lock:
        return _pinned_graph_window_locks.setdefault(window_handle, Lock())


def _release_pinned_graph_transfer(window_handle: int) -> None:
    lock = _pinned_graph_window_lock(window_handle)
    with lock:
        with _pinned_graph_registry_lock:
            transfer = _pinned_graph_transfers.pop(window_handle, None)
        if transfer is not None:
            transfer[0].close()


@final
class _PinnedNativeGraphTransfer:
    """A cache-owned lease that leaves one complete native cursor pinned."""

    def __init__(
        self,
        window_handle: int,
        transfer: NativeGraphTransfer,
        predecessor: NativeGraphTransfer | None,
        store_epoch: str,
        lock: Lock,
    ) -> None:
        self._window_handle = window_handle
        self._transfer = transfer
        self._predecessor = predecessor
        self._store_epoch = store_epoch
        self._lock = lock
        self._closed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._transfer, name)

    def frames(self) -> Iterable[bytes]:
        return self._transfer.frames()

    def read_blob(self, blob: NativeBlobSlice) -> bytes:
        return self._transfer.read_blob(blob)

    def read_blobs(
        self, blobs: tuple[NativeBlobSlice, ...]
    ) -> dict[NativeBlobSlice, bytes]:
        return self._transfer.read_blobs(blobs)

    def cancel(self) -> None:
        self._transfer.cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._transfer._terminal and not self._transfer._closed:
                with _pinned_graph_registry_lock:
                    _pinned_graph_transfers[self._window_handle] = (
                        self._transfer,
                        self._store_epoch,
                    )
                if self._predecessor is not None:
                    self._predecessor.close()
            else:
                self._transfer.close()
        finally:
            self._lock.release()


def query_native_graph(
    window_handle: int,
    request: NativeGraphQuery,
    instrumentation: _GraphInstrumentation | None = None,
) -> NativeGraphTransfer:
    return NativeGraphTransfer(window_handle, request, instrumentation)


def query_native_graph_warm(
    window_handle: int,
    request: NativeGraphQuery,
    pinned: NativeGraphTransfer,
    instrumentation: _GraphInstrumentation | None = None,
) -> NativeGraphTransfer:
    return pinned.open_warm(window_handle, request, instrumentation)


def query_native_graph_pinned(
    window_handle: int,
    request: NativeGraphQuery,
    instrumentation: _GraphInstrumentation | None = None,
) -> _PinnedNativeGraphTransfer:
    """Open cold once, then retain one cursor for version-checked warm opens."""
    lock = _pinned_graph_window_lock(window_handle)
    lock.acquire()
    try:
        with _pinned_graph_registry_lock:
            pinned_entry = _pinned_graph_transfers.get(window_handle)
        pinned = None if pinned_entry is None else pinned_entry[0]
        transfer = (
            query_native_graph_warm(window_handle, request, pinned, instrumentation)
            if pinned_entry is not None and pinned_entry[1] == request.store_epoch
            else query_native_graph(window_handle, request, instrumentation)
        )
        return _PinnedNativeGraphTransfer(
            window_handle,
            transfer,
            pinned,
            request.store_epoch,
            lock,
        )
    except BaseException:
        lock.release()
        raise


def cancel_native_graph_capture_at_checkpoint(
    window_handle: int,
    request: NativeGraphQuery,
    instrumentation: _GraphInstrumentation | None = None,
    *,
    timeout_ms: int = 30_000,
) -> dict[str, int]:
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    event_module = import_module("win32event")
    if not isinstance(event_module, _CaptureEvent):
        raise HwpLiveError("Windows capture 이벤트 런타임을 찾을 수 없습니다")
    # Cancel events are named Local\\HancomGraphCapture.<session>.Cancel and
    # stay signaled until every handle is closed. Reusing the content-derived
    # capture session here left that Cancel event open, so the subsequent
    # large GraphOpen opened only one of the two named events and returned
    # IncompleteCapture/BeginSession (sealed StorageFailure code 17).
    session_uuid = uuid.uuid4()
    bound = request.model_copy(update={"session_id": session_uuid.hex})
    base = f"Local\\HancomGraphCapture.{session_uuid}."
    checkpoint = event_module.CreateEvent(None, True, False, base + "Checkpoint")
    cancel = event_module.CreateEvent(None, True, False, base + "Cancel")
    done = event_module.CreateEvent(None, True, False, base + "Done")
    outcome: list[BaseException | None] = []

    def invoke() -> None:
        try:
            transfer = query_native_graph(window_handle, bound, instrumentation)
        except BaseException as error:
            outcome.append(error)
        else:
            transfer.close()
            outcome.append(HwpLiveError("GraphOpen cancellation returned a cursor"))
        finally:
            event_module.SetEvent(done)

    worker = Thread(target=invoke, name="hwp-graph-open-cancel-sta")
    started_ns = perf_counter_ns()
    worker.start()
    checkpoint_ns = 0
    cancelled_ns = 0
    try:
        if (
            event_module.WaitForSingleObject(checkpoint, timeout_ms)
            != event_module.WAIT_OBJECT_0
        ):
            event_module.SetEvent(cancel)
            raise HwpLiveError("GraphOpen cancellation checkpoint timed out")
        checkpoint_ns = perf_counter_ns()
        event_module.SetEvent(cancel)
        if (
            event_module.WaitForSingleObject(done, timeout_ms)
            != event_module.WAIT_OBJECT_0
        ):
            raise HwpLiveError("GraphOpen cancellation completion timed out")
        cancelled_ns = perf_counter_ns()
    finally:
        event_module.SetEvent(cancel)
        worker.join()
        done.Close()
        cancel.Close()
        checkpoint.Close()
    from hwp_native_graph_performance import CaptureCancelled

    if len(outcome) != 1 or not isinstance(outcome[0], CaptureCancelled):
        error = outcome[0] if outcome else RuntimeError("missing worker outcome")
        raise HwpLiveError("GraphOpen capture cancellation was not observed") from error
    return {
        "checkpoint_wait_ns": checkpoint_ns - started_ns,
        "cancel_to_completion_ns": cancelled_ns - checkpoint_ns,
        "total_ns": cancelled_ns - started_ns,
    }


def native_graph_protocol_version(window_handle: int) -> int | None:
    """Return the additive graph ABI without touching the protocol-14 surface."""
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 14, *modules)
        if batch is None:
            return None
        try:
            return int(batch.GraphProtocolVersion)
        except AttributeError:
            return None
        finally:
            batch = None


@_invalidate_native_dispatch_on_exception
def execute_native_batch(
    window_handle: int,
    request: NativeBatchRequest,
) -> NativeBatchResult | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 1, *modules)
        if batch is None:
            return None

        try:
            forget_cached_content_signatures(window_handle)
            response = batch.Execute(encode_batch_request(request))
        except HwpLiveError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 배치 실행에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_batch_result(str(response))


@_invalidate_native_dispatch_on_exception
def execute_native_protocol_bundle(
    window_handle: int,
    request: NativeBundleRequest,
) -> NativeBundleReceipt | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 14, *modules)
        if batch is None:
            return None

        try:
            forget_cached_content_signatures(window_handle)
            response = batch.ExecuteProtocolBundle(encode_bundle_request(request))
        except HwpLiveError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 통합 프로토콜 실행에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_bundle_receipt(str(response))


@_invalidate_native_dispatch_on_exception
def execute_native_lifecycle(window_handle: int) -> NativeLifecycleResult | None:
    with _com_apartment() as modules:
        # Protocol 12, not 8. SaveReopenVerify itself is older, but protocol 12
        # is what "separates non-destructive SaveVerify from the explicit
        # SaveReopenVerify diagnostic and adds full text/document fingerprints
        # plus failed-reopen session recovery"
        # (addon/HancomLiveBridgeNative/README.md). DocumentLifecycle.cpp:429 is
        # the only emitter and it writes the HCL12 tag, whose recovery and
        # fingerprint fields decode_lifecycle_result requires
        # (hwp_live_native_batch_contract.py:339). A gate of 8 admitted bridges
        # 8-11, which answer in the older shape, so the call passed the gate and
        # then failed in the decoder with a response-format error.
        batch = _batch_for_window(window_handle, 12, *modules)
        if batch is None:
            return None
        try:
            forget_cached_content_signatures(window_handle)
            response = batch.SaveReopenVerify()
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            com_error,
        ) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 저장·재개방 검증 실행에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_lifecycle_result(str(response))


@_invalidate_native_dispatch_on_exception
def execute_native_save(window_handle: int) -> NativeSaveResult | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 12, *modules)
        if batch is None:
            return None
        try:
            forget_cached_content_signatures(window_handle)
            response = batch.SaveVerify()
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            com_error,
        ) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 일반 저장 검증 실행에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_save_result(str(response))


@_invalidate_native_dispatch_on_exception
def read_native_snapshot(window_handle: int) -> NativeSnapshot | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 2, *modules)
        if batch is None:
            return None
        try:
            response = batch.Snapshot()
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 현재 상태 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_snapshot(str(response))


@_invalidate_native_dispatch_on_exception
def read_native_content_signature(window_handle: int) -> str | None:
    """Whole-document text/control signature from the in-process bridge.

    The additive member is absent on older bridges. ``None`` is unknown and
    callers compare it as a mismatch; this read never blocks an edit itself.
    Consecutive reads reuse the last complete signature until this worker
    mutates the document, reconnects, or drops the native dispatch.

    The bridge's incomplete answer is remembered under the same lifetime.
    The engine refuses to serialise a document past its own memory ceiling
    (S_OK with an empty string -- measured at 17.2s for a 317MB/38-page
    document, against 6.7s for a complete answer on its 82MB/34-page save),
    so the refusal costs the full serialisation attempt and repeats
    identically until the document changes. Every invalidation that would
    drop a complete signature -- a native write, the engine change event,
    reconnect -- also drops the refusal, so a document that shrinks is
    re-tried the next time someone asks.

    That same-lifetime refusal only makes a *repeated read* free, and the
    callers who matter here read once per write. So a second memory outlives
    the writes: ``_engine_still_refuses`` re-checks a remembered refusal
    against the cheap revision token instead of buying the verdict again. It
    answers no whenever the document may have got smaller, which is the only
    way a refusal can stop being true.
    """
    cached = _cached_content_signature(window_handle)
    if cached is not None:
        return cached or None
    if _engine_still_refuses(window_handle):
        # Remembered under the ordinary lifetime too, so the rest of this
        # document state is served from the cheap cache rather than re-checked.
        _remember_content_signature(window_handle, "")
        return None
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 13, *modules)
        if batch is None:
            return None
        try:
            response = batch.ContentSignature()
        except AttributeError:
            _invalidate_native_dispatch(window_handle)
            return None
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 문서 본문 지문 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
        signature = str(response)
    if not signature.startswith("SIG "):
        # How big the document was when the engine gave up, so the next write
        # does not make somebody buy this same verdict again.
        #
        # This runs BEFORE the refusal is cached, and the order is load-bearing.
        # A bridge too old to have ``ContentRevision`` answers by invalidating
        # the native dispatch, and that invalidation drops this window's
        # signature cache. Caching the refusal first would hand it straight to
        # that drop, and every refusal would cost two full serialisation
        # attempts instead of one.
        revision = read_native_content_revision(window_handle)
        # The bridge answered and the answer is "cannot capture". That is a
        # property of the document as it stands, not a transient failure
        # (those raise above), so it is cached like a signature would be.
        _remember_content_signature(window_handle, "")
        _remember_signature_refusal(window_handle, revision)
        return None
    _remember_content_signature(window_handle, signature)
    # No refusal cleanup here: this branch is unreachable while a same-size
    # refusal is remembered (_engine_still_refuses returns early above), and a
    # shrink already dropped the memory inside _engine_still_refuses itself.
    return signature


@_invalidate_native_dispatch_on_exception
def read_native_content_revision(window_handle: int) -> str | None:
    """Cheap freshness token from the in-process bridge, not a signature.

    It dates the document; it does not describe it. Use it to decide whether
    something derived from the document may be reused, never to prove that two
    documents are the same -- ``read_native_content_signature`` is still the
    only answer for undo/redo preconditions, checkpoint restore, and rollback
    evidence. What it is blind to, and why, is set out on
    ``bind_graph_query_content_freshness``.

    The cache is per worker process and the token's write epoch is per Hangul
    process, so a sibling worker writing through the bridge does change the
    token this worker reads next; a sibling editing outside the bridge, or a
    person typing, reaches this worker only through the engine change event.

    An empty document never yields a token -- a zero text length is reported as
    incomplete because the bridge cannot separate it from a failed text read --
    so such a document stays permanently on the caller's cold path by design.

    The additive member is absent on bridges before 0.5.172, which answer
    ``None`` here; callers treat that as unknown and recompute rather than
    falling back to the signature, whose cost is the reason this exists.
    """
    cached = _cached_content_revision(window_handle)
    if cached is not None:
        return cached
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 13, *modules)
        if batch is None:
            return None
        try:
            response = batch.ContentRevision()
        except AttributeError:
            _invalidate_native_dispatch(window_handle)
            return None
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 문서 변경 토큰 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
        revision = str(response)
        if not revision.startswith("REV "):
            return None
        _remember_content_revision(window_handle, revision)
        return revision


@_invalidate_native_dispatch_on_exception
def inspect_native_page(
    window_handle: int,
    page: int,
    *,
    include_cells: bool = True,
) -> NativePageInspection | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 3 if include_cells else 4, *modules)
        if batch is None:
            return None
        try:
            response = (
                batch.InspectPageV3(page)
                if include_cells
                else batch.InspectPageSummary(page)
            )
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 쪽 구조 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_page_inspection(str(response))


@_invalidate_native_dispatch_on_exception
def inspect_native_pages(
    window_handle: int,
    pages: tuple[int, ...],
    *,
    include_cells: bool = True,
) -> tuple[NativePageInspection, ...]:
    if not pages:
        return ()
    if len(set(pages)) != len(pages) or any(page < 1 for page in pages):
        raise HwpLiveError("한컴 네이티브 다중 쪽 번호가 올바르지 않습니다")
    if not include_cells:
        inspected = tuple(
            inspect_native_page(window_handle, page, include_cells=False)
            for page in pages
        )
        if any(page is None for page in inspected):
            raise HwpLiveError("한컴 네이티브 다중 쪽 구조를 읽지 못했습니다")
        return tuple(page for page in inspected if page is not None)
    inspected_pages: list[NativePageInspection] = []
    document_metadata: tuple[int, str, int] | None = None
    use_single_page_fallback = False
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 8, *modules)
        if batch is None:
            return ()
        try:
            for start in range(0, len(pages), NATIVE_INSPECT_PAGES_MAX):
                requested_chunk = pages[start : start + NATIVE_INSPECT_PAGES_MAX]
                response = str(
                    batch.InspectPagesV3(
                        ",".join(str(page) for page in requested_chunk)
                    )
                )
                decoded_chunk = decode_page_inspection_batch(response)
                if tuple(page.page for page in decoded_chunk) != requested_chunk:
                    raise HwpLiveError(
                        "한컴 네이티브 다중 쪽 구조 순서가 요청과 다릅니다"
                    )
                chunk_metadata = (
                    decoded_chunk[0].document_id,
                    decoded_chunk[0].full_name,
                    decoded_chunk[0].page_count,
                )
                if document_metadata is None:
                    document_metadata = chunk_metadata
                elif chunk_metadata != document_metadata:
                    raise HwpLiveError(
                        "한컴 네이티브 다중 쪽 구조 응답의 문서 정보가 청크 사이에서 일치하지 않습니다"
                    )
                inspected_pages.extend(decoded_chunk)
        except AttributeError:
            _invalidate_native_dispatch(window_handle)
            inspected_pages.clear()
            use_single_page_fallback = True
        except HwpLiveError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 다중 쪽 구조 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
    if use_single_page_fallback:
        inspected = tuple(
            inspect_native_page(window_handle, page, include_cells=True)
            for page in pages
        )
        if any(page is None for page in inspected):
            raise HwpLiveError("한컴 네이티브 다중 쪽 구조를 읽지 못했습니다")
        return tuple(page for page in inspected if page is not None)
    return tuple(inspected_pages)


@_invalidate_native_dispatch_on_exception
def read_native_routing_context(
    window_handle: int,
    page_hint: int | None,
) -> NativePageInspection | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 8, *modules)
        if batch is None:
            return None
        try:
            response = batch.InspectRoutingContext(
                0 if page_hint is None else page_hint
            )
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 빠른 구조 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_page_inspection(str(response))


def _graph_read_available(window_handle: int) -> bool:
    """Whether this route advertises protocol-15 production GraphRead."""
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 7, *modules)
        if batch is None:
            return False
        try:
            return int(batch.GraphProtocolVersion) == 15
        except AttributeError:
            return False
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 그래프 기능 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None


def _inspect_native_structure_graph(  # pyright: ignore[reportUnusedFunction]
    window_handle: int,
    page: int,
) -> NativeDetailedInspection | None:
    from hwp_native_graph_cache import NativeGraphCache
    from hwp_native_graph_models import NativeGraphQuery
    from hwp_native_graph_structure_projection import (
        native_detailed_inspection_from_graph,
    )

    if not _graph_read_available(window_handle):
        return None
    state = read_native_snapshot(window_handle)
    if state is None:
        return None
    target_page = abs(page) if page != 0 else state.current_page
    if target_page < 1 or target_page > state.page_count:
        raise HwpLiveError("요청한 쪽이 현재 문서 범위를 벗어났습니다")
    identity = hashlib.sha256(
        f"{state.document_id}\0{state.full_name}".encode("utf-8")
    ).hexdigest()
    request = NativeGraphQuery(
        store_id=f"hds1-{identity}",
        projection_bits=0xFFF,
        query_digest=hashlib.sha256(b"HDS1-COMPAT-GRAPH-V1").hexdigest(),
    )
    bound, reusable = bind_graph_query_content_freshness(window_handle, request)
    cache_root = Path(
        os.environ.get(
            "HWP_NATIVE_GRAPH_CACHE_ROOT",
            str(
                Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                / "GSG-HWP"
                / "native-graph-cache"
            ),
        )
    )
    cache = NativeGraphCache(cache_root)

    def producer() -> NativeGraphTransfer:
        return query_native_graph(window_handle, bound)

    snapshot = (
        cache.load_or_publish_transfer(bound, producer)
        if reusable
        else cache.publish_transfer(bound, producer)
    )
    try:
        return native_detailed_inspection_from_graph(
            snapshot,
            document_id=state.document_id,
            full_name=state.full_name,
            page=target_page,
            page_count=state.page_count,
        )
    finally:
        snapshot.close()


@_invalidate_native_dispatch_on_exception
def _inspect_native_structure_legacy(
    window_handle: int,
    page: int,
) -> NativeDetailedInspection | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 7, *modules)
        if batch is None:
            return None
        try:
            response = batch.InspectStructure(page)
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 상세 구조 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_detailed_inspection(str(response))


def inspect_native_structure(
    window_handle: int,
    page: int,
) -> NativeDetailedInspection | None:
    return _inspect_native_structure_legacy(window_handle, page)


@_invalidate_native_dispatch_on_exception
def read_native_paragraph_styles(
    window_handle: int,
    *,
    list_id: int = 0,
    start: int = 0,
    limit: int = NATIVE_PARAGRAPH_STYLE_SCAN_MAX,
    character_shape: bool = False,
    paragraph_shape: bool = False,
    convention_detail: bool = False,
) -> NativeParagraphStyleScan | None:
    """Leading text + applied style for a run of paragraphs, in one call.

    The traversal lives in the bridge (LiveInspection.cpp
    ``InspectParagraphStyles``) because doing it from here would be one COM
    round trip per paragraph. Returns ``None`` when the bridge predates the
    command so callers keep their previous behaviour instead of failing.
    """
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 7, *modules)
        if batch is None:
            return None
        try:
            response = batch.InspectParagraphStyles(
                encode_paragraph_style_request(
                    list_id=list_id,
                    start=start,
                    limit=limit,
                    character_shape=character_shape,
                    paragraph_shape=paragraph_shape,
                    convention_detail=convention_detail,
                )
            )
        except AttributeError:
            # Bridge older than the paragraph scan. Not an error: the caller
            # falls back to style-name matching.
            _invalidate_native_dispatch(window_handle)
            return None
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 문단 스타일 조회에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_paragraph_style_scan(str(response))


@_invalidate_native_dispatch_on_exception
def preflight_native_text_patches(
    window_handle: int,
    request: NativeActionRequest,
) -> NativeTextPatchPreparation | None:
    """Resolve every text target without mutation on bridges that support it."""
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 14, *modules)
        if batch is None:
            return None
        literal = any(
            isinstance(command, TextPatchCommand) and command.literal_range is not None
            for command in request.commands
        )
        try:
            if literal:
                response = str(
                    batch.PrepareLiteralTextPatches(encode_action_request(request))
                )
            else:
                response = str(batch.PrepareTextPatches(encode_action_request(request)))
        except AttributeError:
            _invalidate_native_dispatch(window_handle)
            if literal:
                raise HwpLiveError(
                    "현재 한컴 브리지는 원문 좌표 준비 기능이 없습니다. 문서를 저장한 뒤 한컴을 다시 열어 새 브리지를 로드하세요.",
                    mutation_started=False,
                    safe_to_repeat=True,
                ) from None
            return None
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 text.patch preflight에 실패했습니다",
                error,
            )
        finally:
            batch = None
    fields = response.split("\t")
    if len(fields) in (6, 8) and fields[:2] in (["HTP1", "ERROR"], ["HTP2", "ERROR"]):
        index = int(fields[2])
        code = fields[3]
        message = b64decode(fields[5], validate=True).decode("utf-8")
        # Bridges that carry the two trailing flags say for themselves whether
        # the refusal left the document untouched. Older ones send six fields
        # and no such claim, and the safe pair is the only thing that may be
        # assumed of a bridge that did not answer.
        safe_to_repeat = fields[6] != "0" if len(fields) == 8 else True
        mutation_started = fields[7] == "1" if len(fields) == 8 else False
        raise HwpTextPatchPreflightError(
            index,
            code,
            b64decode(fields[4], validate=True).decode("utf-8"),
            message,
            mutation_started=mutation_started,
            safe_to_repeat=safe_to_repeat,
        )
    if len(fields) < 5 or fields[1:2] != ["OK"] or fields[0] not in {"HTP1", "HTP2"}:
        raise HwpLiveError(
            "한컴 네이티브 text.patch preflight 응답이 올바르지 않습니다"
        )
    revision = b64decode(fields[2], validate=True).decode("utf-8")
    target_count = int(fields[3])
    elapsed_microseconds = int(fields[4])
    targets: list[PreparedTextPatchTarget] = []
    if fields[0] == "HTP2":
        target_width = 12
        if len(fields) != 5 + target_count * target_width:
            raise HwpLiveError(
                "한컴 네이티브 text.patch inverse preflight 응답이 올바르지 않습니다"
            )
        for offset in range(5, len(fields), target_width):
            item = fields[offset : offset + target_width]
            start = NativePosition(int(item[0]), int(item[1]), int(item[2]))
            end = NativePosition(int(item[3]), int(item[4]), int(item[5]))
            targets.append(
                PreparedTextPatchTarget(
                    NativeSelection(True, start, end),
                    b64decode(item[6], validate=True).decode("utf-8"),
                    NativeCharacterFormat(
                        b64decode(item[7], validate=True).decode("utf-8"),
                        int(item[8]),
                        item[9] == "1",
                        int(item[10]),
                    ),
                    int(item[11].split(",", 1)[0]),
                    int(item[11].split(",", 1)[1]),
                )
            )
    if (
        not content_revision_is_complete(revision)
        or target_count < 0
        or elapsed_microseconds < 0
    ):
        raise HwpLiveError(
            "한컴 네이티브 text.patch preflight receipt가 올바르지 않습니다"
        )
    return NativeTextPatchPreparation(
        revision, target_count, elapsed_microseconds, tuple(targets)
    )


@_invalidate_native_dispatch_on_exception
def execute_native_actions(
    window_handle: int,
    request: NativeActionRequest,
    *,
    minimum_version: int = 2,
    expected_content_signature: str = "",
    expected_content_revision: str = "",
) -> NativeActionResult | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(
            window_handle,
            (
                max(minimum_version, 14)
                if expected_content_revision
                else max(minimum_version, 13)
                if expected_content_signature
                else minimum_version
            ),
            *modules,
        )
        if batch is None:
            return None
        try:
            forget_cached_content_signatures(window_handle)
            payload = encode_action_request(request)
            response = (
                batch.ExecutePreparedTextPatches(payload, expected_content_revision)
                if expected_content_revision
                else batch.ExecuteActionsChecked(payload, expected_content_signature)
                if expected_content_signature
                else batch.ExecuteActions(payload)
            )
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            com_error,
        ) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 액션 배치 실행에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return decode_action_result(str(response))


def _decode_history_step(payload: str) -> NativeHistoryStepResult:
    fields = payload.split("\t")
    if len(fields) >= 2 and fields[:2] == ["HCH1", "ERROR"]:
        code = fields[2] if len(fields) > 2 else ""
        message = fields[3] if len(fields) > 3 else ""
        mutation_started = len(fields) > 4 and fields[4] == "1"
        raise HwpLiveError(
            f"한컴 네이티브 이력 사전조건이 거부되었습니다 ({code}: {message})",
            mutation_started=mutation_started,
        )
    if len(fields) != 6 or fields[:2] != ["HCH1", "OK"]:
        raise HwpLiveError("한컴 네이티브 이력 응답 형식이 올바르지 않습니다")
    applied = int(fields[2])
    elapsed = int(fields[3])
    if applied not in (0, 1) or elapsed < 0:
        raise HwpLiveError("한컴 네이티브 이력 응답 숫자가 올바르지 않습니다")
    before = fields[4]
    after = fields[5]
    if not checkpoint_signature_is_complete(
        before
    ) or not checkpoint_signature_is_complete(after):
        raise HwpLiveError(
            "한컴 네이티브 이력 내용 지문이 올바르지 않습니다",
            mutation_started=True,
        )
    return NativeHistoryStepResult(applied, elapsed, before, after)


@_invalidate_native_dispatch_on_exception
def execute_native_history_step(
    window_handle: int,
    direction: str,
    expected_content_signature: str,
) -> NativeHistoryStepResult | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 13, *modules)
        if batch is None:
            return None
        try:
            forget_cached_content_signatures(window_handle)
            response = batch.ExecuteHistory(
                direction,
                expected_content_signature,
            )
        except (
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            com_error,
        ) as error:
            _raise_native_call_error(
                window_handle,
                "한컴 네이티브 이력 사전조건 실행에 실패했습니다",
                error,
            )
        finally:
            batch = None
        return _decode_history_step(str(response))


@_invalidate_native_dispatch_on_exception
def probe_official_api(window_handle: int, payload: str) -> str | None:
    with _com_apartment() as modules:
        batch = _batch_for_window(window_handle, 6, *modules)
        if batch is None:
            return None
        try:
            response = str(batch.ProbeOfficialApi(payload))
        except (OSError, RuntimeError, TypeError, ValueError, com_error) as error:
            detail = f"{type(error).__name__}: {error}"
            _raise_native_call_error(
                window_handle,
                f"한컴 공식 API 네이티브 검증 호출에 실패했습니다: {detail}",
                error,
            )
        finally:
            batch = None
        return response


def _call_foreground_guard(window_handle: int, begin: bool) -> bool:
    # 미리보기·문서 열기·탭 전환 폴백은 파이썬이 한컴 COM을 직접 호출하므로
    # 네이티브 Invoke를 거치지 않는다. 그래서 브리지에 가드를 열고 닫는 명령만
    # 보내고, 무장 여부·거부 여부·복원 여부·모달 판정·사용자 의도 판별은 전부
    # C++ 쪽에 남긴다. 여기에는 정책이 한 줄도 없다.
    #
    # 여는 쪽과 닫는 쪽이 서로 다른 BatchAutomation 객체에 닿을 수 있다(탭
    # 전환 중 문서 라우트가 바뀐다). 그래서 브리지 쪽 가드는 프로세스 전역
    # 브래킷이고, 중첩은 깊이로 센다. 파이썬은 그 사실을 알 필요가 없다.
    #
    # 예외를 넓게 잡는 이유: 가드는 부가 기능이라 실패해도 편집·렌더는 그대로
    # 진행돼야 한다. 구버전 DLL은 GetIDsOfNames가 실패해 pywin32가
    # AttributeError를 던지고, 브리지를 못 찾으면 HwpLiveError가 난다. 어느
    # 쪽이든 "가드 없음"으로 조용히 내려앉는 것이 옳은 동작이다.
    if window_handle <= 0:
        return False
    try:
        with _com_apartment() as modules:
            batch = _batch_for_window(window_handle, 1, *modules)
            if batch is None:
                return False
            try:
                if begin:
                    _ = batch.BeginForegroundGuard()
                else:
                    _ = batch.EndForegroundGuard()
                return True
            finally:
                batch = None
    except Exception:
        return False


def begin_native_foreground_guard(window_handle: int) -> bool:
    return _call_foreground_guard(window_handle, True)


def end_native_foreground_guard(window_handle: int) -> bool:
    return _call_foreground_guard(window_handle, False)


@contextmanager
def native_foreground_guard(window_handle: int) -> Generator[None, None, None]:
    """한컴 COM을 파이썬이 직접 때리는 구간을 브리지 가드로 감싼다.

    구간의 시작과 끝만 알린다. 가드를 실제로 무장할지, 활성화를 거부할지,
    포커스를 되돌릴지는 전부 브리지(C++)가 판단한다. 열기에 실패해도 닫기는
    호출한다 - 브리지가 짝이 안 맞는 닫기를 무시하도록 되어 있고, 여기서
    성공 여부를 보고 분기하면 그 판단이 파이썬으로 새기 때문이다.
    """
    _ = begin_native_foreground_guard(window_handle)
    try:
        yield
    finally:
        _ = end_native_foreground_guard(window_handle)
