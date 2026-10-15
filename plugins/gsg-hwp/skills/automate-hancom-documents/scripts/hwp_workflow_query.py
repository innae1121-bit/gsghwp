# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["pydantic==2.12.5", "pywin32==312"]
# ///
# --- How to run ---
# uv run --quiet skills/automate-hancom-documents/scripts/hwp_workflow_query.py \
#   < request.json
# uv run --quiet skills/automate-hancom-documents/scripts/hwp_workflow_query.py \
#   --out plan.json < request.json
# --quiet keeps uv's own install progress off this process' stdout; a pty merges
# the two streams, and an "Installed ..." line in front of the JSON is what a
# caller then has to strip. --out writes the JSON to a file instead, so the
# stdout contract never has to be trusted at all. Without --out, stdout stays
# exactly what it has always been: the canonical JSON and nothing else.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol, cast
from zipfile import BadZipFile, ZipFile

from pydantic import JsonValue, TypeAdapter, ValidationError

from hwp_errors import HwpLiveError
from hwp_office_xlsx import (
    XlsxMerge,
    XlsxSheet,
    iter_xlsx_rows,
    merged_ranges,
    sheet_inventory,
)
from hwp_office_xlsx_safety import XlsxReadLimits
from hwp_workflow_row_evidence import WorksheetRows, worksheet_matches

if TYPE_CHECKING:
    from hwp_workflow_query_contract import (
        QueryInput,
        QueryResult,
        WorkflowLimits,
        WorkflowRequest,
    )


class _XlsxLimitSource(Protocol):
    zip_members: int
    zip_entry_bytes: int
    zip_total_bytes: int
    zip_compression_ratio: int
    shared_strings: int
    shared_string_bytes: int
    xml_depth: int
    xml_text_bytes: int
    cell_chars: int


class _ReadXlsxLimits:
    workbooks: int = 32
    sheets_per_workbook: int = 128
    rows_per_query: int = 10_000
    columns_per_query: int = 256
    cells: int = 50_000
    output_utf8_bytes: int = 1_000_000
    zip_members: int = 2_048
    zip_entry_bytes: int = 32_000_000
    zip_total_bytes: int = 128_000_000
    zip_compression_ratio: int = 200
    shared_strings: int = 200_000
    shared_string_bytes: int = 32_000_000
    xml_depth: int = 64
    xml_text_bytes: int = 64_000_000
    cell_chars: int = 200_000


_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_MAX_STDIN_BYTES = 8_000_000


def _xlsx_limits(limits: _XlsxLimitSource) -> XlsxReadLimits:
    return XlsxReadLimits(
        members=limits.zip_members,
        entry_bytes=limits.zip_entry_bytes,
        total_bytes=limits.zip_total_bytes,
        compression_ratio=limits.zip_compression_ratio,
        shared_strings=limits.shared_strings,
        shared_string_bytes=limits.shared_string_bytes,
        xml_depth=limits.xml_depth,
        xml_text_bytes=limits.xml_text_bytes,
        cell_chars=limits.cell_chars,
    )


def _header_index(
    headers: tuple[str, ...], aliases: tuple[str, ...], label: str
) -> int | None:
    from hwp_workflow_query_contract import WorkflowQueryError

    matches = tuple(index for index, value in enumerate(headers) if value in aliases)
    if len(matches) > 1:
        raise WorkflowQueryError(
            "ambiguous_header",
            f"{label} header aliases resolved to multiple columns",
        )
    return None if not matches else matches[0]


def _merged_header_labels(
    header_row: int,
    header: Mapping[int, str],
    merges: tuple[XlsxMerge, ...],
) -> tuple[dict[int, str], dict[int, str]]:
    """Fill blanks a merge explains, and say which merge explained each one.

    A merged heading writes its text once, at the rectangle's first cell; every
    other column it covers is simply absent from the sheet, so the reader saw
    an empty label and the caller had to map that column by hand. The fill is
    an inference, so it is returned separately from the values and each filled
    column carries the ``ref`` of the merge it came from. Matching is left
    untouched: a fill can only repeat a label the same row already writes.
    """
    filled = dict(header)
    sources: dict[int, str] = {}
    for merge in merges:
        # XlsxMerge coordinates are zero-based; XlsxRow.index is the sheet's
        # own one-based ``r`` attribute.
        if not merge.first_row <= header_row - 1 <= merge.last_row:
            continue
        label = header.get(merge.first_column, "")
        if not label:
            continue
        for column in range(merge.first_column + 1, merge.last_column + 1):
            if filled.get(column, ""):
                continue
            filled[column] = label
            sources[column] = merge.ref
    return filled, sources


