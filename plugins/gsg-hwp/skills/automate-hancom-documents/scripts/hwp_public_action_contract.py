from __future__ import annotations

# noqa: E501  # noqa: SIZE_OK — public action schemas and their opaque target store form one boundary.

import ntpath
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Annotated, Final, Literal, Protocol, cast, final, override
from uuid import uuid4

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from hwp_color_normalization import ColorInput, canonical_rgb_hex
from hwp_live_structure_contract import FastPageInspection
from hwp_live_native_action_models import NativePosition
from hwp_live_native_format_inputs import TextFormatSpec, normalize_format_name_input
from hwp_live_text_patch_contract import (
    TextPatchPlanGuard,
    TextPatchRequest,
    TextPatchTarget,
)
from hwp_live_values import ContractModel
from hwp_operation_contract import (
    HwpOperateGuards,
    HwpOperateInputs,
    HwpOperateTarget,
    OperationInputValue,
    OperationResult,
    WorkflowTargetCandidate,
)


_MAX_INSPECTED_TARGETS: Final = 256


def _document_path_key(document_path: str | None) -> str | None:
    if not document_path:
        return None
    return ntpath.normcase(ntpath.normpath(document_path))


def _same_document_path(first: str | None, second: str | None) -> bool:
    return _document_path_key(first) == _document_path_key(second)


def _non_empty_expected_text(value: object) -> object:
    if value == "":
        raise PydanticCustomError(
            "empty_search_text",
            "빈 검색 문자열로는 고칠 위치를 정할 수 없습니다. "
            + "expected_text에 찾을 문자열을 1자 이상 넣으세요",
        )
    return value


type PublicImageInsertTarget = Literal["selection", "document_end"]
type PublicObjectKind = Literal["table", "picture"]
type PublicTextAlignment = Annotated[
    Literal["left", "center", "right", "justify", "inherit"],
    BeforeValidator(normalize_format_name_input),
]
type PublicStyleId = Annotated[int, Field(ge=0, le=4_095)]
# 250 x 350 은 네이티브 한계가 아니다. INSERT_PICTURE 는 ActionProtocol.cpp:391
# 에서 ParsePositiveMillimeter 로 읽고, 그 상한은 ActionProtocol.cpp:136 의
# 1,000mm 다. 그래서 A3(297x420) 같은 큰 용지에서 250x350 을 넘는 그림이 막힌다.
# 다만 여기만 올릴 수는 없다. hwp_insert_image / hwp_replace_image 는 같은 값을
# hwp_priority_recipe_contract.py:29-30 의 picture_width_mm / picture_height_mm
# (역시 250 / 350)로 그대로 넘긴다. 여기만 풀면 스키마 거부가 도구 안쪽의
# ValidationError 로 바뀔 뿐이라 상황이 더 나빠진다. 두 곳을 함께 올려야 한다.
type PublicImageWidth = Annotated[float, Field(ge=1, le=1_000)]
type PublicImageHeight = Annotated[float, Field(ge=1, le=1_000)]
# contain 은 지금까지의 동작 그대로다. 상자 안에 원본 비율로 넣고 남는 쪽에
# 여백이 생긴다. cover 는 상자를 꽉 채우고 넘치는 쪽을 한/글 자르기(그림 개체의
# SkipLeft/SkipTop/SkipRight/SkipBottom)로 감춘다. 원본 파일은 어느 쪽이든
# 읽기만 하며, 잘린 사본을 따로 만들지 않는다. 어느 쪽을 쓸지는 모델이 정한다.
type PublicImageFit = Literal["contain", "cover"]
type PublicFontName = Annotated[str, Field(min_length=1, max_length=100)]
type PublicFontSize = Annotated[float, Field(ge=1, le=96)]
type PublicTextColor = ColorInput
type PublicLineSpacing = Annotated[int, Field(ge=50, le=500)]
type PublicLetterSpacing = Annotated[int, Field(ge=-50, le=50, strict=True)]
type PublicParagraphMargin = Annotated[float, Field(ge=0, le=200)]
type PublicParagraphIndent = Annotated[float, Field(ge=-200, le=200)]
type PublicReplacementText = Annotated[str, Field(max_length=1_000_000)]
type PublicTextPatchPostSelection = Annotated[
    Literal["keep", "collapse_to_start", "collapse_to_end"],
    Field(
        description="검증 후 선택 유지 또는 시작/끝으로 접기. collapse_to_end는 이어지는 현재 위치 삽입에 사용합니다."
    ),
]
type PublicExpectedText = Annotated[
    str,
    BeforeValidator(_non_empty_expected_text),
    Field(max_length=1_000_000),
]
type PublicOperationId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description="논리 작업별 고유 ID. 같은 payload 재시도에만 재사용합니다.",
    ),
]

