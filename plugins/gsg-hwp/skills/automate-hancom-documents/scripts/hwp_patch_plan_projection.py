from __future__ import annotations

import hashlib
import json

from hwp_errors import HwpLiveError
from hwp_live_structure_contract import DocumentStructure
from hwp_patch_plan_contract import (
    CellPatchFact,
    ParagraphPatchFact,
    PatchCellTarget,
    PatchFact,
    PatchPlanComplete,
    PatchPlanDocument,
    PatchPlanOverflow,
    PatchPlanResult,
    PatchRangeTarget,
)


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _document(
    structures: tuple[DocumentStructure, ...],
    content_revision: str,
    include_cells: bool,
) -> PatchPlanDocument:
    first = structures[0]
    ordered = tuple(sorted(structures, key=lambda item: item.page))
    identity = (first.document_id, first.full_name, first.page_count)
    if any(
        (item.document_id, item.full_name, item.page_count) != identity
        for item in ordered
    ):
        raise HwpLiveError("patch plan 쪽 조회 결과의 문서 identity가 서로 다릅니다")
    if len({item.page for item in ordered}) != len(ordered):
        raise HwpLiveError("patch plan 쪽 번호는 중복될 수 없습니다")
    page_tokens = tuple((item.page, item.state_token) for item in ordered)
    canonical = json.dumps(page_tokens, ensure_ascii=False, separators=(",", ":"))
    return PatchPlanDocument(
        document_id=first.document_id,
        full_name=first.full_name,
        content_revision=content_revision,
        page_count=first.page_count,
        state_token=hashlib.sha256(canonical.encode()).hexdigest(),
        page_state_tokens=page_tokens,
        table_scan_errors=(
            tuple(
                (item.page, item.table_scan_error)
                for item in ordered
                if not item.tables_complete and item.table_scan_error is not None
            )
            if include_cells
            else ()
        ),
    )


def _items(
    structures: tuple[DocumentStructure, ...],
    include_paragraphs: bool,
    include_cells: bool,
) -> tuple[PatchFact, ...]:
    facts: list[PatchFact] = []
    paragraphs_by_target: dict[tuple[int, int, int], ParagraphPatchFact] = {}
    cells_by_target: dict[tuple[str, str], CellPatchFact] = {}
    for structure in sorted(structures, key=lambda item: item.page):
        if include_paragraphs:
            for paragraph in sorted(structure.paragraphs, key=lambda item: item.index):
                if not paragraph.text_available or paragraph.position is None:
                    continue
                heading_type = paragraph.paragraph_style.heading_type
                # The structure scanner returns literal text. Heading metadata
                # does not identify a removable character in that text.
                text = paragraph.text
                base = paragraph.position
                start = base
                end = base.model_copy(
                    update={"character": base.character + _utf16_units(paragraph.text)}
                )
                fact = ParagraphPatchFact(
                    id=(
                        f"p:{structure.page}:{start.list_id}:"
                        f"{start.paragraph}:{start.character}:{end.character}"
                    ),
                    page=structure.page,
                    text=text,
                    target=PatchRangeTarget(start=start, end=end),
                    heading_type=heading_type,
                    heading_level=paragraph.paragraph_style.heading_level,
                )
                paragraph_key = (start.list_id, start.paragraph, start.character)
                previous_paragraph = paragraphs_by_target.get(paragraph_key)
                if previous_paragraph is not None:
                    if (
                        previous_paragraph.text != fact.text
                        or previous_paragraph.heading_type != fact.heading_type
                        or previous_paragraph.heading_level != fact.heading_level
                    ):
                        raise HwpLiveError(
                            "patch plan page-spanning paragraph is inconsistent: "
                            + f"target={paragraph_key}"
                        )
                    continue
                paragraphs_by_target[paragraph_key] = fact
                facts.append(fact)
        if include_cells:
            # A failed table is excluded, not the page. hwp_live_native_structure
            # already drops the failed controls from ``tables`` and keeps every
            # healthy one, and refuses the page outright when nothing healthy is
            # left -- so the tables that arrive here are exactly the readable
            # ones. Refusing the whole page over one of them would throw away
            # that deliberate partial evidence. What the failure was is carried
            # to the caller in ``document.table_scan_errors``; a request aimed at
            # a failed table finds no fact for it and is refused there, with the
            # reason to hand.
            for table in sorted(
                structure.tables,
                key=lambda item: (item.anchor.list_id, item.table_ref),
            ):
                table_id = table.control_instance_id
                if table_id is None:
                    raise HwpLiveError(
                        f"patch plan 표의 native instance id가 없습니다: {table.table_ref}"
                    )
                owners = {
                    cell.address
                    for cell in table.cells
                    if cell.address == cell.owner_address
                }
                for cell in sorted(
                    table.cells, key=lambda item: (item.row, item.column)
                ):
                    if cell.owner_address not in owners:
                        raise HwpLiveError(
                            "patch plan merged cell owner is missing: "
                            + f"table={table_id}, cell={cell.address}, "
                            + f"owner={cell.owner_address}"
                        )
                    # Covered grid slots are not independently editable cells.
                    if cell.address != cell.owner_address:
                        continue
                    key = (table_id, cell.address)
                    cell_fact = CellPatchFact(
                        id=f"c:{table_id}:{cell.address}",
                        page=structure.page,
                        text=cell.text,
                        target=PatchCellTarget(
                            table_instance_id=table_id,
                            cell=cell.address,
                        ),
                    )
                    previous = cells_by_target.get(key)
                    if previous is not None:
                        if previous.text != cell_fact.text:
                            raise HwpLiveError(
                                "patch plan page-spanning cell text is inconsistent: "
                                + f"table={table_id}, cell={cell.address}"
                            )
                        continue
                    cells_by_target[key] = cell_fact
                    facts.append(cell_fact)
    return tuple(facts)