@dataclass(frozen=True, slots=True)
class _ResolvedHeaders:
    row: int
    headers: tuple[str, ...]
    key_index: int
    columns: tuple[tuple[str, int], ...]
    merged: tuple[tuple[int, str, str], ...]


def _resolve_headers(
    query: QueryInput,
    rows: Mapping[int, Mapping[int, str]],
    merges: tuple[XlsxMerge, ...] = (),
) -> _ResolvedHeaders:
    from hwp_workflow_query_contract import WorkflowQueryError

    resolved: list[_ResolvedHeaders] = []
    candidates = tuple(dict.fromkeys((query.header_row, *query.candidate_header_rows)))
    for row_index in candidates:
        sparse = rows.get(row_index, {})
        labels, sources = _merged_header_labels(row_index, sparse, merges)
        # 병합이 덮은 빈 칸까지 폭에 넣는다. 값은 시트에 적힌 그대로 두고,
        # 그 칸이 왜 비었는지는 merged 가 말한다.
        highest = max((*sparse, *sources, -1))
        headers = tuple(sparse.get(index, "") for index in range(highest + 1))
        key_index = _header_index(
            headers,
            (query.key_column, *query.key_column_aliases),
            query.key_column,
        )
        columns = tuple(
            (alias, _header_index(headers, names, alias))
            for alias, names in sorted(query.columns.items())
        )
        if key_index is not None and all(index is not None for _, index in columns):
            resolved.append(
                _ResolvedHeaders(
                    row_index,
                    headers,
                    key_index,
                    tuple(
                        (alias, index) for alias, index in columns if index is not None
                    ),
                    tuple(
                        (column, labels[column], reference)
                        for column, reference in sorted(sources.items())
                    ),
                )
            )
    if not resolved:
        raise WorkflowQueryError(
            "missing_header", "candidate header rows did not resolve"
        )
    if len(resolved) != 1:
        raise WorkflowQueryError(
            "ambiguous_header_row", "multiple candidate header rows resolved"
        )
    return resolved[0]


def _resolve_query(
    query: QueryInput,
    workbook_path: Path,
    archive: ZipFile,
    limits: WorkflowLimits,
) -> QueryResult:
    from hwp_workflow_query_contract import QueryResult, WorkflowQueryError

    xlsx_limits = _xlsx_limits(limits)
    sheets = sheet_inventory(archive, xlsx_limits)
    if len(sheets) > limits.sheets_per_workbook:
        raise WorkflowQueryError("sheet_limit", "workbook sheet limit exceeded")
    names = frozenset((query.sheet, *query.sheet_aliases))
    matches = tuple(sheet for sheet in sheets if sheet.name in names)
    if len(matches) != 1:
        raise WorkflowQueryError(
            "ambiguous_sheet",
            f"sheet aliases resolved to {len(matches)} sheets",
        )
    sheet: XlsxSheet = matches[0]
    merges = merged_ranges(archive, sheet, xlsx_limits)
    candidates = tuple(dict.fromkeys((query.header_row, *query.candidate_header_rows)))
    first_row = min(candidates)
    last_row = max(candidates) + limits.rows_per_query
    rows = {
        row.index: dict(row.values)
        for row in iter_xlsx_rows(
            archive,
            sheet,
            first_row=first_row,
            last_row=last_row,
            columns=frozenset(range(limits.columns_per_query)),
            max_cells=limits.cells,
            limits=xlsx_limits,
            reject_formulas=True,
        )
    }
    resolved = _resolve_headers(query, rows, merges)
    header_row = resolved.row
    key_index = resolved.key_index
    resolved_columns = resolved.columns
    key_values = frozenset((query.key_value, *query.key_value_aliases))
    found = tuple(
        (
            row_index,
            tuple(sparse.get(index, "") for _, index in resolved_columns),
        )
        for row_index, sparse in sorted(rows.items())
        if row_index > header_row and sparse.get(key_index, "") in key_values
    )
    if not found:
        raise WorkflowQueryError("missing_row", "key aliases did not resolve to a row")
    if len(found) != 1:
        raise WorkflowQueryError(
            "ambiguous_row", "key aliases resolved to multiple rows"
        )
    row_index, values = found[0]
    return QueryResult(
        workbook_alias=query.workbook,
        workbook_path=str(workbook_path.resolve()),
        sheet=sheet.name,
        headers=resolved.headers,
        row=row_index,
        values=tuple(
            (alias, value)
            for (alias, _), value in zip(resolved_columns, values, strict=True)
        ),
        merged_headers=resolved.merged,
    )