_NATIVE_CONTROL_KINDS: Final[Mapping[str, PublicObjectKind]] = MappingProxyType(
    {
        "tbl": "table",
        "gso": "picture",
        "pic": "picture",
        "picture": "picture",
    }
)
OBJECT_INPUT_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "inputs.assets.images": "path",
        "inputs.recipe.caption_text": "text",
        "inputs.target": "target_id",
        "inputs.target.control_instance_id": "target_id",
    }
)
SELECTION_INPUT_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "inputs.parameters": "formatting",
        "inputs.recipe.target_position": "target_position",
        "inputs.target": "target",
    }
)
DOCUMENT_INPUT_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {"inputs.layout": "layout"}
)


class PublicActionExecutor(Protocol):
    async def execute(
        self,
        intent: str,
        inputs: HwpOperateInputs,
        guards: HwpOperateGuards | None,
    ) -> OperationResult: ...

    async def inspect_page_fast(
        self,
        document_selector: str | None,
        page: int,
        include_cells: bool,
    ) -> FastPageInspection: ...


class PublicSelectionExecutor(PublicActionExecutor, Protocol):
    async def replace_selected_text(
        self,
        intent: str,
        inputs: HwpOperateInputs,
        replacement: str,
    ) -> OperationResult: ...

    async def patch_text(
        self,
        intent: str,
        inputs: HwpOperateInputs,
        request: TextPatchRequest,
    ) -> OperationResult: ...

    async def patch_text_batch(
        self,
        intent: str,
        inputs: HwpOperateInputs,
        requests: tuple[TextPatchRequest, ...],
        plan_guard: TextPatchPlanGuard | None = None,
    ) -> OperationResult: ...


class PublicTextPatchPosition(ContractModel):
    list_id: int = Field(ge=0)
    paragraph: int = Field(ge=0)
    character: int = Field(ge=0)

    def to_native(self) -> NativePosition:
        return NativePosition(self.list_id, self.paragraph, self.character)


_TEXT_PATCH_TARGET_FIELDS: Final = (
    "kind",
    "start",
    "end",
    "occurrence",
    "match_case",
    "table_instance_id",
    "cell",
)
# 실기에서 네 번 막힌 자리다. kind="find" 에는 "무엇을 찾는가" 를 담을 칸이 없다.
# 찾을 문자열은 최상위 expected_text 이고, target 은 어디를 찾을지만 말한다.
# 그래서 모델은 target.find / target.text 를 지어냈고 pydantic 은
# "Extra inputs are not permitted" 만 돌려줬다 — 무엇을 넣으라는 말이 없다.
_TEXT_PATCH_SEARCH_KEYS: Final = frozenset(
    (
        "contains",
        "find",
        "find_text",
        "old_text",
        "pattern",
        "query",
        "search",
        "search_text",
        "text",
        "value",
    )
)
_TEXT_PATCH_KEY_HINTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "address": "cell",
        "case_sensitive": "match_case",
        "cell_address": "cell",
        "cell_name": "cell",
        "character": "start 또는 end 안의 character",
        "control_instance_id": "table_instance_id",
        "index": "occurrence",
        "instance_id": "table_instance_id",
        "list_id": "start 또는 end 안의 list_id",
        "nth": "occurrence",
        "paragraph": "start 또는 end 안의 paragraph",
        "table_id": "table_instance_id",
        "target_id": "table_instance_id",
    }
)
_TEXT_PATCH_SEARCH_HINT: Final = (
    "찾을 문자열은 target이 아니라 최상위 expected_text에 넣고, "
    'target은 어디를 찾을지만 말합니다: target={"kind": "find"}. '
    '두 번째 일치를 바꾸려면 target={"kind": "find", "occurrence": 2}입니다.'
)
_TEXT_PATCH_KIND_REQUIREMENTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "current": (
            "current는 start·end·occurrence·match_case·table_instance_id·cell을 "
            "받지 않습니다. 검색해서 고치려면 kind를 find로 지정하세요."
        ),
        "range": (
            "range는 start와 end를 둘 다 받아야 하고 "
            "occurrence·match_case·table_instance_id·cell은 받지 않습니다."
        ),
        "find": (
            "find는 start·end를 받지 않고, 표 셀 안만 찾을 때는 "
            "table_instance_id와 cell을 함께 지정합니다. "
            "찾을 문자열은 최상위 expected_text입니다."
        ),
        "table_cell": (
            "table_cell은 table_instance_id와 cell을 둘 다 받아야 하고 "
            "start·end는 받지 않습니다."
        ),
    }
)
_TEXT_PATCH_KIND_DESCRIPTION: Final = (
    "어디를 고칠지 고릅니다. current는 현재 커서·현재 선택, "
    "range는 start/end 좌표, find는 최상위 expected_text를 문서에서 검색, "
    "table_cell은 table_instance_id와 cell로 지정한 표 셀입니다. "
    "find·range·table_cell은 최상위 expected_text가 반드시 필요합니다."
)


