from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Protocol, final

from pydantic import Field, ValidationError, model_validator

from hwp_live_layout_contract import LayoutPlan
from hwp_live_structure_contract import (
    DocumentStructure,
    FastPageControl,
    FastPageInspection,
    StructureTable,
)
from hwp_live_table_contract import TableBlock, TableCell, TableMerge
from hwp_live_values import ContractModel
from hwp_object_control_types import is_picture_control_type
from hwp_operation_contract import (
    HwpOperateInputs,
    HwpOperatePolicy,
    HwpOperatePostconditions,
    HwpOperateTarget,
    OperationResult,
)
from hwp_operation_registry import operation_registry
from hwp_priority_picture_edit import (
    COPY_DESTINATION_CELL_PARAMETER,
    COPY_DESTINATION_TABLE_PARAMETER,
)
from hwp_priority_recipe_contract import HwpPriorityRecipeInputs
from hwp_public_action_contract import PublicOperationId
from hwp_public_contract import (
    PublicActionResult,
    PublicTableTarget,
    to_public_action_result,
)


RESTRUCTURE_INTENT = "사진 표를 병합 없이 새 표로 재구성"


class PublicPhotoCellMapping(ContractModel):
    source_cell: str = Field(pattern=r"^[A-Za-z]+[1-9][0-9]*$")
    photo_cell: str = Field(pattern=r"^[A-Za-z]+[1-9][0-9]*$")
    label_cell: str | None = Field(default=None, pattern=r"^[A-Za-z]+[1-9][0-9]*$")


class PublicRestructureLayout(ContractModel):
    columns: int = Field(ge=1, le=50)
    bands: int | None = Field(default=None, ge=1, le=25)
    cell_mapping: tuple[PublicPhotoCellMapping, ...] = Field(default=(), max_length=200)
    merges: tuple[TableMerge, ...] = Field(
        default=(),
        max_length=100,
        description=(
            "최종 새 표를 만들 때 적용할 hwp_insert_layout TableBlock 병합입니다. "
            "기본 사진 격자는 빈 목록이며 병합하지 않습니다."
        ),
    )

    @model_validator(mode="after")
    def require_one_layout_mode(self) -> PublicRestructureLayout:
        if (self.bands is None) == (not self.cell_mapping):
            raise ValueError("bands 또는 cell_mapping 중 하나만 지정해야 합니다")
        source_cells = [item.source_cell.upper() for item in self.cell_mapping]
        photo_cells = [item.photo_cell.upper() for item in self.cell_mapping]
        if len(source_cells) != len(set(source_cells)):
            raise ValueError("cell_mapping.source_cell은 중복될 수 없습니다")
        if len(photo_cells) != len(set(photo_cells)):
            raise ValueError("cell_mapping.photo_cell은 중복될 수 없습니다")
        for item in self.cell_mapping:
            destinations = (item.photo_cell, item.label_cell)
            for address in destinations:
                if address is None:
                    continue
                if _address_key(address)[1] > self.columns:
                    raise ValueError(
                        "cell_mapping 목적지 열은 layout.columns 안에 있어야 합니다"
                    )
        return self


class PublicRestructuredPhoto(ContractModel):
    source_id: str = Field(min_length=1, max_length=100)
    final_id: str = Field(min_length=1, max_length=100)
    source_cell: str = Field(pattern=r"^[A-Z]+[1-9][0-9]*$")
    final_cell: str = Field(pattern=r"^[A-Z]+[1-9][0-9]*$")
    width_mm: float = Field(gt=0, le=250)
    height_mm: float = Field(gt=0, le=350)


class PublicRestructureTableResult(PublicActionResult):
    source_table_id: str | None = Field(default=None, max_length=100)
    new_table_id: str | None = Field(default=None, max_length=100)
    photo_map: tuple[PublicRestructuredPhoto, ...] = Field(default=(), max_length=200)


@dataclass(frozen=True, slots=True)
class _PhotoPlan:
    mapping: PublicPhotoCellMapping
    source_id: str
    width_mm: float
    height_mm: float