def _canonical_json(value: JsonValue | Mapping[str, JsonValue]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_identity(source: BinaryIO) -> tuple[int, int, int, int]:
    stat = os.fstat(source.fileno())
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _source_sha256(source: BinaryIO) -> str:
    position = source.tell()
    digest = hashlib.sha256()
    try:
        # Seeking outside the BufferedReader window invalidates bytes cached from
        # the initial ZIP parse, so same-size in-place rewrites are re-read.
        _ = source.seek(0, os.SEEK_END)
        _ = source.seek(0)
        while chunk := source.read(1_048_576):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        _ = source.seek(position)


def read_workbook_rows(
    paths: tuple[Path, ...],
    *,
    key: str,
    columns: tuple[str, ...] = (),
    sheet_queries: tuple[str, ...] = (),
    limits: WorkflowLimits | None = None,
) -> Mapping[str, JsonValue]:
    """Read each workbook once and return only rows containing the requested key."""
    active_limits = _ReadXlsxLimits() if limits is None else limits
    if not paths or len(paths) > active_limits.workbooks:
        raise ValueError("workbook limit exceeded")
    xlsx_limits = _xlsx_limits(active_limits)
    output: list[JsonValue] = []
    with ExitStack() as stack:
        for path in paths:
            source = stack.enter_context(path.open("rb"))
            archive = stack.enter_context(ZipFile(source))
            sheets = sheet_inventory(archive, xlsx_limits)
            if len(sheets) > active_limits.sheets_per_workbook:
                raise ValueError("workbook sheet limit exceeded")
            normalized_queries = tuple(
                "".join(query.split()).casefold() for query in sheet_queries
            )
            selected_sheets = tuple(
                sheet
                for sheet in sheets
                if not normalized_queries
                or any(
                    query in "".join(sheet.name.split()).casefold()
                    for query in normalized_queries
                )
            )
            matches: list[JsonValue] = []
            for sheet in selected_sheets:
                rows = tuple(
                    iter_xlsx_rows(
                        archive,
                        sheet,
                        first_row=1,
                        last_row=active_limits.rows_per_query,
                        columns=frozenset(range(active_limits.columns_per_query)),
                        max_cells=active_limits.cells,
                        limits=xlsx_limits,
                        reject_formulas=False,
                    )
                )
                merges = merged_ranges(archive, sheet, xlsx_limits)
                matches.extend(
                    worksheet_matches(
                        WorksheetRows(
                            sheet.name, rows, merges, active_limits.columns_per_query
                        ),
                        key,
                        columns,
                    )
                )
            output.append(
                {
                    "path": str(path.resolve()),
                    "actual_sheets": [sheet.name for sheet in sheets],
                    "matched_sheets": [sheet.name for sheet in selected_sheets],
                    "matches": matches,
                }
            )
    result = _JSON_OBJECT.validate_python({"key": key, "workbooks": output})
    if len(_canonical_json(result).encode()) > active_limits.output_utf8_bytes:
        raise ValueError("output UTF-8 byte limit exceeded")
    return result


def build_workflow_plan(request: WorkflowRequest) -> Mapping[str, JsonValue]:
    """Resolve bounded workbook facts into guarded exact projection targets."""
    from hwp_live_text_patch_batch import compile_text_patch_batch
    from hwp_live_text_patch_contract import (
        TextPatchRequest,
        validate_text_patch_request,
    )
    from hwp_live_text_patch_prepared import canonical_prepared_text_patch_requests
    from hwp_public_action_contract import PublicTextPatchItem, PublicTextPatchTarget
    from hwp_workflow_query_contract import WorkflowQueryError

    if len(request.workbooks) > request.limits.workbooks:
        raise WorkflowQueryError("workbook_limit", "workbook limit exceeded")
    if len(request.queries) > request.limits.queries:
        raise WorkflowQueryError("query_limit", "query limit exceeded")
    workbook_inputs = {item.alias: item for item in request.workbooks}
    results: dict[str, QueryResult] = {}
    source_stats: list[dict[str, JsonValue]] = []
    with ExitStack() as stack:
        archives: dict[str, ZipFile] = {}
        identities: dict[str, tuple[int, int, int, int]] = {}
        source_hashes: dict[str, str] = {}
        handles: dict[str, BinaryIO] = {}
        for item in request.workbooks:
            source = stack.enter_context(item.path.open("rb"))
            identity = _source_identity(source)
            sha256 = _source_sha256(source)
            archive = stack.enter_context(ZipFile(source))
            handles[item.alias] = source
            identities[item.alias] = identity
            source_hashes[item.alias] = sha256
            archives[item.alias] = archive
            source_stats.append(
                {
                    "alias": item.alias,
                    "path": str(item.path.resolve()),
                    "size": identity[2],
                    "mtime_ns": identity[3],
                    "sha256": sha256,
                }
            )
        for query in request.queries:
            workbook = workbook_inputs.get(query.workbook)
            if workbook is None:
                raise WorkflowQueryError("unknown_workbook", query.workbook)
            results[query.alias] = _resolve_query(
                query, workbook.path, archives[query.workbook], request.limits
            )
        for item in request.workbooks:
            source = handles[item.alias]
            if _source_identity(source) != identities[item.alias]:
                raise WorkflowQueryError("source_changed", item.alias)
            if _source_sha256(source) != source_hashes[item.alias]:
                raise WorkflowQueryError("source_changed", item.alias)
            path_stat = item.path.stat()
            identity = identities[item.alias]
            if (
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_size,
                path_stat.st_mtime_ns,
            ) != identity:
                raise WorkflowQueryError("source_changed", item.alias)
    facts = {item.id: item for item in request.projection.items}
    patches: list[PublicTextPatchItem] = []
    requests: list[TextPatchRequest] = []
    live_formatting = (
        None if request.formatting is None else request.formatting.to_live()
    )
    for binding in request.bindings:
        fact = facts.get(binding.target_id)
        if fact is None:
            raise WorkflowQueryError("unknown_target_id", binding.target_id)
        result = results.get(binding.query)
        if result is None:
            raise WorkflowQueryError("unknown_query", binding.query)
        values = dict(result.values)
        if binding.column not in values:
            raise WorkflowQueryError("unknown_column", binding.column)
        replacement = binding.replacement.replace("{value}", values[binding.column])
        if not replacement:
            raise WorkflowQueryError("empty_replacement", binding.column)
        if replacement == fact.text:
            raise WorkflowQueryError("unchanged_replacement", binding.column)
        target = PublicTextPatchTarget.model_validate(fact.target.model_dump())
        patch = PublicTextPatchItem(
            target=target,
            expected_text=fact.text,
            replacement=replacement,
        )
        patches.append(patch)
        requests.append(
            TextPatchRequest(
                target=target.to_live(),
                expected_text=fact.text,
                replacement=replacement,
                formatting=live_formatting,
            )
        )
    for item in requests:
        validate_text_patch_request(item)
    try:
        canonical = canonical_prepared_text_patch_requests(tuple(requests))
    except HwpLiveError as error:
        raise WorkflowQueryError("intersecting_targets", str(error)) from error
    if canonical is None:
        raise WorkflowQueryError("semantic_target", "projection targets must be exact")
    by_request = {
        id(request_item): patch
        for request_item, patch in zip(requests, patches, strict=True)
    }
    ordered_patches = tuple(by_request[id(item)] for item in canonical)
    compiled = compile_text_patch_batch(canonical)
    document = request.projection.document
    payload: dict[str, JsonValue] = {
        "patches": [item.model_dump(mode="json") for item in ordered_patches],
        "expected_document_id": document.document_id,
        "expected_document_full_name": document.full_name,
        "expected_content_revision": document.content_revision,
    }
    if request.formatting is not None:
        payload["formatting"] = request.formatting.model_dump(
            mode="json", exclude_none=True
        )
    payload["document_path"] = request.document_path or document.full_name
    plan = _JSON_OBJECT.validate_python(
        {
            "sources": source_stats,
            "queries": [
                {
                    "alias": alias,
                    "workbook": result.workbook_alias,
                    "path": result.workbook_path,
                    "sheet": result.sheet,
                    "headers": list(result.headers),
                    # 병합이 설명하는 빈 머리글만 여기 실린다. headers 는 시트에
                    # 적힌 그대로이고, 채운 라벨은 그 출처(ref)와 붙어서 나간다.
                    "merged_headers": [
                        {"column": column + 1, "label": label, "merged_from": reference}
                        for column, label, reference in result.merged_headers
                    ],
                    "row": result.row,
                    "values": [list(item) for item in result.values],
                }
                for alias, result in sorted(results.items())
            ],
            "projection_state_token": document.state_token,
            "payload": payload,
            "item_count": len(ordered_patches),
            "command_count": len(compiled.commands),
            "transport_bytes": compiled.transport_bytes,
        }
    )
    plan_sha256 = hashlib.sha256(_canonical_json(plan).encode()).hexdigest()
    output = dict(plan)
    output["plan_sha256"] = plan_sha256
    if len(_canonical_json(output).encode()) > request.limits.output_utf8_bytes:
        raise WorkflowQueryError("output_limit", "output UTF-8 byte limit exceeded")
    return output


def _write_json(
    value: JsonValue | Mapping[str, JsonValue],
    out: Path | None = None,
) -> None:
    encoded = _canonical_json(value).encode()
    if len(encoded) > 7_000_000:
        encoded = b'{"error":{"code":"output_limit","message":"output limit exceeded"}}'
    if out is None:
        _ = sys.stdout.buffer.write(encoded)
        return
    # --out 을 준 호출은 stdout 을 아예 쓰지 않는다. 진행 요약이나 경고가
    # 섞여도 JSON 은 이 파일에 그대로 남는다.
    _ = out.write_bytes(encoded)


def _out_argument(arguments: list[str]) -> Path | None:
    """Read only ``--out`` and leave every other argument alone.

    The stdin path has always ignored its argv, so this uses
    ``parse_known_args``: adding a defensive output file must not start
    rejecting calls that used to run.
    """
    parser = argparse.ArgumentParser(prog="hwp_workflow_query.py", add_help=False)
    _ = parser.add_argument("--out", type=Path, default=None)
    parsed, _rest = parser.parse_known_args(arguments)
    return cast("Path | None", parsed.out)


def _read_xlsx_main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hwp_workflow_query.py read-xlsx")
    _ = parser.add_argument("--key", required=True)
    _ = parser.add_argument("--column", action="append", default=[])
    _ = parser.add_argument("--out", type=Path, default=None)
    _ = parser.add_argument("paths", nargs="+", type=Path)
    parsed = parser.parse_args(arguments)
    paths = cast(list[Path], parsed.paths)
    key = cast(str, parsed.key)
    columns = cast(list[str], parsed.column)
    out = cast("Path | None", parsed.out)
    try:
        output = read_workbook_rows(tuple(paths), key=key, columns=tuple(columns))
    except (ValueError, HwpLiveError, OSError, BadZipFile) as error:
        _write_json({"error": {"code": "invalid_request", "message": str(error)}}, out)
        return 2
    _write_json(output, out)
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "read-xlsx":
        return _read_xlsx_main(sys.argv[2:])
    out = _out_argument(sys.argv[1:])
    from hwp_workflow_query_contract import WorkflowQueryError, WorkflowRequest

    try:
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
        if len(raw) > _MAX_STDIN_BYTES:
            raise WorkflowQueryError("stdin_limit", "stdin UTF-8 byte limit exceeded")
        request = WorkflowRequest.model_validate_json(raw)
        if len(raw) > request.limits.stdin_utf8_bytes:
            raise WorkflowQueryError("stdin_limit", "stdin UTF-8 byte limit exceeded")
        output = build_workflow_plan(request)
    except (
        ValidationError,
        WorkflowQueryError,
        HwpLiveError,
        OSError,
        BadZipFile,
    ) as error:
        code = (
            error.code if isinstance(error, WorkflowQueryError) else "invalid_request"
        )
        _write_json({"error": {"code": code, "message": str(error)}}, out)
        return 2
    _write_json(output, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