class PublicTextPatchTarget(ContractModel):
    kind: Literal["current", "range", "find", "table_cell"] = Field(
        default="current",
        description=_TEXT_PATCH_KIND_DESCRIPTION,
    )
    start: PublicTextPatchPosition | None = None
    end: PublicTextPatchPosition | None = None
    # 네이티브 text.patch의 occurrence 계약(1~20,000)을 유지한다.
    # 스캔 결과 한 개에 여러 일치 항목이 있을 수 있으므로 순번 상한과
    # SelectTextPatchMatch의 스캔 결과/문자 수 예산은 별개다.
    occurrence: int | None = Field(default=None, ge=1, le=20_000)
    match_case: bool = False
    table_instance_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="hwp_inspect_page_fast가 반환한 표 instance_id입니다.",
    )
    cell: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z]+[1-9][0-9]*$",
        description='표 셀 주소입니다. 예: "B3".',
    )

    @model_validator(mode="before")
    @classmethod
    def explain_unknown_target_field(cls, data: object) -> object:
        """Name the field that takes the value, instead of only refusing it.

        ``extra="forbid"`` already rejects these keys; this only replaces
        ``Extra inputs are not permitted`` with the field that does exist.
        Nothing new is accepted — every key that used to fail still fails.
        """
        if not isinstance(data, dict):
            return data
        mapping = cast("dict[str, object]", data)
        for key in mapping:
            if key in _TEXT_PATCH_TARGET_FIELDS:
                continue
            if key in _TEXT_PATCH_SEARCH_KEYS:
                raise PydanticCustomError(
                    "public_text_patch_target_search",
                    "target.{key}는 없는 자리입니다. {hint}",
                    {"key": key, "hint": _TEXT_PATCH_SEARCH_HINT},
                )
            hint = _TEXT_PATCH_KEY_HINTS.get(key)
            if hint is not None:
                raise PydanticCustomError(
                    "public_text_patch_target_moved",
                    "target.{key}는 없는 자리입니다. 그 값은 {hint}에 넣습니다.",
                    {"key": key, "hint": hint},
                )
            raise PydanticCustomError(
                "public_text_patch_target_unknown",
                "target.{key}는 없는 항목입니다. target이 받는 항목: {fields}.",
                {"key": key, "fields": ", ".join(_TEXT_PATCH_TARGET_FIELDS)},
            )
        return mapping

    @model_validator(mode="after")
    def validate_target_fields(self) -> PublicTextPatchTarget:
        range_fields = self.start is not None or self.end is not None
        search_fields = self.occurrence is not None or self.match_case
        cell_fields = self.table_instance_id is not None or self.cell is not None
        valid = {
            "current": not range_fields and not search_fields and not cell_fields,
            "range": self.start is not None
            and self.end is not None
            and not search_fields
            and not cell_fields,
            "find": not range_fields
            and (
                not cell_fields
                or (self.table_instance_id is not None and self.cell is not None)
            ),
            "table_cell": not range_fields
            and self.table_instance_id is not None
            and self.cell is not None,
        }[self.kind]
        if not valid:
            # 무엇이 어긋났는지 말하지 않으면 호출자는 같은 조합을 다시 보낸다.
            # kind 별로 무엇이 필요하고 무엇이 남았는지 그대로 적는다.
            raise PydanticCustomError(
                "public_text_patch_target",
                'target.kind="{kind}"에 맞지 않는 조합입니다. {requirement}',
                {
                    "kind": self.kind,
                    "requirement": _TEXT_PATCH_KIND_REQUIREMENTS[self.kind],
                },
            )
        if (
            self.kind == "range"
            and self.start is not None
            and self.end is not None
            and self.start.list_id != self.end.list_id
        ):
            raise PydanticCustomError(
                "public_text_patch_cross_control",
                "text.patch 범위는 서로 다른 한컴 컨트롤을 가로지를 수 없습니다",
            )
        return self

    def to_live(self) -> TextPatchTarget:
        return TextPatchTarget(
            kind=self.kind,
            start=None if self.start is None else self.start.to_native(),
            end=None if self.end is None else self.end.to_native(),
            occurrence=self.occurrence,
            match_case=self.match_case,
            table_instance_id=self.table_instance_id,
            cell_address=None if self.cell is None else self.cell.upper(),
        )