@dataclass(frozen=True, slots=True)
class _ResumeState:
    source_table_id: str
    source_page: int
    document_path: str | None
    final_table_id: str
    photos: tuple[_PhotoPlan, ...]
    copied: tuple[PublicRestructuredPhoto, ...]
    label_values: dict[str, str]
    caption: str | None
    operations: tuple[OperationResult, ...]


class PublicRestructureExecutor(Protocol):
    async def execute(
        self,
        intent: str,
        inputs: HwpOperateInputs,
        guards: object | None,
    ) -> OperationResult: ...

    async def inspect_page_fast(
        self, document_selector: str | None, page: int, include_cells: bool
    ) -> FastPageInspection: ...

    async def read_table_structure(
        self, document_path: str | None, page: int
    ) -> DocumentStructure: ...


def _column_name(index: int) -> str:
    value = ""
    while index >= 0:
        value = chr(65 + index % 26) + value
        index = index // 26 - 1
    return value


def _address(column: int, row: int) -> str:
    return f"{_column_name(column)}{row + 1}"


def _address_key(address: str) -> tuple[int, int]:
    letters = "".join(character for character in address.upper() if character.isalpha())
    digits = address[len(letters) :]
    column = 0
    for character in letters:
        column = column * 26 + ord(character) - 64
    return int(digits), column


def _changed_pages(operations: tuple[OperationResult, ...]) -> tuple[int, ...]:
    pages = {
        page
        for operation in operations
        for page in (
            *operation.changed_pages,
            *((operation.current_page,) if operation.modified else ()),
        )
        if page is not None
    }
    return tuple(sorted(pages))


def _state_token(operations: tuple[OperationResult, ...]) -> str | None:
    for operation in reversed(operations):
        token = (
            operation.structure_digest_after
            or operation.after_document_hash
            or operation.result_digest
        )
        if token is not None:
            return token
    return None


def _failed(
    operation_id: str,
    message: str,
    *,
    modified: bool,
    source_table_id: str | None,
    photo_map: tuple[PublicRestructuredPhoto, ...] = (),
    created_ids: tuple[str, ...] = (),
    operations: tuple[OperationResult, ...] = (),
    retry_safe: bool = False,
    guidance: tuple[str, ...] = (),
    new_table_id: str | None = None,
    needs_input: bool = False,
    state_token: str | None = None,
) -> PublicRestructureTableResult:
    return PublicRestructureTableResult(
        status=(
            "needs_input"
            if needs_input
            else ("partial_failure" if modified else "failed")
        ),
        message=message,
        request_id=operation_id,
        runtime=to_public_action_result(
            OperationResult(
                request_id=operation_id,
                status="partial_change" if modified else "known_failure",
                query=RESTRUCTURE_INTENT,
                registry_entries=operation_registry().count,
                lookup_microseconds=0,
                message=message,
                modified=modified,
                partial_change=modified,
            ),
            (),
        ).runtime,
        verified=False,
        modified=modified,
        required_inputs=("layout",) if needs_input else (),
        retry_safe=retry_safe or needs_input,
        retry_operation_id=None,
        source_table_id=source_table_id,
        new_table_id=new_table_id,
        photo_map=photo_map,
        created_target_ids=created_ids,
        affected_pages=_changed_pages(operations),
        state_token=state_token or _state_token(operations),
        affected_target_ids=tuple(
            target_id
            for target_id in (source_table_id, new_table_id)
            if target_id is not None
        ),
        selected_target_id=new_table_id,
        input_guidance=guidance,
    )


def _public(
    result: OperationResult,
    *,
    operation_id: str,
    source_table_id: str,
    new_table_id: str,
    photos: tuple[PublicRestructuredPhoto, ...],
    operations: tuple[OperationResult, ...],
) -> PublicRestructureTableResult:
    base = to_public_action_result(
        result,
        (),
        selected_target_id=new_table_id,
    )
    created = (new_table_id, *(photo.final_id for photo in photos))
    payload = base.model_dump()
    payload.update(
        {
            "request_id": operation_id,
            "message": (
                f"사진 {len(photos)}장을 원본 내장 바이트 재인코딩 없이 직접 복사해 "
                "검증한 뒤 원본 표를 새 표로 교체했습니다."
            ),
            "created_target_ids": created,
            "selected_target_id": new_table_id,
            "affected_target_ids": (source_table_id, new_table_id),
            "affected_pages": _changed_pages(operations),
            "state_token": _state_token(operations),
            "source_table_id": source_table_id,
            "new_table_id": new_table_id,
            "photo_map": photos,
            "input_guidance": base.input_guidance,
        }
    )
    return PublicRestructureTableResult.model_validate(payload)


