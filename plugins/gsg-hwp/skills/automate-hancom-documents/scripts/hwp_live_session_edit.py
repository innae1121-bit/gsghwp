from __future__ import annotations

from hwp_errors import HwpLiveError
from hwp_live_contract import (
    LayoutPlan,
    LayoutResult,
    MutationResult,
    SelectionPosition,
)
from hwp_live_edit_history_runtime import execute_managed_text_patch
from hwp_live_layout import prepare_layout_assets
from hwp_live_native_table_snapshot import retain_table_result_snapshot
from hwp_live_session_inspection import LiveHwpInspectionSession
from hwp_live_session_structure import (
    apply_validated_layout,
    insert_validated_table_images,
    replace_validated_selection,
    update_validated_table_cells,
)
from hwp_live_structure_contract import (
    DocumentStructure,
    StructureTable,
    TableCellUpdate,
    TableImageResult,
    TableImageUpdate,
    TableUpdateResult,
)
from hwp_live_text_patch_batch_history import execute_managed_text_patch_batch
from hwp_live_text_patch_contract import (
    TextPatchPlanGuard,
    TextPatchRequest,
    TextPatchResult,
)


class LiveHwpEditSession(LiveHwpInspectionSession):
    __slots__: tuple[str, ...] = ()
    _structure_snapshot: DocumentStructure | None

    def replace_selection(
        self,
        session_id: str,
        expected_selection: SelectionPosition,
        expected_text: str,
        replacement: str,
    ) -> MutationResult:
        candidate, hwp = self._validate(session_id)
        return replace_validated_selection(
            hwp,
            candidate,
            expected_selection,
            expected_text,
            replacement,
            self._unsafe_selectors,
            self._guard(candidate, hwp),
        )

    def patch_text(
        self,
        session_id: str,
        request: TextPatchRequest,
    ) -> TextPatchResult:
        candidate, hwp = self._validate(session_id)
        if (
            request.formatting is not None
            and request.formatting.letter_spacing_percent is None
            and request.formatting.left_margin_mm is None
            and request.formatting.indentation_mm is None
            and request.formatting.keep_with_next is None
            and request.formatting.page_break_before is None
            and request.target.kind != "current"
        ):
            # A format-only patch is a one-target prepared transaction. It captures
            # only requested prior format values and records native inverses; no
            # document-file checkpoint belongs on this public formatting route.
            #
            # ``current`` stays on the single path: the batch engine validates with
            # require_current_expected=True, which rejects a cursor-targeted patch
            # that carries no expected_text -- and the cursor is the caller's own
            # selection, so there is nothing to re-confirm it against. The single
            # path supports formatting on its own (execute_managed_text_patch sends
            # every formatting request down the checkpointed route).
            return execute_managed_text_patch_batch(
                hwp,
                candidate,
                self._live_edit_history,
                (request,),
                self._unsafe_selectors,
                self._guard(candidate, hwp),
            )
        # Spacing uses the single, checkpoint-aware route: the loaded bridge
        # verifies all seven Spacing* setters, but prepared inverse receipts do
        # not contain those properties. Do not misreport a batch as undoable.
        return execute_managed_text_patch(
            hwp,
            candidate,
            self._live_edit_history,
            request,
            self._unsafe_selectors,
            self._guard(candidate, hwp),
        )

    def patch_text_batch(
        self,
        session_id: str,
        requests: tuple[TextPatchRequest, ...],
        plan_guard: TextPatchPlanGuard | None = None,
    ) -> TextPatchResult:
        candidate, hwp = self._validate(session_id)
        return execute_managed_text_patch_batch(
            hwp,
            candidate,
            self._live_edit_history,
            requests,
            self._unsafe_selectors,
            self._guard(candidate, hwp),
            plan_guard,
        )

    def apply_layout(
        self,
        session_id: str,
        plan: LayoutPlan,
        expected_cursor: tuple[int, int, int],
        expected_selected_text: str | None,
    ) -> LayoutResult:
        assets = prepare_layout_assets(plan)
        candidate, hwp = self._validate(session_id)
        return apply_validated_layout(
            hwp,
            candidate,
            plan,
            assets,
            self._unsafe_selectors,
            expected_cursor,
            expected_selected_text,
            self._guard(candidate, hwp),
        )

    def update_table_cells(
        self,
        session_id: str,
        table_ref: str,
        state_token: str,
        updates: tuple[TableCellUpdate, ...],
    ) -> TableUpdateResult:
        candidate, hwp = self._validate(session_id)
        cached_snapshot, self._structure_snapshot = self._structure_snapshot, None
        result = update_validated_table_cells(
            hwp,
            candidate,
            table_ref,
            state_token,
            updates,
            self._unsafe_selectors,
            cached_snapshot,
            self._guard(candidate, hwp),
        )
        self._structure_snapshot = retain_table_result_snapshot(
            cached_snapshot,
            result.table,
            result.state_token,
        )
        return result

    @staticmethod
    def _table(snapshot: DocumentStructure, table_ref: str) -> StructureTable:
        table = next(
            (item for item in snapshot.tables if item.table_ref == table_ref),
            None,
        )
        if table is None:
            raise HwpLiveError("지정한 쪽에서 표 참조값을 찾지 못했습니다")
        return table

    def insert_table_images(
        self,
        session_id: str,
        table_ref: str,
        state_token: str,
        images: tuple[TableImageUpdate, ...],
    ) -> TableImageResult:
        candidate, hwp = self._validate(session_id)
        cached_snapshot, self._structure_snapshot = self._structure_snapshot, None
        result = insert_validated_table_images(
            hwp,
            candidate,
            table_ref,
            state_token,
            images,
            self._unsafe_selectors,
            cached_snapshot,
            self._guard(candidate, hwp),
        )
        self._structure_snapshot = retain_table_result_snapshot(
            cached_snapshot,
            result.table,
            result.state_token,
        )
        return result
