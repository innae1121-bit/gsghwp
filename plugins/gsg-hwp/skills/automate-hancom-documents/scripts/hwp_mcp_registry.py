from __future__ import annotations

# noqa: E501  # noqa: SIZE_OK — this module is the declarative MCP tool catalog.

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import environ
from types import MappingProxyType
from typing import Final, Literal

from hwp_mcp_search import rank_tool_specs
from hwp_operation_contract import HwpWorkflowId
from hwp_operation_descriptor import (
    OperationVerificationMode,
    descriptor_workflows_for_tool,
    operation_descriptor,
)
from hwp_public_action_metadata import (
    HWP_ADD_CAPTION_DESCRIPTION,
    HWP_APPEND_EXCEL_TABLE_DESCRIPTION,
    HWP_APPEND_LAYOUT_DESCRIPTION,
    HWP_APPEND_REPORT_DESCRIPTION,
    HWP_APPLY_STYLE_DESCRIPTION,
    HWP_DELETE_CONTROL_DESCRIPTION,
    HWP_DELETE_PAGE_DESCRIPTION,
    HWP_EDIT_PICTURE_DESCRIPTION,
    HWP_COPY_PICTURE_DESCRIPTION,
    HWP_FILL_TABLE_DESCRIPTION,
    HWP_FORMAT_TEXT_DESCRIPTION,
    HWP_PATCH_TEXT_BATCH_DESCRIPTION,
    HWP_PATCH_TEXT_DESCRIPTION,
    HWP_PATCH_TEXT_FROM_XLSX_DESCRIPTION,
    HWP_INSERT_CURRENT_FORMAT_CONTENT_DESCRIPTION,
    HWP_INSERT_IMAGE_DESCRIPTION,
    HWP_INSERT_LAYOUT_DESCRIPTION,
    HWP_REDO_DESCRIPTION,
    HWP_REPLACE_IMAGE_DESCRIPTION,
    HWP_REPLACE_SELECTED_TEXT_DESCRIPTION,
    HWP_RESTRUCTURE_TABLE_DESCRIPTION,
    HWP_SAVE_DESCRIPTION,
    HWP_SAVE_REOPEN_VERIFY_DESCRIPTION,
    HWP_UNDO_DESCRIPTION,
    LAYOUT_MINIMUM_SHAPE,
)
from hwp_public_table_metadata import (
    HWP_BUILD_TABLE_SERIES_DESCRIPTION,
    HWP_EXPAND_AND_FILL_TABLE_DESCRIPTION,
    HWP_FILL_TABLE_IMAGES_DESCRIPTION,
    HWP_FORMAT_TABLE_DESCRIPTION,
    HWP_SET_TABLE_SIZE_FIXED_DESCRIPTION,
    HWP_MERGE_TABLE_CELLS_DESCRIPTION,
    HWP_REPEAT_TABLE_TEMPLATE_DESCRIPTION,
    HWP_SPLIT_TABLE_CELL_DESCRIPTION,
    HWP_SYNC_VISIBILITY_ANALYSIS_TABLES_DESCRIPTION,
)


type McpProfile = Literal["production", "qa"]
type ExecutionPath = Literal[
    "native_required",
    "hybrid_python_native_preferred",
    "hybrid_event_com",
    "rot_com",
    "com_dispatch",
    "python_catalog",
    "mcp_forward",
    "win32_ui",
]
type CapabilityCategory = Literal[
    "catalog",
    "diagnostic",
    "document",
    "image",
    "layout",
    "office",
    "official_api",
    "state",
    "structure",
    "style",
    "table",
    "text",
    "verification",
]
type CapabilityOperation = Literal["read", "write", "session"]
type ToolEffect = Literal["read", "document", "file", "artifact", "session"]
type ToolHandlerSource = Literal["bindings", "catalog", "server", "gateway", "proxy"]
type ToolExposure = Literal["worker", "proxy"]
type ToolBindingOwner = Literal[
    "operation",
    "document_wrappers",
    "qa",
    "public_tools",
    "public_table_tools",
    "public_visibility_tools",
    "public_table_edit_tools",
    "public_object_tools",
    "public_restructure_tools",
    "public_inspection_tools",
    "public_graph_tools",
    "public_selection_tools",
    "public_xlsx_tools",
    "public_document_tools",
    "public_live_edit_tools",
    "reference_image_tools",
    "pageplan_tools",
    "custom_action_tools",
]
type ToolVerificationDelegate = Literal["forwarded_tool", "graph_readback"]