def _complete(
    document: PatchPlanDocument, items: tuple[PatchFact, ...]
) -> PatchPlanComplete:
    provisional = PatchPlanComplete(
        document=document,
        items=items,
        item_count=len(items),
        utf8_bytes=0,
    )
    size = len(provisional.model_dump_json().encode())
    while True:
        result = provisional.model_copy(update={"utf8_bytes": size})
        actual = len(result.model_dump_json().encode())
        if actual == size:
            return result
        size = actual


def _partitions(
    document: PatchPlanDocument,
    items: tuple[PatchFact, ...],
    max_items: int,
    max_utf8_bytes: int,
) -> tuple[tuple[tuple[int, ...], ...], tuple[int, ...]]:
    pages = tuple(dict.fromkeys(item.page for item in items))
    by_page = {
        page: tuple(item for item in items if item.page == page) for page in pages
    }
    partitions: list[tuple[int, ...]] = []
    current: tuple[int, ...] = ()
    impossible: list[int] = []
    for page in pages:
        page_items = by_page[page]
        if (
            len(page_items) > max_items
            or _complete(document, page_items).utf8_bytes > max_utf8_bytes
        ):
            impossible.append(page)
        candidate = (*current, page)
        candidate_items = tuple(item for part in candidate for item in by_page[part])
        if current and (
            len(candidate_items) > max_items
            or _complete(document, candidate_items).utf8_bytes > max_utf8_bytes
        ):
            partitions.append(current)
            current = (page,)
        else:
            current = candidate
    if current:
        partitions.append(current)
    return tuple(partitions), tuple(impossible)


def build_patch_plan(
    structures: tuple[DocumentStructure, ...],
    include_paragraphs: bool,
    include_cells: bool,
    max_items: int,
    max_utf8_bytes: int,
    content_revision: str,
) -> PatchPlanResult:
    """Project detailed pages into complete, deterministic patch facts."""
    if not structures:
        raise HwpLiveError("patch plan에는 한 쪽 이상이 필요합니다")
    if include_paragraphs:
        for structure in structures:
            if structure.paragraph_scan_error is not None:
                raise HwpLiveError(
                    f"patch plan paragraph scan failed on page {structure.page}: "
                    + structure.paragraph_scan_error
                )
            if not structure.paragraphs_complete:
                raise HwpLiveError(
                    f"patch plan paragraph scan incomplete on page {structure.page}"
                )
            unavailable = next(
                (
                    paragraph.index
                    for paragraph in structure.paragraphs
                    if not paragraph.text_available or paragraph.position is None
                ),
                None,
            )
            if unavailable is not None:
                raise HwpLiveError(
                    "patch plan requested paragraph target is unavailable: "
                    + f"page={structure.page}, paragraph={unavailable}"
                )
            truncated = next(
                (
                    paragraph.index
                    for paragraph in structure.paragraphs
                    if len(paragraph.text) >= 200_000
                ),
                None,
            )
            if truncated is not None:
                raise HwpLiveError(
                    "patch plan paragraph text may be truncated at the contract boundary: "
                    + f"page={structure.page}, paragraph={truncated}"
                )
    document = _document(structures, content_revision, include_cells)
    items = _items(structures, include_paragraphs, include_cells)
    complete = _complete(document, items)
    if len(items) <= max_items and complete.utf8_bytes <= max_utf8_bytes:
        return complete
    partitions, impossible = _partitions(
        document,
        items,
        max_items,
        max_utf8_bytes,
    )
    return PatchPlanOverflow(
        document=document,
        required_items=len(items),
        required_utf8_bytes=complete.utf8_bytes,
        max_items=max_items,
        max_utf8_bytes=max_utf8_bytes,
        page_partitions=partitions,
        unpartitionable_pages=impossible,
    )
