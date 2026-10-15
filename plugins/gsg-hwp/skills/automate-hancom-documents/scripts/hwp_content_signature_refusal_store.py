"""서명 거부 기억을 프로세스 경계 너머로 나르는 자리.

``hwp_live_native_batch`` 의 ``_content_signature_refusals`` 는 "엔진이 이 문서를
직렬화하지 못한다" 는, 한 번 17.2초를 주고 산 사실이다. 그 값이 프로세스 안
dict 에만 있으면 워커가 다시 뜰 때마다 같은 17.2초를 다시 산다. 여기는 그 dict
를 디스크에 적어 두었다가 새 워커의 첫 활성화에서 되읽는 창구다.

되읽은 기억은 **그 기억을 만든 문서에만** 쓸 수 있다. 거부를 부르는 것은 쪽수도
컨트롤 수도 글자 수도 아니라 임베드된 바이트 부피이고(OfficialApiState.cpp
:803-805 "HWPML serialisation is proportional to what was embedded" — 82MB 는
서명되고 317MB 는 거부된다), 그 부피는 REV 토큰의 어느 필드에도 없다. 이미지가
무거운 317MB 문서가 3MB 짜리 텍스트 보고서보다 쪽·컨트롤·글자 수가 **적을** 수
있으므로, 크기 측정치만으로 "다른 문서도 거부될 것" 을 추론하면 서명 가능한
문서를 거부로 잘못 답한다(실측: 수리 전 SIG 반환·체크포인트 가능 → 잘못된 수리
후 None·체크포인트 불가). 그래서 항목은 문서 신원(세션 태그 + controlHash +
textHash)을 함께 싣고, 적재 시점에 그 셋이 지금 REV 와 모두 같을 때만 살아난다.
하나라도 다르면 항목은 버려지고 이 수리 이전과 똑같이 엔진에 묻는다.

모든 I/O 는 best effort 다. 이 기억은 편집을 빠르게 만드는 장치이지 편집의
전제가 아니므로, 디스크가 읽히지 않으면 예외가 아니라 "기억 없음" 이 되어야
한다 — 기억이 없을 때의 동작은 이 수리 이전의 동작 그 자체다.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Final, cast

# 시험과 격리 실행이 실제 워커의 원장을 건드리지 않게 하는 유일한 수단.
# `HWP_GRAPH_FRESHNESS_STATE_ROOT`(같은 모듈의 창 신선도 워터마크)와 같은 관례다.
REFUSAL_STORE_ROOT_ENV: Final = "HWP_CONTENT_SIGNATURE_REFUSAL_ROOT"

# 2 는 문서 신원(control_hash, text_hash)을 싣기 시작한 세대다. 1 로 적힌 항목은
# 크기 측정치밖에 없어 어느 문서의 기억인지 말하지 못하므로, 되읽어도 적재 조건을
# 영영 만족시킬 수 없다. 해독 실패와 같이 취급해 그 자리에서 버린다.
_SCHEMA_VERSION: Final = 2

_STORE_DIRECTORY: Final = "content-signature-refusals-v1"
_ENTRY_PREFIX: Final = "window-"
_ENTRY_SUFFIX: Final = ".json"
# 임시 파일은 `window-<h>.json.<hex>.tmp` 라 이 글롭에 걸리지 않는다. 걸리면
# 반쯤 적힌 항목을 해독 실패로 읽고 남의 원자적 교체를 지우게 된다.
_ENTRY_GLOB: Final = f"{_ENTRY_PREFIX}*{_ENTRY_SUFFIX}"
# 한 항목은 정수 여덟 개다. 이보다 큰 파일은 이 저장소가 쓴 것이 아니므로
# 읽지 않는다 — 손상된 원장이 워커 기동을 붙잡고 늘어지지 못하게 하는 상한.
_MAX_ENTRY_BYTES: Final = 4096
# 항목 수는 프로덕션 경로의 비용이 아니다. 적재는 활성화되는 창의 파일 하나를
# stat+read 할 뿐이고(`load_signature_refusal`), 원장을 훑는 경로는 프로덕션에
# 남아 있지 않다 — 원장이 몇 건이든 활성화 비용은 같다. 항목은 자기 창의
# 재바인드나 적재 시점의 불일치 판정으로만 지워지므로, 재바인드 없이 끝난
# 워커의 항목은 남는다. 창 핸들은 재활용되니 다음 주인이 같은 이름을 덮어쓴다.
# 이 상한이 지키는 것은 그 자연 증가가 아니라 `load_signature_refusals` 다:
# 손상되거나 밖에서 흘러든 원장이 전체 스캔을 무한정 길게 만들지 못하게 한다.
_MAX_ENTRIES: Final = 4096

_MEASURE_FIELDS: Final = ("session_tag", "page_count", "control_count", "text_length")
_IDENTITY_FIELDS: Final = ("control_hash", "text_hash")

# (세션 태그, 쪽수, 컨트롤 수, 글자 수) — 메모리 dict 가 담는 값 그대로.
RefusalMeasures = tuple[int, int, int, int]
# (controlHash, textHash) — 어느 문서의 기억인지 말하는 부분.
RefusalContentIdentity = tuple[int, int]
PersistedRefusal = tuple[RefusalMeasures, RefusalContentIdentity]


def default_refusal_store_root() -> Path:
    """거부 기억이 사는 곳. 환경변수가 있으면 그쪽이 이긴다."""
    override = os.environ.get(REFUSAL_STORE_ROOT_ENV)
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "HancomDocumentAutomation" / _STORE_DIRECTORY


def _entry_path(root: Path, window_handle: int) -> Path:
    return root / f"{_ENTRY_PREFIX}{window_handle:d}{_ENTRY_SUFFIX}"


def _field(raw: object) -> int | None:
    # `bool` 은 `int` 의 하위형이다. 정수 자리에 True 가 적힌 파일은 이 저장소가
    # 쓴 것이 아니므로 정수로 받아 주지 않는다.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _decode_entry(raw: object) -> tuple[int, PersistedRefusal] | None:
    """항목 하나를 해독한다. ``None`` 은 "이 저장소가 쓴 항목이 아니다" 이다."""
    if not isinstance(raw, dict):
        return None
    entry = cast(dict[str, object], raw)
    if entry.get("schema_version") != _SCHEMA_VERSION:
        return None
    window_handle = _field(entry.get("window_handle"))
    if window_handle is None:
        return None
    values: list[int] = []
    for name in (*_MEASURE_FIELDS, *_IDENTITY_FIELDS):
        value = _field(entry.get(name))
        if value is None or value < 0:
            return None
        values.append(value)
    session_tag, pages, controls, text_length, control_hash, text_hash = values
    return window_handle, (
        (session_tag, pages, controls, text_length),
        (control_hash, text_hash),
    )


def store_signature_refusal(
    window_handle: int,
    measures: RefusalMeasures,
    content_identity: RefusalContentIdentity,
    *,
    root: Path | None = None,
) -> bool:
    """거부 기억 한 건을 원자적으로 남긴다. 성공 여부를 돌려주되 던지지 않는다.

    임시 파일에 다 적고 ``os.replace`` 로 갈아 끼우므로, 절반만 적힌 항목을
    다음 워커가 읽는 일은 없다(``hwp_operation_journal_store.replace`` 와 같은
    방식). 실패는 삼킨다: 이 저장은 편집 사이에 끼어 있고, 원장에 못 적은 것이
    편집을 멈출 이유는 되지 못한다.
    """
    target = default_refusal_store_root() if root is None else root
    session_tag, pages, controls, text_length = measures
    control_hash, text_hash = content_identity
    payload = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "window_handle": window_handle,
            "session_tag": session_tag,
            "page_count": pages,
            "control_count": controls,
            "text_length": text_length,
            "control_hash": control_hash,
            "text_hash": text_hash,
        }
    )
    path = _entry_path(target, window_handle)
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        target.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                _ = stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def drop_signature_refusals(
    window_handle: int | None = None,
    *,
    root: Path | None = None,
) -> None:
    """거부 기억을 지운다 — 창 하나, 또는 ``None`` 이면 전부. 던지지 않는다."""
    target = default_refusal_store_root() if root is None else root
    try:
        if window_handle is not None:
            _entry_path(target, window_handle).unlink(missing_ok=True)
            return
        for path in target.glob(_ENTRY_GLOB):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        return


def load_signature_refusal(
    window_handle: int,
    *,
    root: Path | None = None,
) -> PersistedRefusal | None:
    """창 **하나** 의 거부 기억. 없거나 읽히지 않으면 ``None``. 던지지 않는다.

    ``load_signature_refusals`` 와 해독 규칙은 같고 대상만 좁다: 항목 파일 하나만
    열어 보고, 해독에 실패한 그 항목만 그 자리에서 지운다. 원장 전체를 훑지
    않는 것이 요점이다 — 적재는 **활성화되는 창 하나** 에 대한 일이고, 남의 창
    항목까지 읽어 오면 그 창을 바인딩하지 않은 프로세스가 남의 한/글 인스턴스에
    REV 를 보내고(``hwp_live_native_batch._batch_for_window`` 는
    ``GetWindowThreadProcessId`` 로 살아 있는 어느 창이든 그 프로세스로 디스패치를
    만든다) 불일치면 남의 항목을 지운다. 그 좁힘의 근거는
    ``hwp_live_native_batch._adopt_persisted_refusal`` 주석에 있다.

    파일 이름이 가리키는 창과 안에 적힌 창이 다르면 적재하지 않되 **지우지도
    않는다**: 이 저장소는 둘을 늘 같이 쓰므로 그런 파일은 우리 것이 아니고,
    지우는 쪽을 고르면 이 수리가 없애려는 바로 그 "남의 항목 삭제" 가 된다.
    """
    target = default_refusal_store_root() if root is None else root
    path = _entry_path(target, window_handle)
    try:
        if path.stat().st_size > _MAX_ENTRY_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        decoded = _decode_entry(cast(object, json.loads(raw.decode("utf-8"))))
    except (UnicodeDecodeError, ValueError):
        decoded = None
    if decoded is None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    written_handle, persisted = decoded
    if written_handle != window_handle:
        return None
    return persisted


def load_signature_refusals(*, root: Path | None = None) -> dict[int, PersistedRefusal]:
    """디스크에 남은 거부 기억 전부. 읽히지 않는 것은 조용히 빠진다.

    삭제는 **해독 실패에만** 한다. 읽지 못한 것(``OSError``)은 지우지 않는다:
    다른 워커가 같은 항목을 ``os.replace`` 로 갈아 끼우는 순간을 읽으면 Windows
    가 ``PermissionError`` 를 주는데(경합 4초에 398회 실측), 그것을 손상으로
    읽으면 멀쩡한 항목을 지운다. ``OperationJournalStore.read_lookup_metadata``
    가 OSError 를 unreadable 로만 표시하고 파일에는 손대지 않는 것과 같은 규칙
    이다 — "지금 못 읽었다" 와 "이건 우리 것이 아니다" 는 다른 말이다.

    해독에 실패한 항목은 그 자리에서 지운다. 이 저장소가 쓴 적 없는 모양이거나
    폐기된 스키마라 다음 적재에서도 같은 답을 낼 것이고, 남겨 두면 원장이
    영원히 부푼다.
    """
    target = default_refusal_store_root() if root is None else root
    loaded: dict[int, PersistedRefusal] = {}
    try:
        paths = sorted(target.glob(_ENTRY_GLOB))[:_MAX_ENTRIES]
    except OSError:
        return loaded
    for path in paths:
        try:
            if path.stat().st_size > _MAX_ENTRY_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            decoded = _decode_entry(cast(object, json.loads(raw.decode("utf-8"))))
        except (UnicodeDecodeError, ValueError):
            decoded = None
        if decoded is None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        window_handle, persisted = decoded
        loaded[window_handle] = persisted
    return loaded