class PublicImageSize(ContractModel):
    width_mm: PublicImageWidth | None = None
    height_mm: PublicImageHeight | None = None
    fit: PublicImageFit = "contain"

    @model_validator(mode="after")
    def require_complete_size(self) -> PublicImageSize:
        if (self.width_mm is None) != (self.height_mm is None):
            raise PydanticCustomError(
                "public_image_size_pair",
                "width_mm와 height_mm는 함께 전달해야 합니다",
            )
        if self.fit == "cover" and self.width_mm is None:
            raise PydanticCustomError(
                "public_image_fit_needs_box",
                'fit="cover"는 width_mm와 height_mm가 있어야 합니다',
            )
        return self


class PublicTextFormattingInput(ContractModel):
    bold: bool | None = None
    font_name: PublicFontName | None = None
    font_size_pt: PublicFontSize | None = None
    text_color: PublicTextColor | None = None
    alignment: PublicTextAlignment = "inherit"
    line_spacing: PublicLineSpacing | None = None

    @model_validator(mode="after")
    def require_format(self) -> PublicTextFormattingInput:
        if (
            all(
                value is None
                for value in (
                    self.bold,
                    self.font_name,
                    self.font_size_pt,
                    self.text_color,
                    self.line_spacing,
                )
            )
            and self.alignment == "inherit"
        ):
            raise PydanticCustomError(
                "public_text_format_required",
                "적용할 글자 또는 문단 서식이 필요합니다",
            )
        return self

    def to_parameters(self) -> Mapping[str, OperationInputValue]:
        parameters: dict[str, OperationInputValue] = {}
        if self.bold is not None:
            parameters["bold"] = self.bold
        if self.font_name is not None:
            parameters["font_name"] = self.font_name
        if self.font_size_pt is not None:
            parameters["font_size_pt"] = self.font_size_pt
        if self.text_color is not None:
            parameters["text_color"] = canonical_rgb_hex(self.text_color)
        if self.alignment != "inherit":
            parameters["alignment"] = self.alignment
        if self.line_spacing is not None:
            parameters["line_spacing"] = self.line_spacing
        return parameters

    def to_live(self) -> TextFormatSpec:
        return TextFormatSpec(
            self.bold,
            self.font_name,
            self.font_size_pt,
            self.text_color,
            self.alignment,
            self.line_spacing,
        )


