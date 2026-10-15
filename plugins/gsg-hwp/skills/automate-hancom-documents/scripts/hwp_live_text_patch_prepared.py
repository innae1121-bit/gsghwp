from __future__ import annotations


from dataclasses import replace

from hwp_errors import HwpLiveError

from hwp_live_native_action_models import NativePosition, PreparedTextPatchTarget

from hwp_live_text_patch_contract import TextPatchRequest


def _position_key(position: NativePosition) -> tuple[int, int, int]:

    return position.list_id, position.paragraph, position.character


def canonical_prepared_text_patch_requests(
    requests: tuple[TextPatchRequest, ...],
) -> tuple[TextPatchRequest, ...] | None:
    """Return immutable exact targets in position-safe execution order."""

    ranges: list[TextPatchRequest] = []

    cells: list[TextPatchRequest] = []

    format_only_semantic = (
        len(requests) == 1
        and requests[0].target.kind in {"current", "find"}
        and requests[0].formatting is not None
        and requests[0].expected_text is not None
        and requests[0].replacement == requests[0].expected_text
    )

    for request in requests:
        match request.target.kind:
            case "range":
                if request.target.start is None or request.target.end is None:
                    raise HwpLiveError("prepared range target is incomplete")

                if _position_key(request.target.start) >= _position_key(
                    request.target.end
                ):
                    raise HwpLiveError("prepared range target must be nonempty")

                ranges.append(request)

            case "table_cell":
                cells.append(request)

            case "current" | "find":
                if format_only_semantic:
                    ranges.append(request)
                else:
                    return None

    if format_only_semantic:
        return tuple(requests)

    ascending = sorted(
        ranges,
        key=lambda item: (
            _position_key(item.target.start)
            if item.target.start is not None
            else (-1, -1, -1)
        ),
    )

    for left, right in zip(ascending, ascending[1:], strict=False):
        left_end = left.target.end

        right_start = right.target.start

        if (
            left_end is not None
            and right_start is not None
            and left_end.list_id == right_start.list_id
            and _position_key(right_start) < _position_key(left_end)
        ):
            raise HwpLiveError("prepared range targets overlap")

    cell_keys = tuple(
        (request.target.table_instance_id, request.target.cell_address)
        for request in cells
    )

    if len(set(cell_keys)) != len(cell_keys):
        raise HwpLiveError("prepared table cell targets contain a duplicate")

    descending_ranges = tuple(reversed(ascending))

    deterministic_cells = tuple(
        sorted(
            cells,
            key=lambda item: (
                item.target.table_instance_id or "",
                item.target.cell_address or "",
            ),
        )
    )

    return (*descending_ranges, *deterministic_cells)


def effective_prepared_text_patch_requests(
    requests: tuple[TextPatchRequest, ...],
    targets: tuple[PreparedTextPatchTarget, ...],
) -> tuple[TextPatchRequest, ...]:
    """Bind exact prepared patches to the text captured before mutation."""
    canonical = canonical_prepared_text_patch_requests(requests)
    if canonical is None:
        raise HwpLiveError("semantic text.patch target cannot use prepared execution")
    if targets and len(targets) != len(canonical):
        raise HwpLiveError("prepared text.patch target capture 수가 요청과 다릅니다")
    if not targets:
        if any(request.expected_text is None for request in canonical):
            raise HwpLiveError(
                "prepared text.patch가 생략된 기존 텍스트를 캡처하지 못했습니다",
                mutation_started=False,
                safe_to_repeat=True,
            )
        return canonical

    effective: list[TextPatchRequest] = []
    for request, target in zip(canonical, targets, strict=True):
        if request.replacement == target.text and request.formatting is None:
            raise HwpLiveError(
                "text.patch replacement가 현재 대상 텍스트와 같습니다",
                mutation_started=False,
                safe_to_repeat=True,
            )
        bound = (
            request
            if request.expected_text is not None
            else replace(request, expected_text=target.text)
        )
        if request.target.kind in {"current", "find", "range"}:
            if request.target.kind == "range":
                start, end = request.target.start, request.target.end
                assert start is not None and end is not None
                if (start.list_id, start.paragraph) != (
                    target.selection.start.list_id,
                    target.selection.start.paragraph,
                ) or (end.list_id, end.paragraph) != (
                    target.selection.end.list_id,
                    target.selection.end.paragraph,
                ):
                    raise HwpLiveError("prepared range target changed paragraph scope")
            bound = replace(
                bound,
                literal_range=None,
                target=replace(
                    request.target,
                    kind="range",
                    start=target.selection.start,
                    end=target.selection.end,
                    occurrence=None,
                    match_case=False,
                    table_instance_id=None,
                    cell_address=None,
                ),
            )
        effective.append(bound)
    result = tuple(effective)
    if canonical_prepared_text_patch_requests(result) != result:
        raise HwpLiveError("prepared range targets changed execution order")
    return result
