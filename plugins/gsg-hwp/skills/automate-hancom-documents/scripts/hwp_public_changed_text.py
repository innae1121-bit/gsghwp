from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from hwp_errors import HwpLiveError
from hwp_live_native_action_models import LiteralTextRange, NativePosition
from hwp_live_native_format_inputs import TextFormatSpec
from hwp_live_text_patch_contract import TextPatchRequest, TextPatchTarget
from hwp_public_action_contract import PublicTextPatchBatch


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    start: int
    end: int


def _tokens(value: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    for match in re.finditer(r"\s+|\S+", value):
        text = match.group()
        prefix = 0
        # An unchanged leading symbol (for example, ◦ or an emoji) is separate
        # from its adjacent word. Keep number punctuation such as (25) intact.
        while prefix < len(text) and unicodedata.category(text[prefix]).startswith("S"):
            prefix += 1
        if 0 < prefix < len(text):
            tokens.append(_Token(text[:prefix], match.start(), match.start() + prefix))
            tokens.append(_Token(text[prefix:], match.start() + prefix, match.end()))
        else:
            tokens.append(_Token(text, match.start(), match.end()))
    return tuple(tokens)


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _request(
    target: TextPatchTarget,
    before: str,
    replacement: str,
    span: tuple[int, int],
    formatting: TextFormatSpec | None,
    inspected: bool = False,
) -> TextPatchRequest:
    start = target.start
    if start is None:
        raise HwpLiveError("changed-only range target is incomplete")
    left, right = span
    # Guarded inspection keeps these offsets only for ordering. The literal
    # context requires native preparation before these requests can execute.
    character = start.character + _units(before[:left])
    return TextPatchRequest(
        TextPatchTarget(
            kind="range",
            start=NativePosition(start.list_id, start.paragraph, character),
            end=NativePosition(
                start.list_id,
                start.paragraph,
                character + _units(before[left:right]),
            ),
        ),
        before[left:right],
        replacement,
        formatting,
        literal_range=(
            LiteralTextRange(before, _units(before[:left]), _units(before[:right]))
            if inspected and start.character == 0
            else None
        ),
    )


def _paragraph_requests(
    before: str,
    after: str,
    target: TextPatchTarget,
    formatting: TextFormatSpec,
    inspected: bool = False,
) -> tuple[TextPatchRequest, ...]:
    start = target.start
    end = target.end
    if start is None or end is None or start.list_id != end.list_id:
        raise HwpLiveError("changed-only range target is incomplete")
    if start.paragraph != end.paragraph:
        raise HwpLiveError(
            "changed-only range target must stay within one paragraph",
            mutation_started=False,
            safe_to_repeat=True,
        )
    return tuple(
        _request(
            target,
            before,
            replacement,
            (left, right),
            formatting if replacement.strip() else None,
            inspected,
        )
        for left, right, replacement in _changed_spans(before, after)
    )


def _changed_spans(before: str, after: str) -> tuple[tuple[int, int, str], ...]:
    old_tokens = _tokens(before)
    new_tokens = _tokens(after)
    matcher = SequenceMatcher(
        None,
        tuple(token.text for token in old_tokens),
        tuple(token.text for token in new_tokens),
        autojunk=False,
    )
    spans: list[tuple[int, int, str]] = []
    for tag, old_left, old_right, new_left, new_right in matcher.get_opcodes():
        if tag == "equal":
            continue
        left = old_tokens[old_left].start if old_left < len(old_tokens) else len(before)
        right = old_tokens[old_right - 1].end if old_right > old_left else left
        replacement = "".join(token.text for token in new_tokens[new_left:new_right])
        # A shortened token can retain a prefix/suffix that owns an embedded
        # HWP control. Delete only the removed text, preserving that anchor.
        removed = before[left:right]
        if replacement and len(replacement) < len(removed):
            if removed.startswith(replacement):
                left += len(replacement)
                replacement = ""
            elif removed.endswith(replacement):
                right -= len(replacement)
                replacement = ""
        if left == right:
            if (
                old_left == 0
                or old_left == len(old_tokens)
                or not old_tokens[old_left - 1].text.isspace()
            ):
                raise HwpLiveError(
                    "changed-only insertion needs an interior whitespace anchor",
                    mutation_started=False,
                    safe_to_repeat=True,
                )
            left = old_tokens[old_left - 1].start
            replacement = before[left:right] + replacement
        if spans and spans[-1][1] > left:
            raise HwpLiveError(
                "changed-only whitespace anchors overlap",
                mutation_started=False,
                safe_to_repeat=True,
            )
        spans.append((left, right, replacement))
    return tuple(spans)


def _cell_request(
    before: str,
    after: str,
    target: TextPatchTarget,
    formatting: TextFormatSpec,
) -> TextPatchRequest:
    spans = _changed_spans(before, after)
    left, right = spans[0][0], spans[-1][1]
    for previous, following in zip(spans, spans[1:]):
        if before[previous[1] : following[0]].strip():
            raise HwpLiveError(
                "changed-only cell changes surround unchanged visible text",
                mutation_started=False,
                safe_to_repeat=True,
            )
    expected = before[left:right]
    if before.find(expected, left + 1) != -1 or before.find(expected) != left:
        raise HwpLiveError(
            "changed-only cell expected substring is not unique",
            mutation_started=False,
            safe_to_repeat=True,
        )
    replacement = before[left:right]
    for span_left, span_right, value in reversed(spans):
        replacement = (
            replacement[: span_left - left] + value + replacement[span_right - left :]
        )
    return TextPatchRequest(
        target,
        expected,
        replacement,
        formatting if replacement.strip() else None,
    )


def changed_text_requests(
    patches: PublicTextPatchBatch,
    formatting: TextFormatSpec,
    *,
    inspected: bool = False,
) -> tuple[TextPatchRequest, ...]:
    requests: list[TextPatchRequest] = []
    for patch in patches:
        if patch.expected_text is None:
            raise HwpLiveError(
                "changed-only batch requires expected_text for every item",
                mutation_started=False,
                safe_to_repeat=True,
            )
        if patch.expected_text == patch.replacement:
            continue
        live = patch.to_live()
        match live.target.kind:
            case "range":
                requests.extend(
                    _paragraph_requests(
                        patch.expected_text,
                        patch.replacement,
                        live.target,
                        formatting,
                        inspected,
                    )
                )
            case "table_cell":
                requests.append(
                    _cell_request(
                        patch.expected_text,
                        patch.replacement,
                        live.target,
                        formatting,
                    )
                )
            case "current" | "find":
                raise HwpLiveError(
                    "changed-only batch requires exact range or table_cell targets",
                    mutation_started=False,
                    safe_to_repeat=True,
                )
    if not requests:
        raise HwpLiveError(
            "changed-only batch contains no text changes",
            mutation_started=False,
            safe_to_repeat=True,
        )
    return tuple(requests)