class PublicSelectionFormattingInput(PublicTextFormattingInput):
    """Selection formatting also supports native character spacing.

    Prepared text-patch batches do not yet capture per-script spacing for their
    inverses, so their existing input model deliberately stays unchanged.
    """

    letter_spacing_percent: PublicLetterSpacing | None = None
    left_margin_mm: PublicParagraphMargin | None = None
    indentation_mm: PublicParagraphIndent | None = None
    keep_with_next: bool | None = None
    page_break_before: bool | None = None

    @model_validator(mode="after")
    def require_format(self) -> PublicSelectionFormattingInput:
        if all(
            value is None
            for value in (
                self.letter_spacing_percent,
                self.left_margin_mm,
                self.indentation_mm,
                self.keep_with_next,
                self.page_break_before,
            )
        ):
            _ = PublicTextFormattingInput.model_validate(
                self.model_dump(
                    exclude={
                        "letter_spacing_percent",
                        "left_margin_mm",
                        "indentation_mm",
                        "keep_with_next",
                        "page_break_before",
                    }
                )
            )
        return self

    @override
    def to_parameters(self) -> Mapping[str, OperationInputValue]:
        parameters = dict(super().to_parameters())
        if self.letter_spacing_percent is not None:
            parameters["letter_spacing_percent"] = self.letter_spacing_percent
        if self.left_margin_mm is not None:
            parameters["left_margin_mm"] = self.left_margin_mm
        if self.indentation_mm is not None:
            parameters["indentation_mm"] = self.indentation_mm
        if self.keep_with_next is not None:
            parameters["keep_with_next"] = self.keep_with_next
        if self.page_break_before is not None:
            parameters["page_break_before"] = self.page_break_before
        return parameters

    @override
    def to_live(self) -> TextFormatSpec:
        return replace(
            super().to_live(),
            letter_spacing_percent=self.letter_spacing_percent,
            left_margin_mm=self.left_margin_mm,
            indentation_mm=self.indentation_mm,
            keep_with_next=self.keep_with_next,
            page_break_before=self.page_break_before,
        )


class PublicTextPatchItem(ContractModel):
    target: PublicTextPatchTarget = Field(default_factory=PublicTextPatchTarget)
    expected_text: str | None = Field(
        default=None,
        max_length=1_000_000,
        description=(
            "문서 구조가 반환한 리터럴 텍스트입니다. 화면에 렌더링된 자동 번호·"
            "글머리표(예: 가), ◦)는 제외합니다. 실제 리터럴 문자인 경우에는 그대로 "
            "포함하며 자동으로 제거하지 않습니다."
        ),
    )
    replacement: PublicReplacementText

    @model_validator(mode="after")
    def validate_empty_cell_guard(self) -> PublicTextPatchItem:
        if self.expected_text == "" and self.target.kind != "table_cell":
            _ = _non_empty_expected_text(self.expected_text)
        return self

    def to_live(self) -> TextPatchRequest:
        return TextPatchRequest(
            target=self.target.to_live(),
            expected_text=self.expected_text,
            replacement=self.replacement,
        )


type PublicTextPatchBatch = Annotated[
    tuple[PublicTextPatchItem, ...], Field(min_length=1, max_length=1_000)
]


@dataclass(frozen=True, slots=True)
class CanonicalPublicObjectTarget:
    document_path: str | None
    target: HwpOperateTarget


@dataclass(frozen=True, slots=True)
class PublicObjectTargetReference:
    document_path: str | None
    kind: PublicObjectKind
    page: int
    index: int
    control_instance_id: str | None = None
    document_selector: str | None = None