@final
class HwpPublicRestructureTools:
    __slots__ = ("_executor", "_receipts", "_resumable")

    def __init__(self, executor: PublicRestructureExecutor) -> None:
        self._executor = executor
        self._receipts: dict[str, tuple[str, PublicRestructureTableResult]] = {}
        self._resumable: dict[str, _ResumeState] = {}

    async def _execute(
        self,
        operation_id: str,
        suffix: str,
        operation: str,
        *,
        document_path: str | None,
        target: HwpOperateTarget | None = None,
        layout: LayoutPlan | None = None,
        parameters: dict[str, object] | None = None,
        recipe: HwpPriorityRecipeInputs | None = None,
    ) -> OperationResult:
        digest = sha256(f"{operation_id}:{suffix}".encode()).hexdigest()[:16]
        child_operation_id = f"{operation_id[:80]}:{suffix}:{digest}"
        return await self._executor.execute(
            RESTRUCTURE_INTENT,
            HwpOperateInputs(
                request_id=child_operation_id,
                document=document_path,
                operation=operation,  # pyright: ignore[reportArgumentType]
                target=target,
                layout=layout,
                parameters={} if parameters is None else parameters,  # pyright: ignore[reportArgumentType]
                recipe=recipe,
                policy=HwpOperatePolicy(ambiguity="return_candidates", atomic=False),
                postconditions=HwpOperatePostconditions(verify_structure=True),
            ),
            None,
        )

    async def _source(
        self, document_path: str | None, target: PublicTableTarget
    ) -> tuple[StructureTable, tuple[FastPageInspection, ...]] | None:
        page = 0 if target.page is None else target.page
        structure = await self._executor.read_table_structure(document_path, page)
        table = next(
            (
                item
                for item in structure.tables
                if item.control_instance_id == target.target_id
            ),
            None,
        )
        if table is None:
            return None
        inspections = tuple(
            [
                await self._executor.inspect_page_fast(document_path, page, True)
                for page in range(table.page_start, table.page_end + 1)
            ]
        )
        return table, inspections

    async def _find_table(
        self,
        document_path: str | None,
        table_id: str,
        page_hint: int = 0,
    ) -> StructureTable | None:
        first = await self._executor.read_table_structure(document_path, page_hint)
        for table in first.tables:
            if table.control_instance_id == table_id:
                return table
        # Reflow normally moves a table to a neighbouring page. Do not restart
        # a detailed document scan at page one for every copy and verification.
        pages = sorted(
            (page for page in range(1, first.page_count + 1) if page != first.page),
            key=lambda page: (abs(page - first.page), page < first.page),
        )
        for page in pages:
            structure = await self._executor.read_table_structure(document_path, page)
            for table in structure.tables:
                if table.control_instance_id == table_id:
                    return table
        return None

    async def _table_pictures(
        self,
        document_path: str | None,
        table: StructureTable,
    ) -> tuple[FastPageControl, ...]:
        pictures = []
        for page in range(table.page_start, table.page_end + 1):
            inspection = await self._executor.inspect_page_fast(
                document_path, page, True
            )
            pictures.extend(
                control
                for control in inspection.controls
                if is_picture_control_type(control.control_type)
                and control.parent_table_instance_id == table.control_instance_id
            )
        return tuple(pictures)

    async def _resume(
        self,
        operation_id: str,
        fingerprint: str,
        state: _ResumeState,
    ) -> PublicRestructureTableResult:
        return await self._complete_final(
            operation_id,
            fingerprint,
            state,
            resuming=True,
        )

    async def _complete_final(
        self,
        operation_id: str,
        fingerprint: str,
        state: _ResumeState,
        *,
        resuming: bool,
    ) -> PublicRestructureTableResult:
        operations = list(state.operations)
        source_table = await self._find_table(
            state.document_path, state.source_table_id, state.source_page
        )
        final_table = await self._find_table(
            state.document_path, state.final_table_id, state.source_page
        )
        if source_table is None or final_table is None:
            return _failed(
                operation_id,
                "재개에 필요한 원본 표 또는 최종 표를 찾지 못했습니다.",
                modified=True,
                source_table_id=state.source_table_id,
                created_ids=(state.final_table_id,),
                operations=tuple(operations),
                new_table_id=state.final_table_id,
            )
        completed = list(state.copied)
        if resuming and completed:
            existing = {
                picture.instance_id: picture
                for picture in await self._table_pictures(
                    state.document_path, final_table
                )
            }
            if any(
                photo.final_id not in existing
                or existing[photo.final_id].parent_cell_address != photo.final_cell
                or abs((existing[photo.final_id].width_mm or 0) - photo.width_mm) > 0.1
                or abs((existing[photo.final_id].height_mm or 0) - photo.height_mm)
                > 0.1
                for photo in completed
            ):
                return _failed(
                    operation_id,
                    "미완성 최종 표의 기존 사진 ID, 칸 또는 크기가 달라 재개하지 않았습니다. 원본 표는 그대로 남아 있습니다.",
                    modified=True,
                    source_table_id=state.source_table_id,
                    photo_map=tuple(completed),
                    created_ids=(state.final_table_id,),
                    operations=tuple(operations),
                    new_table_id=state.final_table_id,
                )

        for index in range(len(completed), len(state.photos)):
            photo = state.photos[index]
            copied = await self._execute(
                operation_id,
                f"final-copy-{index}",
                "image.resize",
                document_path=state.document_path,
                target=HwpOperateTarget(
                    kind="picture",
                    control_instance_id=photo.source_id,
                    page_hint=source_table.page_start,
                ),
                parameters={
                    COPY_DESTINATION_TABLE_PARAMETER: state.final_table_id,
                    COPY_DESTINATION_CELL_PARAMETER: photo.mapping.photo_cell,
                },
                recipe=HwpPriorityRecipeInputs(
                    picture_width_mm=photo.width_mm,
                    picture_height_mm=photo.height_mm,
                ),
            )
            operations.append(copied)
            if copied.status != "executed" or not copied.created_control_ids:
                self._resumable[fingerprint] = _ResumeState(
                    source_table_id=state.source_table_id,
                    source_page=state.source_page,
                    document_path=state.document_path,
                    final_table_id=state.final_table_id,
                    photos=state.photos,
                    copied=tuple(completed),
                    label_values=state.label_values,
                    caption=state.caption,
                    operations=tuple(operations),
                )
                return _failed(
                    operation_id,
                    f"사진 {index + 1}/{len(state.photos)} 직접 복사에서 멈췄습니다. 원본 표 {state.source_table_id}과 미완성 최종 표 {state.final_table_id}은 모두 남아 있습니다.",
                    modified=True,
                    source_table_id=state.source_table_id,
                    photo_map=tuple(completed),
                    created_ids=(
                        state.final_table_id,
                        *(item.final_id for item in completed),
                    ),
                    operations=tuple(operations),
                    retry_safe=True,
                    guidance=(
                        "새 operation_id로 같은 인자를 보내면 최종 표의 다음 사진부터 재개합니다.",
                    ),
                    new_table_id=state.final_table_id,
                    state_token=(
                        await self._executor.read_table_structure(
                            state.document_path, final_table.page_start
                        )
                    ).state_token,
                )
            completed.append(
                PublicRestructuredPhoto(
                    source_id=photo.source_id,
                    final_id=copied.created_control_ids[0],
                    source_cell=photo.mapping.source_cell.upper(),
                    final_cell=photo.mapping.photo_cell.upper(),
                    width_mm=photo.width_mm,
                    height_mm=photo.height_mm,
                )
            )

        verified_source = await self._find_table(
            state.document_path, state.source_table_id, state.source_page
        )
        verified_table = await self._find_table(
            state.document_path, state.final_table_id, final_table.page_start
        )
        source_pictures = (
            ()
            if verified_source is None
            else await self._table_pictures(state.document_path, verified_source)
        )
        final_pictures = (
            ()
            if verified_table is None
            else await self._table_pictures(state.document_path, verified_table)
        )
        source_ids = {picture.instance_id for picture in source_pictures}
        final_by_id = {picture.instance_id: picture for picture in final_pictures}
        expected_labels = {
            address: value for address, value in state.label_values.items() if value
        }
        observed_labels = (
            {}
            if verified_table is None
            else {cell.address: cell.text for cell in verified_table.cells}
        )
        observed_caption = (
            None
            if verified_table is None or verified_table.caption is None
            else verified_table.caption.text
        )
        photos_verified = (
            len(completed) == len(state.photos)
            and len(source_pictures) == len(state.photos)
            and len(final_pictures) == len(state.photos)
            and source_ids == {photo.source_id for photo in state.photos}
            and set(final_by_id) == {photo.final_id for photo in completed}
            and all(
                final_by_id[photo.final_id].parent_cell_address == photo.final_cell
                and abs((final_by_id[photo.final_id].width_mm or 0) - photo.width_mm)
                <= 0.1
                and abs((final_by_id[photo.final_id].height_mm or 0) - photo.height_mm)
                <= 0.1
                for photo in completed
            )
        )
        if (
            verified_source is None
            or verified_table is None
            or any(
                observed_labels.get(address) != value
                for address, value in expected_labels.items()
            )
            or observed_caption != state.caption
            or not photos_verified
        ):
            self._resumable[fingerprint] = _ResumeState(
                source_table_id=state.source_table_id,
                source_page=state.source_page,
                document_path=state.document_path,
                final_table_id=state.final_table_id,
                photos=state.photos,
                copied=tuple(completed),
                label_values=state.label_values,
                caption=state.caption,
                operations=tuple(operations),
            )
            return _failed(
                operation_id,
                f"최종 표 {state.final_table_id}의 사진 수, ID, 칸, 크기, 라벨, 캡션 또는 쪽 검증에 실패했습니다. 원본 표는 삭제하지 않았습니다.",
                modified=True,
                source_table_id=state.source_table_id,
                photo_map=tuple(completed),
                created_ids=(
                    state.final_table_id,
                    *(photo.final_id for photo in completed),
                ),
                operations=tuple(operations),
                retry_safe=True,
                guidance=(
                    "새 operation_id로 같은 인자를 보내면 최종 표를 다시 검증합니다.",
                ),
                new_table_id=state.final_table_id,
                state_token=(
                    await self._executor.read_table_structure(
                        state.document_path, state.source_page
                    )
                ).state_token,
            )

        delete_source = await self._execute(
            operation_id,
            "source-delete",
            "control.delete",
            document_path=state.document_path,
            target=HwpOperateTarget(
                kind="control",
                page_hint=verified_source.page_start,
                control_instance_ids=(state.source_table_id,),
            ),
        )
        operations.append(delete_source)
        if delete_source.status != "executed":
            return _failed(
                operation_id,
                f"최종 표 {state.final_table_id}의 전량 검증은 끝났지만 원본 표 {state.source_table_id} 삭제에 실패해 두 표를 모두 남겼습니다.",
                modified=True,
                source_table_id=state.source_table_id,
                photo_map=tuple(completed),
                created_ids=(
                    state.final_table_id,
                    *(photo.final_id for photo in completed),
                ),
                operations=tuple(operations),
                new_table_id=state.final_table_id,
            )
        placed_table = await self._find_table(
            state.document_path, state.final_table_id, state.source_page
        )
        if placed_table is None or placed_table.page_start != state.source_page:
            return _failed(
                operation_id,
                f"원본 표 삭제 뒤 최종 표 {state.final_table_id}가 원본 쪽 "
                f"{state.source_page}에 자리 잡았는지 확인하지 못했습니다. 사진은 "
                "검증된 최종 표에 보존되어 있습니다.",
                modified=True,
                source_table_id=state.source_table_id,
                photo_map=tuple(completed),
                created_ids=(
                    state.final_table_id,
                    *(photo.final_id for photo in completed),
                ),
                operations=tuple(operations),
                new_table_id=state.final_table_id,
            )
        self._resumable.pop(fingerprint, None)
        return _public(
            delete_source,
            operation_id=operation_id,
            source_table_id=state.source_table_id,
            new_table_id=state.final_table_id,
            photos=tuple(completed),
            operations=tuple(operations),
        )

    async def hwp_restructure_table(
        self,
        *,
        operation_id: PublicOperationId,
        target: PublicTableTarget,
        layout: PublicRestructureLayout,
        labels: Annotated[list[str] | None, Field(max_length=200)] = None,
        photo_width_mm: Annotated[float | None, Field(ge=1, le=250)] = None,
        photo_height_mm: Annotated[float | None, Field(ge=1, le=350)] = None,
        column_widths_mm: Annotated[
            list[float] | None, Field(min_length=1, max_length=50)
        ] = None,
        page: Annotated[int | None, Field(ge=1)] = None,
        document_path: Annotated[
            str | None, Field(min_length=1, max_length=32_767)
        ] = None,
    ) -> PublicRestructureTableResult:
        payload = repr(
            (
                target.model_dump(mode="json"),
                layout.model_dump(mode="json"),
                labels,
                photo_width_mm,
                photo_height_mm,
                column_widths_mm,
                page,
                document_path,
            )
        )
        fingerprint = sha256(payload.encode()).hexdigest()
        cached = self._receipts.get(operation_id)
        if cached is not None:
            cached_fingerprint, cached_result = cached
            if cached_fingerprint == fingerprint:
                return cached_result.model_copy(
                    update={"idempotency_status": "replayed"}
                )
            return _failed(
                operation_id,
                "같은 operation_id에 다른 인자를 사용할 수 없습니다. 새 operation_id를 주세요.",
                modified=False,
                source_table_id=target.target_id,
            )
        try:
            resumable = self._resumable.get(fingerprint)
            if resumable is not None:
                result = await self._resume(operation_id, fingerprint, resumable)
            else:
                result = await self._restructure_table(
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                    target=target,
                    layout=layout,
                    labels=labels,
                    photo_width_mm=photo_width_mm,
                    photo_height_mm=photo_height_mm,
                    column_widths_mm=column_widths_mm,
                    page=page,
                    document_path=document_path,
                )
        except Exception as error:  # noqa: BLE001 - MCP 경계에서 raw 예외를 내보내지 않는다.
            result = _failed(
                operation_id,
                f"사진 표 재구성 중 예외가 발생했습니다: {type(error).__name__}: {error}",
                modified=True,
                source_table_id=target.target_id,
                guidance=(
                    "문서를 확인한 뒤 새 operation_id로 같은 인자를 재시도하세요.",
                ),
            )
        self._receipts[operation_id] = (fingerprint, result)
        return result

    async def _restructure_table(
        self,
        *,
        operation_id: PublicOperationId,
        fingerprint: str,
        target: PublicTableTarget,
        layout: PublicRestructureLayout,
        labels: list[str] | None,
        photo_width_mm: float | None,
        photo_height_mm: float | None,
        column_widths_mm: list[float] | None,
        page: int | None,
        document_path: str | None,
    ) -> PublicRestructureTableResult:
        source_id = target.target_id
        if source_id is None:
            return _failed(
                operation_id,
                "target에는 hwp_inspect_page_fast가 반환한 target_id가 필요합니다.",
                modified=False,
                source_table_id=None,
            )
        document = document_path or target.document_path
        if page is not None:
            target = target.model_copy(update={"page": page})
        source = await self._source(document, target)
        if source is None:
            return _failed(
                operation_id,
                f"원본 표 {source_id}을 찾지 못했습니다.",
                modified=False,
                source_table_id=source_id,
            )
        source_table, inspections = source
        source_cells = {cell.address: cell for cell in source_table.cells}
        pictures = sorted(
            (
                control
                for inspection in inspections
                for control in inspection.controls
                if is_picture_control_type(control.control_type)
                and control.parent_table_instance_id == source_id
                and control.parent_cell_address is not None
            ),
            key=lambda control: _address_key(control.parent_cell_address or "A1"),
        )
        if not pictures:
            return _failed(
                operation_id,
                "원본 표에서 재배치할 그림을 찾지 못했습니다.",
                modified=False,
                source_table_id=source_id,
            )
        if layout.cell_mapping:
            mapping = layout.cell_mapping
        else:
            capacity = layout.columns * (layout.bands or 0)
            if len(pictures) > capacity:
                return _failed(
                    operation_id,
                    f"새 표의 사진 칸 {capacity}개보다 원본 그림 {len(pictures)}장이 많습니다.",
                    modified=False,
                    source_table_id=source_id,
                )
            mapping = tuple(
                PublicPhotoCellMapping(
                    source_cell=picture.parent_cell_address or "A1",
                    photo_cell=_address(
                        index % layout.columns, (index // layout.columns) * 2
                    ),
                    label_cell=_address(
                        index % layout.columns, (index // layout.columns) * 2 + 1
                    ),
                )
                for index, picture in enumerate(pictures)
            )
        picture_by_cell = {picture.parent_cell_address: picture for picture in pictures}
        mapped_source_cells = {item.source_cell.upper() for item in mapping}
        if mapped_source_cells != set(picture_by_cell) or len(mapping) != len(pictures):
            return _failed(
                operation_id,
                "cell_mapping은 원본 표의 모든 그림을 빠짐없이 한 번씩 포함해야 합니다.",
                modified=False,
                source_table_id=source_id,
            )
        row_count = max(
            int(
                "".join(
                    character for character in item.photo_cell if character.isdigit()
                )
            )
            for item in mapping
        )
        row_count = max(
            row_count,
            *(
                int("".join(c for c in item.label_cell if c.isdigit()))
                for item in mapping
                if item.label_cell
            ),
        )
        total_width = next(
            (
                control.width_mm
                for inspection in inspections
                for control in inspection.controls
                if control.instance_id == source_id
            ),
            None,
        ) or sum(cell.width_mm or 0 for cell in source_table.cells if cell.row == 0)
        widths = (
            tuple(column_widths_mm)
            if column_widths_mm is not None
            else tuple(total_width / layout.columns for _ in range(layout.columns))
        )
        if len(widths) != layout.columns:
            return _failed(
                operation_id,
                "column_widths_mm 개수는 layout.columns와 같아야 합니다.",
                modified=False,
                source_table_id=source_id,
            )
        explicit_labels = None if labels is None else tuple(labels)
        label_values: dict[str, str] = {}
        consumed_text_cells: set[str] = set()
        for index, item in enumerate(mapping):
            if item.label_cell is None:
                continue
            source_address = item.source_cell.upper()
            source_row, source_column = _address_key(source_address)
            source_label = source_cells.get(
                _address(source_column - 1, source_row), source_cells[source_address]
            )
            # A replacement label accounts for the old label, not unrelated prose.
            if (
                explicit_labels is None
                or not source_label.has_picture
                or source_label.address == source_address
            ):
                consumed_text_cells.add(source_label.address)
            if explicit_labels is not None:
                if index >= len(explicit_labels):
                    return _failed(
                        operation_id,
                        "labels 개수는 배치할 사진 수와 같아야 합니다.",
                        modified=False,
                        source_table_id=source_id,
                    )
                label_values[item.label_cell.upper()] = explicit_labels[index]
            else:
                label_values[item.label_cell.upper()] = source_label.text
        if explicit_labels is not None and len(explicit_labels) != len(mapping):
            return _failed(
                operation_id,
                "labels 개수는 배치할 사진 수와 같아야 합니다.",
                modified=False,
                source_table_id=source_id,
            )
        unmapped_text = tuple(
            cell.address
            for cell in source_table.cells
            if cell.owner_address == cell.address
            and cell.text.strip()
            and cell.address not in consumed_text_cells
        )
        if unmapped_text:
            return _failed(
                operation_id,
                "사진 재구성에 포함되지 않는 본문 셀이 있어 문서를 변경하지 않았습니다: "
                + ", ".join(unmapped_text)
                + ". 기존 표의 셀 입력은 hwp_fill_table을 사용하세요.",
                modified=False,
                source_table_id=source_id,
                needs_input=True,
            )
        caption = None if source_table.caption is None else source_table.caption.text
        covered_cells = {
            (row, column)
            for merge in layout.merges
            for row in range(merge.row, merge.row + merge.row_span)
            for column in range(merge.column, merge.column + merge.column_span)
            if (row, column) != (merge.row, merge.column)
        }
        covered_addresses = {_address(column, row) for row, column in covered_cells}
        invalid_covered = sorted(
            address
            for address in covered_addresses
            if label_values.get(address, "")
            or any(item.photo_cell.upper() == address for item in mapping)
        )
        if invalid_covered:
            return _failed(
                operation_id,
                "최종 표 레이아웃이 올바르지 않습니다: merge-covered cells must not "
                "receive labels or photos; use the merge anchor cell instead: "
                + ", ".join(invalid_covered),
                modified=False,
                source_table_id=source_id,
                needs_input=True,
            )
        final_rows = tuple(
            tuple(
                TableCell()
                if (row, column) in covered_cells
                else TableCell(
                    text=label_values.get(_address(column, row), ""),
                    vertical_alignment="center",
                    alignment="center",
                )
                for column in range(layout.columns)
            )
            for row in range(row_count)
        )
        # 최종 TableBlock을 순수 preflight에서 완전히 검증한다. 이 아래부터만
        # 문서를 바꾸므로 잘못된 topology는 원본과 문서에 손대지 않는다.
        try:
            final_block = TableBlock(
                kind="table",
                caption=caption,
                rows=final_rows,
                column_widths_mm=widths,
                merges=layout.merges,
            )
        except ValidationError as error:
            return _failed(
                operation_id,
                f"최종 표 레이아웃이 올바르지 않습니다: {error}",
                modified=False,
                source_table_id=source_id,
                needs_input=True,
            )

        photo_plans = []
        for item in mapping:
            picture = picture_by_cell[item.source_cell.upper()]
            ratio = (picture.width_mm or 1) / (picture.height_mm or 1)
            row, column = _address_key(item.photo_cell)
            span = next(
                (
                    merge.column_span
                    for merge in layout.merges
                    if merge.row == row - 1 and merge.column == column - 1
                ),
                1,
            )
            width = photo_width_mm or sum(widths[column - 1 : column - 1 + span])
            height = photo_height_mm or width / ratio
            if photo_width_mm is None and photo_height_mm is not None:
                width = photo_height_mm * ratio
            photo_plans.append(
                _PhotoPlan(
                    mapping=item,
                    source_id=picture.instance_id,
                    width_mm=width,
                    height_mm=height,
                )
            )

        operations: list[OperationResult] = []
        final_insert = await self._execute(
            operation_id,
            "final-create",
            "document.insert_layout",
            document_path=document,
            target=HwpOperateTarget(
                kind="table",
                binding="below_selection",
                page_hint=source_table.page_start,
                control_instance_id=source_id,
            ),
            layout=LayoutPlan(
                target="current",
                blocks=(final_block,),
            ),
        )
        operations.append(final_insert)
        if final_insert.status != "executed" or not final_insert.created_control_ids:
            return _failed(
                operation_id,
                "원본 표 바로 뒤에 최종 표를 만들지 못해 원본 표는 변경하지 않았습니다: "
                + final_insert.message,
                modified=bool(final_insert.modified),
                source_table_id=source_id,
                created_ids=final_insert.created_control_ids,
                operations=tuple(operations),
            )
        final_id = final_insert.created_control_ids[0]
        final_table = await self._find_table(
            document, final_id, source_table.page_start
        )
        if final_table is None:
            return _failed(
                operation_id,
                "원본 표 바로 뒤에 만든 최종 표를 찾지 못해 사진 복사를 시작하지 않았습니다. 원본 표는 그대로 남아 있습니다.",
                modified=True,
                source_table_id=source_id,
                created_ids=(final_id,),
                operations=tuple(operations),
                new_table_id=final_id,
            )
        state = _ResumeState(
            source_table_id=source_id,
            source_page=source_table.page_start,
            document_path=document,
            final_table_id=final_id,
            photos=tuple(photo_plans),
            copied=(),
            label_values=label_values,
            caption=caption,
            operations=tuple(operations),
        )
        return await self._complete_final(
            operation_id,
            fingerprint,
            state,
            resuming=False,
        )