_ALL_PROFILES: Final = frozenset[McpProfile](("production", "qa"))
_QA_ONLY: Final = frozenset[McpProfile](("qa",))
_PUBLIC_WRITE_CONTINUATION_GUIDANCE: Final = (
    " 성공 시 affected_pages·state_token·selected_target_id·affected_target_ids로 "
    "재조회 없이 계속하고, 응답 손실·동일 payload 재시도는 같은 operation_id를 쓰세요."
)
_LAYOUT_BULK_GUIDANCE: Final = (
    " 여러 편집은 layout.blocks에 묶어 한 번의 bulk 호출로 실행하세요."
)


class McpToolSpecError(RuntimeError):
    tool_name: str
    reason: str

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"{tool_name}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    name: str
    description: str
    category: CapabilityCategory
    operation: CapabilityOperation
    execution_path: ExecutionPath
    handler_source: ToolHandlerSource = "bindings"
    binding_owner: ToolBindingOwner | None = None
    exposure: ToolExposure = "worker"
    verification_delegate: ToolVerificationDelegate | None = None
    effect_override: ToolEffect | None = None
    requires_session: bool = True
    requires_state_token: bool = False
    profiles: frozenset[McpProfile] = _QA_ONLY
    workflow: HwpWorkflowId | None = None
    workflows: tuple[HwpWorkflowId, ...] = ()

    def __post_init__(self) -> None:
        effect = self.effect
        allowed_effects: Mapping[CapabilityOperation, frozenset[ToolEffect]] = {
            "read": frozenset(("read", "artifact")),
            "write": frozenset(("document", "file", "artifact", "session")),
            "session": frozenset(("session",)),
        }
        if effect not in allowed_effects[self.operation]:
            raise McpToolSpecError(
                self.name,
                f"effect {effect!r} is incompatible with operation {self.operation!r}",
            )
        descriptor_workflows = descriptor_workflows_for_tool(self.name)
        if (
            self.workflow is not None
            and descriptor_workflows
            and self.workflow not in descriptor_workflows
        ):
            raise McpToolSpecError(
                self.name, "workflow does not match its operation descriptor"
            )
        workflows = (
            descriptor_workflows
            if descriptor_workflows
            else (() if self.workflow is None else (self.workflow,))
        )
        object.__setattr__(self, "workflows", workflows)
        if self.workflow is None and workflows:
            object.__setattr__(self, "workflow", workflows[0])
        if (self.exposure == "proxy") != (self.handler_source == "proxy"):
            raise McpToolSpecError(
                self.name, "proxy exposure and handler source must match"
            )
        if self.handler_source != "bindings" and self.binding_owner is not None:
            raise McpToolSpecError(
                self.name, "binding owner requires the bindings handler source"
            )
        if (
            "production" in self.profiles
            and self.may_mutate
            and not self.has_readback_verifier
        ):
            raise McpToolSpecError(
                self.name, "mutating production tool has no verifier"
            )
        if (
            "production" in self.profiles
            and self.operation == "write"
            and self.handler_source == "bindings"
            # artifact 쓰기는 문서를 바꾸지 않는다. affected_pages·state_token을
            # 약속하는 이 문장을 붙이면 도구 설명이 거짓말이 된다.
            and self.effect != "artifact"
        ):
            description = self.description + _PUBLIC_WRITE_CONTINUATION_GUIDANCE
            if self.name in {"hwp_append_layout", "hwp_insert_layout"}:
                description += _LAYOUT_BULK_GUIDANCE
            object.__setattr__(self, "description", description)

    @property
    def effect(self) -> ToolEffect:
        if self.effect_override is not None:
            return self.effect_override
        if self.operation == "read":
            return "read"
        if self.operation == "write":
            return "document"
        if self.operation == "session":
            return "session"
        raise McpToolSpecError(
            self.name, f"has unsupported operation {self.operation!r}"
        )

    @property
    def may_mutate(self) -> bool:
        return self.effect in {"document", "file"}

    @property
    def verification_modes(self) -> tuple[OperationVerificationMode, ...]:
        modes: list[OperationVerificationMode] = []
        for workflow_id in self.workflows:
            descriptor = operation_descriptor(workflow_id)
            if descriptor is None:
                continue
            for mode in descriptor.verification_modes:
                if mode not in modes:
                    modes.append(mode)
        return tuple(modes)

    @property
    def has_readback_verifier(self) -> bool:
        return bool(self.verification_modes) or self.verification_delegate is not None


class McpProfileError(RuntimeError):
    value: str

    def __init__(self, value: str) -> None:
        super().__init__(f"지원하지 않는 HANCOM_MCP_PROFILE입니다: {value}")
        self.value = value


class McpToolSearchError(RuntimeError):
    field: str

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