@final
class PublicObjectTargetStore:
    __slots__ = ("_inspected_document", "_inspected_targets", "_targets")

    def __init__(self) -> None:
        self._targets: dict[str, PublicObjectTargetReference] = {}
        self._inspected_targets: dict[str, PublicObjectTargetReference] = {}
        self._inspected_document: tuple[int, str | None] | None = None

    def _clear_targets(self) -> None:
        self._targets.clear()
        self._inspected_targets.clear()

    def _prepare_candidate_document(self, document_path: str | None) -> None:
        inspected_document = self._inspected_document
        candidate_path = _document_path_key(document_path)
        if (
            inspected_document is not None
            and candidate_path is not None
            and inspected_document[1] != candidate_path
        ):
            self._clear_targets()
            self._inspected_document = None

    def _prepare_inspection(self, inspection: FastPageInspection) -> None:
        document = (
            inspection.document_id,
            _document_path_key(inspection.full_name),
        )
        changed_document = (
            self._inspected_document is not None
            and self._inspected_document != document
        )
        if self._inspected_document is None and self._targets:
            candidate_paths = {
                _document_path_key(target.document_path)
                for target in self._targets.values()
            }
            changed_document = candidate_paths != {document[1]}
        if changed_document:
            self._clear_targets()
        self._inspected_document = document

    def _remember_inspected(
        self,
        instance_id: str,
        target: PublicObjectTargetReference,
    ) -> None:
        _ = self._inspected_targets.pop(instance_id, None)
        self._inspected_targets[instance_id] = target
        while len(self._inspected_targets) > _MAX_INSPECTED_TARGETS:
            oldest = next(iter(self._inspected_targets))
            del self._inspected_targets[oldest]

    def _reference(self, target_id: str) -> PublicObjectTargetReference | None:
        return self._targets.get(target_id) or self._inspected_targets.get(target_id)

    def resolve_picture(
        self,
        target_id: str | None,
        document_path: str | None,
    ) -> CanonicalPublicObjectTarget | None:
        if target_id is None:
            return CanonicalPublicObjectTarget(
                document_path,
                HwpOperateTarget(
                    kind="picture",
                    binding="selection",
                    match_policy="return_candidates",
                ),
            )
        reference = self._reference(target_id)
        if reference is None:
            if target_id.startswith("hwp-target-"):
                return None
            return CanonicalPublicObjectTarget(
                document_path,
                HwpOperateTarget(
                    kind="picture",
                    binding="active",
                    match_policy="return_candidates",
                    control_instance_id=target_id,
                ),
            )
        if reference.kind != "picture":
            return None
        if (
            document_path is not None
            and reference.document_path is not None
            and not _same_document_path(reference.document_path, document_path)
            and not _same_document_path(reference.document_selector, document_path)
        ):
            return None
        return CanonicalPublicObjectTarget(
            reference.document_path,
            HwpOperateTarget(
                kind="picture",
                binding="active",
                page_hint=reference.page,
                table_index=reference.index,
                match_policy="return_candidates",
                control_instance_id=reference.control_instance_id,
            ),
        )

    def resolve_control(
        self,
        target_id: str | None,
        document_path: str | None,
    ) -> CanonicalPublicObjectTarget | None:
        if target_id is None:
            return CanonicalPublicObjectTarget(
                document_path,
                HwpOperateTarget(
                    kind="control",
                    binding="selection",
                    match_policy="return_candidates",
                ),
            )
        reference = self._reference(target_id)
        if reference is None:
            if target_id.startswith("hwp-target-"):
                return None
            return CanonicalPublicObjectTarget(
                document_path,
                HwpOperateTarget(
                    kind="control",
                    binding="active",
                    match_policy="return_candidates",
                    control_instance_id=target_id,
                ),
            )
        if (
            document_path is not None
            and reference.document_path is not None
            and not _same_document_path(reference.document_path, document_path)
        ):
            return None
        return CanonicalPublicObjectTarget(
            reference.document_path,
            HwpOperateTarget(
                kind="control",
                binding="active",
                page_hint=reference.page,
                table_index=reference.index,
                match_policy="return_candidates",
                control_instance_id=reference.control_instance_id,
            ),
        )

    def remember(
        self,
        candidates: tuple[WorkflowTargetCandidate, ...],
        document_path: str | None,
    ) -> tuple[str, ...]:
        if not candidates:
            return ()
        self._prepare_candidate_document(document_path)
        self._targets.clear()
        target_ids: list[str] = []
        for candidate in candidates[:3]:
            if candidate.kind == "picture":
                index = candidate.picture_index
            else:
                index = candidate.table_index
            assert index is not None
            target_id = f"hwp-target-{uuid4().hex}"
            self._targets[target_id] = PublicObjectTargetReference(
                document_path,
                candidate.kind,
                candidate.page,
                index,
            )
            target_ids.append(target_id)
        return tuple(target_ids)

    def remember_inspection(
        self,
        inspection: FastPageInspection,
        document_selector: str | None = None,
    ) -> None:
        self._prepare_inspection(inspection)
        counts: dict[PublicObjectKind, int] = {"table": 0, "picture": 0}
        remembered = 0
        for control in inspection.controls:
            kind = _NATIVE_CONTROL_KINDS.get(control.control_type)
            if kind is None:
                continue
            remembered += 1
            if remembered > _MAX_INSPECTED_TARGETS:
                break
            counts[kind] += 1
            self._remember_inspected(
                control.instance_id,
                PublicObjectTargetReference(
                    inspection.full_name,
                    kind,
                    inspection.page,
                    counts[kind],
                    control.instance_id,
                    document_selector,
                ),
            )

    def clear(self) -> None:
        self._clear_targets()
        self._inspected_document = None
