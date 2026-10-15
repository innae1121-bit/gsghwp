from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal, TypedDict, final

from anyio import to_thread
from pydantic import Field, JsonValue, TypeAdapter

from hwp_public_action_contract import (
    PublicOperationId,
    PublicTextPatchBatch,
    PublicTextPatchItem,
)
from hwp_public_contract import PublicActionResult
from hwp_workflow_query import build_workflow_plan, read_workbook_rows
from hwp_workflow_query_contract import WorkflowRequest


_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_PATCH_BATCH = TypeAdapter[tuple[PublicTextPatchItem, ...]](PublicTextPatchBatch)
type BatchMutation = Callable[..., Awaitable[PublicActionResult]]

_MAX_WORKBOOKS = 32
_MAX_DIRECTORY_CHILDREN = 4_096
_MAX_OUTPUT_UTF8_BYTES = 1_000_000


class _SourceProvenance(TypedDict):
    requested_path: str
    resolved_path: str
    resolution: Literal["exact", "unique_whitespace_equivalent"]
    discovery: Literal["source_file", "immediate_xlsx_child"]


def _whitespace_key(value: str) -> str:
    return "".join(value.split()).casefold()


def _resolve_source_path(
    requested: Path,
) -> tuple[Path, Literal["exact", "unique_whitespace_equivalent"]]:
    if requested.exists():
        return requested.resolve(), "exact"
    missing_parts: list[str] = []
    existing_parent = requested
    while not existing_parent.exists():
        if existing_parent == existing_parent.parent:
            raise ValueError(f"path_not_found: {requested}")
        missing_parts.append(existing_parent.name)
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir() or not missing_parts:
        raise ValueError(f"path_not_found: {requested}")
    corrected_name = missing_parts[-1]
    exact_tail = tuple(reversed(missing_parts[:-1]))
    children: list[Path] = []
    for index, child in enumerate(existing_parent.iterdir(), start=1):
        if index > _MAX_DIRECTORY_CHILDREN:
            raise ValueError("directory child enumeration limit exceeded")
        children.append(child)
    matches = tuple(
        candidate
        for child in children
        if _whitespace_key(child.name) == _whitespace_key(corrected_name)
        and (candidate := child.joinpath(*exact_tail)).exists()
    )
    if not matches:
        raise ValueError(f"path_not_found: {requested}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous_whitespace_path: {requested}")
    resolved_match = next(iter(matches))
    return resolved_match.resolve(), "unique_whitespace_equivalent"


def _discover_workbooks(
    source_paths: tuple[str, ...],
) -> tuple[tuple[Path, ...], tuple[_SourceProvenance, ...]]:
    workbooks: list[Path] = []
    provenance: list[_SourceProvenance] = []
    for requested_path in source_paths:
        requested = Path(requested_path)
        resolved, resolution = _resolve_source_path(requested)
        if resolved.is_file():
            if resolved.suffix.casefold() != ".xlsx":
                raise ValueError(f"source_not_xlsx: {requested}")
            candidates = (resolved,)
            discovery: Literal["source_file", "immediate_xlsx_child"] = "source_file"
        elif resolved.is_dir():
            children: list[Path] = []
            for index, child in enumerate(resolved.iterdir(), start=1):
                if index > _MAX_DIRECTORY_CHILDREN:
                    raise ValueError("directory child enumeration limit exceeded")
                children.append(child)
            candidates = tuple(
                sorted(
                    (
                        child.resolve()
                        for child in children
                        if child.is_file() and child.suffix.casefold() == ".xlsx"
                    ),
                    key=lambda child: (child.name.casefold(), child.name),
                )
            )
            discovery = "immediate_xlsx_child"
        else:
            raise ValueError(f"path_not_found: {requested}")
        if len(workbooks) + len(candidates) > _MAX_WORKBOOKS:
            raise ValueError("workbook limit exceeded")
        for workbook in candidates:
            workbooks.append(workbook)
            provenance.append(
                {
                    "requested_path": requested_path,
                    "resolved_path": str(workbook),
                    "resolution": resolution,
                    "discovery": discovery,
                }
            )
    if not workbooks:
        raise ValueError("path_not_found: no immediate .xlsx children")
    return tuple(workbooks), tuple(provenance)


@final
class HwpPublicXlsxTools:
    """Resolve workbook values and consume them without crossing the MCP boundary."""

    __slots__ = ("_patch_batch",)

    def __init__(self, patch_batch: BatchMutation) -> None:
        self._patch_batch = patch_batch

    async def hwp_read_workbook_rows(
        self,
        *,
        source_paths: Annotated[
            tuple[Annotated[str, Field(min_length=1, max_length=32_767)], ...],
            Field(min_length=1, max_length=32),
        ],
        key: Annotated[str, Field(min_length=1, max_length=200_000)],
        columns: Annotated[tuple[str, ...], Field(max_length=256)] = (),
        sheet_queries: Annotated[tuple[str, ...], Field(max_length=128)] = (),
    ) -> dict[str, JsonValue]:
        if any(not "".join(query.split()) for query in sheet_queries):
            raise ValueError("sheet_queries must not contain whitespace-only values")
        workbooks, provenance = await to_thread.run_sync(_discover_workbooks, source_paths)
        result = await to_thread.run_sync(
            lambda: read_workbook_rows(
                workbooks,
                key=key,
                columns=columns,
                sheet_queries=sheet_queries,
            )
        )
        response = _JSON_OBJECT.validate_python({**result, "sources": list(provenance)})
        response_bytes = json.dumps(
            response, ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(response_bytes) > _MAX_OUTPUT_UTF8_BYTES:
            raise ValueError("output UTF-8 byte limit exceeded")
        return response

    async def hwp_patch_text_from_xlsx(
        self,
        *,
        operation_id: PublicOperationId,
        workflow: WorkflowRequest,
    ) -> PublicActionResult:
        plan = build_workflow_plan(workflow)
        payload = _JSON_OBJECT.validate_python(plan["payload"])
        patches = _PATCH_BATCH.validate_python(payload["patches"])
        document = workflow.projection.document
        return await self._patch_batch(
            operation_id=operation_id,
            patches=patches,
            formatting=workflow.formatting,
            document_path=workflow.document_path or document.full_name,
            expected_document_id=document.document_id,
            expected_document_full_name=document.full_name,
            expected_content_revision=document.content_revision,
        )