MCP_TOOL_SPECS: Final = (
    McpToolSpec(
        "hwp_list_open_documents",
        "새 HWP를 띄우지 않고 ROT의 열린 문서를 나열합니다.",
        "document",
        "read",
        "rot_com",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_open_document",
        "열린 한컴 프로세스를 전체 경로 또는 selector로 지정해 기존 문서를 새 탭이나 새 창에 열고, 기본적으로 원래 활성 문서를 복원한 뒤 정확한 document_id를 다시 읽습니다. restore_reference=false이면 새 문서를 활성 상태로 유지합니다.",
        "document",
        "session",
        "rot_com",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_connect",
        "선택자를 생략하면 활성 HWP에 연결하고, 이미 같은 문서에 연결됐으면 기존 session_id를 그대로 반환합니다.",
        "document",
        "session",
        "rot_com",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_inspect",
        "connect나 session_id 선행 작업 없이 현재 쪽·마지막 한컴 커서·선택 텍스트·선택 셀 블록·선택 표와 글자·문단·용지 모양을 읽습니다. active_target은 현재 또는 포커스 이동 직전 마지막 한컴 위치를 뜻합니다.",
        "state",
        "read",
        "com_dispatch",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_inspect_page_fast",
        'connect나 session_id 없이 표·개체의 크기와 표 앵커 문단 모양을 C++/ATL로 즉시 읽습니다. 반환된 instance_id는 hwp_add_caption·hwp_replace_image·hwp_fill_table의 직접 target_id로, target 파라미터가 있는 표 도구에서는 target: {"target_id": instance_id}로 재사용하며, include_cells=true일 때 셀·병합·내용과 각 셀의 실제 너비·높이까지 조회합니다.',
        "structure",
        "read",
        "native_required",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_find_tables",
        "42쪽 문서의 사진 표 탐색에 41회 쪽별 호출이 걸린 측정 경로를 대신해, 문서 전체 표를 한 호출로 읽고 쪽·표 instance_id·행/열·그림 수와 그림 instance_id·셀 주소를 반환합니다. min_pictures는 호출자가 정한 최솟값만 적용하며 읽지 못한 쪽과 검사 오류도 그대로 반환합니다.",
        "structure",
        "read",
        "native_required",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_inspect_structure",
        "connect나 session_id 없이 지정 쪽의 문단·개체 크기·표·셀·병합·그림·캡션·쪽 범위와 상태 토큰을 읽습니다.",
        "structure",
        "read",
        "native_required",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_inspect_patch_plan",
        "최대 32쪽의 상세 구조를 한 호출로 읽어 문단 literal range와 표 셀 patch target만 64KiB 기본 한도 안에 반환합니다. 완전한 결과가 한도를 넘으면 내용을 자르지 않고 필요한 bytes/items와 결정적 쪽 분할을 반환합니다.",
        "structure",
        "read",
        "native_required",
        binding_owner="public_inspection_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_graph_manifest",
        "대형 문서 전수 그래프 추출·감사 전용입니다. 일반 편집·조회에는 쓰지 마십시오. 현재 문서의 완전한 불변 그래프 세대·버전·프로필·레코드 수와 content-addressed 원본 프레임 인덱스를 32KB 이하 manifest로 반환합니다. 그래프 데이터는 축약하거나 버리지 않습니다.",
        "structure",
        "read",
        "native_required",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_query_graph",
        "대형 문서 전수 그래프 추출·감사 전용입니다. 일반 편집·조회에는 쓰지 마십시오. 현재 문서 그래프를 레코드 종류로 조회하고 명시적 continuation과 content-addressed artifact receipt로 모든 결과를 손실 없이 반환합니다.",
        "structure",
        "read",
        "native_required",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_graph_node",
        "대형 문서 전수 그래프 추출·감사 전용입니다. 일반 편집·조회에는 쓰지 마십시오. 불변 그래프 버전에서 공개 NodeId 하나를 조회합니다. 존재하지 않거나 공개되지 않은 항목은 NotExposed로 명시하며 내부 네이티브 구현 이름은 노출하지 않습니다.",
        "structure",
        "read",
        "native_required",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_graph_property",
        "대형 문서 전수 그래프 추출·감사 전용입니다. 일반 편집·조회에는 쓰지 마십시오. 공개 NodeId의 속성 관측값을 조회합니다. NotExposed·NotApplicable·ReadFailed 등 원래 unavailable 이유를 보존합니다.",
        "structure",
        "read",
        "native_required",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_graph_asset",
        "대형 문서 전수 그래프 추출·감사 전용입니다. 일반 편집·조회에는 쓰지 마십시오. 그래프 asset 레코드를 불변 버전과 digest에 결속된 artifact receipt로 조회합니다. 대형 바이트는 inline하지 않습니다.",
        "structure",
        "read",
        "native_required",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_fetch_graph_artifact",
        "대형 문서 전수 그래프 추출·감사 전용입니다. 일반 편집·조회에는 쓰지 마십시오. 그래프 응답의 content-addressed artifact URI를 digest 검증 후 최대 32KB씩 읽고 다음 byte offset을 반환합니다.",
        "structure",
        "read",
        "python_catalog",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_validate_graph_patch",
        "완전히 시링된 그래프 패치를 검증하고 작은 receipt와 artifact URI를 반환합니다. 부분 스트림과 임의 HAction/COM은 거부합니다.",
        "structure",
        "read",
        "python_catalog",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_diff_graph_patch",
        "검증된 그래프 패치의 forward/inverse digest와 영향 노드를 작은 receipt와 artifact URI로 반환합니다.",
        "structure",
        "read",
        "python_catalog",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_invert_graph_patch",
        "검증된 그래프 패치의 역패치를 계산하고 작은 receipt와 artifact URI를 반환합니다.",
        "structure",
        "read",
        "python_catalog",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_graph_patch_history",
        "검증된 그래프 패치 적용 이력을 작은 receipt와 artifact URI로 반환합니다.",
        "structure",
        "read",
        "python_catalog",
        binding_owner="public_graph_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_apply_graph_patch",
        "검증된 그래프 패치를 원자 적용합니다. stale/wrong-document/읽기 전용 속성은 거부하고, 재시도는 같은 operation_id로 idempotent receipt를 반환합니다.",
        "structure",
        "write",
        "python_catalog",
        binding_owner="public_graph_tools",
        verification_delegate="graph_readback",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_reconcile_graph_patch",
        "원자 적용 복원이 실패한 그래프를 reconcile-required로 막고 재시도를 차단합니다.",
        "structure",
        "write",
        "python_catalog",
        binding_owner="public_graph_tools",
        verification_delegate="graph_readback",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_prepare_image_crops",
        "모델이 의미상 선택한 대략 영역을 전체 이미지 분석과 연결 콘텐츠 경계로 정밀 보정한 뒤 개별 PNG로 자릅니다. 요청·보정 좌표, 신뢰도, 검토 여부, 분석·크롭 오버레이와 해시를 반환하며 원본은 변경하지 않습니다.",
        "image",
        "read",
        "python_catalog",
        binding_owner="reference_image_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_analyze_reference_image",
        "원본 이미지를 변경하지 않고 분석해 100KB 이하 compact 수치 증거와 실행 모드 추천을 반환합니다. 기본 시간 예산에서는 미완 결과가 가능하며 analysis_complete·stop_reason·omissions로 표시합니다. 완전한 결과에는 검증 좌표와 reference_layout 초안이 포함될 수 있습니다. 대용량 검출 자료와 이미지 산출물은 로컬 파일에 둡니다.",
        "image",
        "read",
        "python_catalog",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_reference_image_analysis_section",
        "hwp_analyze_reference_image의 analysis_id로 객체·텍스트·선·breakpoint·타일·산출물 중 요청한 section만 offset/limit 범위로 읽습니다. 이미지 바이트는 반환하지 않습니다.",
        "image",
        "read",
        "python_catalog",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_compile_page_plan",
        "G02 PagePlan과 SourceRegistry를 검증하고, 선택적 생성·참조 이미지에 대해 G01 좌표 마스킹·참조 분석·결정론적 스냅·명시적 슬롯 매핑을 수행해 G03 정본 계획을 반환합니다. HWP 문서는 변경하지 않습니다.",
        "layout",
        "read",
        "python_catalog",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
        binding_owner="pageplan_tools",
    ),
    McpToolSpec(
        "hwp_apply_page_plan",
        "G03에서 컴파일된 candidate A 한 쪽 PagePlan만 원본 SourceRegistry로 문서 끝의 새 한 쪽에 적용합니다. 원본 SHA-256을 변이 직전에 다시 확인하고, 한 번의 원자 네이티브 배치·완전 롤백·한 단계 MCP 되돌리기·삽입 바이트와 원본 비율 검증·최종 렌더 1회를 증거로 반환합니다. candidate B와 명시적 쪽 나눔은 MULTI_PAGE_REQUIRES_G05로 거부합니다.",
        "layout",
        "write",
        "native_required",
        binding_owner="pageplan_tools",
        verification_delegate="forwarded_tool",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_preflight_layout",
        LAYOUT_MINIMUM_SHAPE
        + "현재 문서의 실제 용지·여백과 LayoutPlan의 문단 간격·표 행 높이·그림·캡션을 비교해 쓰기 전에 쪽 넘침, 문제 block, 안전한 조정 항목을 반환하며 문서는 변경하지 않습니다.",
        "layout",
        "read",
        "com_dispatch",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_watch_state",
        "revision 이후 문서 구조·커서·선택·창·대화상자 변경점을 기다려 반환합니다.",
        "state",
        "read",
        "hybrid_event_com",
    ),
    McpToolSpec(
        "hwp_list_window_states",
        "ROT 문서가 없어도 현재 보이는 한/글 창과 시작 오류 팝업의 핸들·제목·클래스·소유 관계를 나열합니다.",
        "diagnostic",
        "read",
        "win32_ui",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_inspect_dialog",
        "지정한 한/글 팝업의 모든 현재 자식 컨트롤과 실제 control_id, 기본 버튼, 가시성·활성 상태를 구조적으로 반환합니다. 버튼 이름이나 개수를 가정하지 않습니다.",
        "diagnostic",
        "read",
        "win32_ui",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_invoke_dialog_action",
        "먼저 읽은 한/글 팝업에서 모델이 선택한 임의 control_id를 즉시 재검증한 뒤 Win32 버튼, WPF InvokePattern, 또는 X 전용 창 닫기 액션 하나만 실행하고 전후 구조를 반환합니다.",
        "diagnostic",
        "write",
        "win32_ui",
        effect_override="session",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_inspect_window_state",
        "지정한 한/글 창의 모달·오류 대화상자 구조를 즉시 반환합니다.",
        "diagnostic",
        "read",
        "win32_ui",
        requires_session=False,
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_dismiss_dialogs",
        "기록한 한/글 모달 대화상자를 닫고 전후 구조를 반환합니다.",
        "diagnostic",
        "write",
        "win32_ui",
        effect_override="session",
        requires_session=False,
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_list_styles",
        "connect나 session_id 없이 현재 문서에 정의된 문단 스타일 이름과 ID를 읽고, observed_usage에 이 문서가 실제로 쓰는 체계를 함께 싣습니다. 스타일별 사용 문단 수, 문서가 처음 쓴 순서, 관측한 문단 앞머리 원문 표본이므로 목차 단계와 글머리 규칙을 문서 자신에게서 확인할 수 있습니다.",
        "style",
        "read",
        "com_dispatch",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_operate",
        "현재 열린 저장 가능한 HWP에서 사용자의 요청을 가장 구체적인 인증 작업으로 즉시 수행합니다. 선행 list/connect/inspect/catalog 호출 없이 한 호출 안에서 연결, 대상 확정, native 실행, 검증을 처리합니다. needs_input이거나 실제 대상 후보가 여러 개일 때만 질문하며 공식 API 이름, 셀 주소, 컨트롤 ID를 추측하지 않습니다.",
        "official_api",
        "write",
        "native_required",
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_fill_table",
        HWP_FILL_TABLE_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_expand_and_fill_table",
        HWP_EXPAND_AND_FILL_TABLE_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_repeat_table_template",
        HWP_REPEAT_TABLE_TEMPLATE_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_insert_image",
        HWP_INSERT_IMAGE_DESCRIPTION,
        "image",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_replace_image",
        HWP_REPLACE_IMAGE_DESCRIPTION,
        "image",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_edit_picture",
        HWP_EDIT_PICTURE_DESCRIPTION,
        "image",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_copy_picture",
        HWP_COPY_PICTURE_DESCRIPTION,
        "image",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_restructure_table",
        HWP_RESTRUCTURE_TABLE_DESCRIPTION,
        "table",
        "write",
        "native_required",
        binding_owner="public_restructure_tools",
        verification_delegate="forwarded_tool",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_build_table_series",
        HWP_BUILD_TABLE_SERIES_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_sync_visibility_analysis_tables",
        HWP_SYNC_VISIBILITY_ANALYSIS_TABLES_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_fill_table_images",
        HWP_FILL_TABLE_IMAGES_DESCRIPTION,
        "image",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_add_caption",
        HWP_ADD_CAPTION_DESCRIPTION,
        "text",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_copy_style",
        "원본 위치의 글자·문단 스타일을 대상 위치에 복사합니다. style.copy 인증 recipe로 실행합니다.",
        "style",
        "write",
        "native_required",
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_apply_style",
        HWP_APPLY_STYLE_DESCRIPTION,
        "style",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_format_text",
        HWP_FORMAT_TEXT_DESCRIPTION,
        "text",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_patch_text",
        HWP_PATCH_TEXT_DESCRIPTION,
        "text",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_patch_text_batch",
        HWP_PATCH_TEXT_BATCH_DESCRIPTION,
        "text",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_read_workbook_rows",
        "세션이나 한/글 문서 변경 없이 1~32개 XLSX 파일 또는 폴더의 바로 아래 XLSX를 읽어 key가 있는 행의 원문 값·물리 열·머리글/단위/주석 근거를 반환합니다. source_paths는 정확 경로를 우선하며, 누락된 마지막 구성요소가 공백 제거·대소문자 무시 기준으로 유일한 형제일 때만 교정합니다. sheet_queries는 실제 시트명의 공백 정규화·대소문자 무시 부분 문자열이고, 일치가 없으면 실제 시트 목록과 빈 matched_sheets를 반환합니다.",
        "office",
        "read",
        "python_catalog",
        binding_owner="public_xlsx_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_patch_text_from_xlsx",
        HWP_PATCH_TEXT_FROM_XLSX_DESCRIPTION,
        "office",
        "write",
        "native_required",
        binding_owner="public_xlsx_tools",
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_replace_selected_text",
        HWP_REPLACE_SELECTED_TEXT_DESCRIPTION,
        "text",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_format_table",
        HWP_FORMAT_TABLE_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_set_table_size_fixed",
        HWP_SET_TABLE_SIZE_FIXED_DESCRIPTION,
        "table",
        "write",
        "native_required",
        binding_owner="public_table_edit_tools",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_merge_table_cells",
        HWP_MERGE_TABLE_CELLS_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_split_table_cell",
        HWP_SPLIT_TABLE_CELL_DESCRIPTION,
        "table",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_delete_page",
        HWP_DELETE_PAGE_DESCRIPTION,
        "document",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_delete_control",
        HWP_DELETE_CONTROL_DESCRIPTION,
        "structure",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_undo",
        HWP_UNDO_DESCRIPTION,
        "document",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_redo",
        HWP_REDO_DESCRIPTION,
        "document",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_append_layout",
        HWP_APPEND_LAYOUT_DESCRIPTION,
        "layout",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_insert_layout",
        HWP_INSERT_LAYOUT_DESCRIPTION,
        "layout",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_insert_current_format_content",
        HWP_INSERT_CURRENT_FORMAT_CONTENT_DESCRIPTION,
        "layout",
        "write",
        "hybrid_python_native_preferred",
        profiles=_ALL_PROFILES,
        workflow="document.insert_layout",
    ),
    McpToolSpec(
        "hwp_append_report",
        HWP_APPEND_REPORT_DESCRIPTION,
        "layout",
        "write",
        "native_required",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_append_excel_table",
        HWP_APPEND_EXCEL_TABLE_DESCRIPTION,
        "office",
        "write",
        "hybrid_python_native_preferred",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_save",
        HWP_SAVE_DESCRIPTION,
        "document",
        "write",
        "native_required",
        effect_override="file",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_save_reopen_verify",
        HWP_SAVE_REOPEN_VERIFY_DESCRIPTION,
        "verification",
        "write",
        "native_required",
        effect_override="file",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_run_official_api_batch",
        "현재 세션 문서에서 공식 API 인덱스 범위를 C++/ATL로 실행합니다.",
        "official_api",
        "write",
        "native_required",
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_probe_official_api_batch",
        "지정한 한컴 창에서 공식 API 인덱스 범위를 C++/ATL로 진단합니다.",
        "official_api",
        "write",
        "native_required",
        requires_session=False,
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_probe_official_api_payload",
        "검증 하네스의 HCV1 요청을 지정한 한컴 창의 C++/ATL 진단기에 전달합니다.",
        "official_api",
        "write",
        "native_required",
        requires_session=False,
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_replace_selection",
        "예상 좌표와 원문을 검증해 선택 텍스트를 C++/ATL 내부에서 교체합니다.",
        "text",
        "write",
        "native_required",
    ),
    McpToolSpec(
        "hwp_apply_layout",
        "현재 커서 또는 plan.target=document_end에 문단·표·캡션·원본 비율 그림을 한 번의 C++/ATL 배치로 삽입합니다.",
        "layout",
        "write",
        "native_required",
    ),
    McpToolSpec(
        "hwp_update_table_cells",
        "표 참조값·상태 토큰·셀 원문을 검증한 뒤 기존 셀을 채웁니다.",
        "table",
        "write",
        "native_required",
        requires_state_token=True,
    ),
    McpToolSpec(
        "hwp_insert_table_images",
        "현재 열린 HWP의 지정 표 셀에 주소별 그림을 삽입합니다.",
        "image",
        "write",
        "native_required",
        profiles=_QA_ONLY,
    ),
    McpToolSpec(
        "hwp_import_office_table",
        "PPTX 또는 XLSX 표 값을 읽어 기존 한컴 표를 네이티브 배치로 채웁니다.",
        "office",
        "write",
        "hybrid_python_native_preferred",
        requires_state_token=True,
    ),
    McpToolSpec(
        "hwp_propagate_table_cells",
        "원본 표 셀을 여러 반복 표의 대응 셀에 네이티브 배치로 채웁니다.",
        "table",
        "write",
        "native_required",
        requires_state_token=True,
    ),
    McpToolSpec(
        "hwp_insert_folder_images",
        "파일명 키로 그림을 찾아 여러 표 셀에 원본 비율로 넣습니다.",
        "image",
        "write",
        "native_required",
        requires_state_token=True,
    ),
    McpToolSpec(
        "hwp_rebuild_document",
        "원본을 덮어쓰지 않고 HWPX 구조 왕복 후 새 HWP를 만들고 검증합니다.",
        "document",
        "write",
        "com_dispatch",
        requires_session=False,
    ),
    McpToolSpec(
        "hwp_render_page",
        "문서 상태를 유지한 채 CreatePageImage로 현재 또는 지정 쪽 PNG를 만듭니다. 열린 HWP 문서의 내용·배치·표현을 분석하거나 결과를 검증할 때 이 렌더를 사용합니다.",
        "verification",
        "read",
        "com_dispatch",
        binding_owner="public_inspection_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_ground_document",
        "한 번의 읽기 호출로 live PageSetup 기반 본문 영역, 문서 관례·스타일, 상태 결합 provenance와 clean PNG 및 별도 analysis overlay를 반환합니다.",
        "verification",
        "read",
        "com_dispatch",
        binding_owner="public_inspection_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_disconnect",
        "HWP를 종료하지 않고 연결 참조만 해제합니다.",
        "document",
        "session",
        "rot_com",
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_capabilities",
        "현재 프로필의 도구를 기능·읽기/쓰기·실행 경로별로 반환합니다.",
        "catalog",
        "read",
        "python_catalog",
        handler_source="catalog",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_search_tools",
        "한컴 도구를 검색합니다. 공식 API 찾아봐·API 열어봐·API 확인·명세 확인은 include_official_api=true 호출에만 상세·근거를 최대 5건 포함합니다.",
        "catalog",
        "read",
        "python_catalog",
        handler_source="catalog",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_operation_status",
        "호출자 제공 operation_id로 현재 문서의 작업 상태를 조회하고 완료 결과를 다시 받습니다.",
        "state",
        "read",
        "com_dispatch",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_runtime_info",
        "현재 MCP 버전·프로세스·소스 경로·도구 스키마 해시와 소스 갱신 필요 여부를 반환합니다.",
        "diagnostic",
        "read",
        "python_catalog",
        handler_source="server",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_execute",
        "이 서버의 도구 하나를 tool_name과 arguments로 대신 호출합니다. 전달 대상은 지금 보이는 도구 목록과 정확히 같고 숨은 도구는 없으므로, 도구가 보이면 그냥 직접 부르세요. 갱신 직후 새 도구가 아직 목록에 안 뜰 때만 필요합니다. arguments는 그 도구의 인자를 그대로 담은 객체입니다.",
        "catalog",
        "write",
        "mcp_forward",
        handler_source="gateway",
        verification_delegate="forwarded_tool",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_reload",
        "현재 Codex 연결을 유지한 채 설치 소스에서 HWP MCP 워커를 다시 시작하고 갱신된 런타임 신원과 도구 스키마 해시를 반환합니다.",
        "diagnostic",
        "session",
        "mcp_forward",
        handler_source="proxy",
        exposure="proxy",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    # --- 한컴MCP 리본 탭 custom action (Stage 1) --------------------------
    # 일곱 도구 모두 문서를 열지도 바꾸지도 않는다. 레지스트리 JSON 파일 하나와
    # 리본 탭만 다루므로 effect는 artifact다.
    McpToolSpec(
        "hwp_list_custom_actions",
        "한컴MCP 리본 탭에 올릴 사용자 정의 동작(custom action) 목록과 각 스텝이 현재"
        + " 프로필에서 아직 유효한 도구인지를 반환합니다. include_tab_state=true이면 실제"
        + " 리본 탭 상태도 읽기 전용으로 관측합니다. 탭 자체는 워커가 한/글에 붙는"
        + " 순간 자동으로 서므로, 등록된 action이 0개여도 씨앗 버튼 하나짜리 기본 탭이"
        + " 리본에 있습니다(tab.default_tab=true). 문서는 열지 않습니다.",
        "catalog",
        "read",
        "python_catalog",
        binding_owner="custom_action_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_get_custom_action",
        "action_id 하나의 custom action 정의를 스텝·인자·순서까지 반환합니다. 문서는"
        + " 열지 않습니다.",
        "catalog",
        "read",
        "python_catalog",
        binding_owner="custom_action_tools",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_register_custom_action",
        "확정된 공개 도구 호출 시퀀스를 custom action으로 등록하고 한컴MCP 리본 탭을"
        + " 다시 구성합니다. 스텝의 도구 이름은 현재 프로필에 등록된 전달 가능한 공개"
        + " 도구여야 하며 hwp_execute와 custom action 도구 자신은 거부합니다. 원시"
        + " HAction·COM·스크립트 매크로를 담을 필드는 없습니다. 브리지 DLL이 슬롯 AID"
        + " 32개를 광고하므로 클릭은 어느 action인지 구분되고, MCP 워커가 살아 있으면"
        + " 그 레시피가 공개 도구 경로로 실행됩니다. 재구성은 매번 새 탭 키로 하고"
        + " 옛 키를 지우므로 한/글 재기동 없이 리본에 즉시 반영됩니다. 한/글이 안 떠"
        + " 있으면 등록만 성공하고 탭은 deferred로 보류합니다. 문서는 열지 않습니다.",
        "catalog",
        "write",
        "python_catalog",
        binding_owner="custom_action_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_update_custom_action",
        "기존 custom action의 라벨·설명·스텝을 바꾸고 리본 탭을 다시 구성합니다. 스텝"
        + " 검증 규칙은 등록과 같습니다. 문서는 열지 않습니다.",
        "catalog",
        "write",
        "python_catalog",
        binding_owner="custom_action_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_delete_custom_action",
        "custom action 하나를 레지스트리에서 지우고 남은 것으로 리본 탭을 다시 구성합니다."
        + " 문서는 열지 않습니다.",
        "catalog",
        "write",
        "python_catalog",
        binding_owner="custom_action_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_reorder_custom_actions",
        "action_ids에 등록된 custom action 전체를 정확히 한 번씩 담아 버튼 순서를"
        + " 재배치하고 리본 탭을 다시 구성합니다. 문서는 열지 않습니다.",
        "catalog",
        "write",
        "python_catalog",
        binding_owner="custom_action_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_remove_custom_action_tab",
        "한컴MCP 리본 탭을 제거합니다. purge_registry=true이면 custom action 정의까지"
        + " 지우고, 기본값 false이면 정의는 보존해 나중에 다시 구성할 수 있게 둡니다."
        + " 그 한/글에 워커가 다시 붙거나 custom action을 다시 만지면 탭은 되살아납니다."
        + " 문서는 열지 않습니다.",
        "catalog",
        "write",
        "python_catalog",
        binding_owner="custom_action_tools",
        effect_override="artifact",
        requires_session=False,
        profiles=_ALL_PROFILES,
    ),
    McpToolSpec(
        "hwp_search_official_api",
        "공식 2025-04 API 카탈로그에서 이름·설명·선언·근거 쪽을 검색합니다.",
        "catalog",
        "read",
        "python_catalog",
        handler_source="catalog",
        requires_session=False,
    ),
    McpToolSpec(
        "hwp_get_official_api_coverage",
        "공식 API를 네이티브 실행 가능·정확한 매핑·인덱스 전용으로 분류합니다.",
        "catalog",
        "read",
        "python_catalog",
        handler_source="catalog",
        requires_session=False,
    ),
)

_TOOL_BY_NAME: Final[Mapping[str, McpToolSpec]] = MappingProxyType(
    {spec.name: spec for spec in MCP_TOOL_SPECS}
)


def configured_mcp_profile(arguments: Sequence[str] = ()) -> McpProfile:
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--profile":
            raise McpProfileError(" ".join(arguments))
        value = arguments[1]
    else:
        value = environ.get("HANCOM_MCP_PROFILE", "production")
    if value == "production" or value == "qa":
        return value
    raise McpProfileError(value)


def tool_spec(name: str) -> McpToolSpec:
    return _TOOL_BY_NAME[name]


def tool_effect(name: str) -> ToolEffect:
    return tool_spec(name).effect


def tool_specs(profile: McpProfile) -> tuple[McpToolSpec, ...]:
    return tuple(
        spec
        for spec in MCP_TOOL_SPECS
        if profile in spec.profiles and spec.exposure == "worker"
    )


def proxy_tool_specs(profile: McpProfile) -> tuple[McpToolSpec, ...]:
    return tuple(
        spec
        for spec in MCP_TOOL_SPECS
        if profile in spec.profiles and spec.exposure == "proxy"
    )


def tool_names(profile: McpProfile) -> frozenset[str]:
    return frozenset(spec.name for spec in tool_specs(profile))


def search_tool_specs(
    query: str,
    *,
    profile: McpProfile,
    category: CapabilityCategory | None = None,
    operation: CapabilityOperation | None = None,
    limit: int = 10,
) -> tuple[McpToolSpec, ...]:
    if not query.strip():
        raise McpToolSearchError("query", "tool search query must not be blank")
    if limit < 1 or limit > 50:
        raise McpToolSearchError(
            "limit",
            "tool search limit must be between 1 and 50",
        )
    candidates = tuple(
        spec
        for spec in tool_specs(profile)
        if (category is None or spec.category == category)
        and (operation is None or spec.operation == operation)
    )
    return rank_tool_specs(query, candidates, limit=limit)
