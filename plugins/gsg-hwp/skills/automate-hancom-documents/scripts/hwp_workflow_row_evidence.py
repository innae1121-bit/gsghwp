from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from hwp_office_xlsx import XlsxMerge, XlsxRow

_DATA_NUMBER_TRANSLATION: Final = str.maketrans("", "", ",.()%+-")
_UNIT_LABEL: Final = re.compile(r"^\s*(?:단위|unit)\s*[:：]", re.IGNORECASE)
_NOTE_LABEL: Final = re.compile(
    r"^\s*(?:자료|출처|주|source|note)\s*[:：]", re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True, slots=True)
class WorksheetRows:
    sheet: str
    rows: tuple[XlsxRow, ...]
    merges: tuple[XlsxMerge, ...]
    column_limit: int


@dataclass(frozen=True, slots=True)
class HeaderSource:
    row: int
    column: int
    text: str
    merged_from: str | None = None

    def to_json(self) -> Mapping[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "row": self.row,
            "column": self.column,
            "text": self.text,
        }
        if self.merged_from is not None:
            value["merged_from"] = self.merged_from
        return value


def _normalized_district_key(value: str) -> str:
    normalized = "".join(value.split())
    return normalized[:-1] if normalized.endswith("구") else normalized


def _looks_like_data_row(row: XlsxRow) -> bool:
    values = tuple(value for _, value in row.values if value.strip())
    numeric = sum(
        not (candidate := "".join(value.split()).translate(_DATA_NUMBER_TRANSLATION))
        or candidate.isdecimal()
        for value in values
    )
    return bool(values) and numeric * 2 >= len(values)


def _header_rows(
    prior: list[XlsxRow], key_columns: frozenset[int]
) -> tuple[XlsxRow, ...]:
    first_data = next(
        (
            row.index
            for row in prior
            if any(
                value.strip() for column, value in row.values if column in key_columns
            )
            and _looks_like_data_row(row)
        ),
        None,
    )
    selected: list[XlsxRow] = []
    for row in reversed(prior):
        if not any(value.strip() for _, value in row.values):
            continue
        has_row_key = any(
            value.strip() for column, value in row.values if column in key_columns
        )
        data_started = first_data is not None and row.index >= first_data
        if _looks_like_data_row(row) and (has_row_key or data_started):
            if selected:
                break
            continue
        if any(_UNIT_LABEL.match(value) for _, value in row.values):
            break
        selected.append(row)
    return tuple(reversed(selected))


def _unit_sources(prior: list[XlsxRow]) -> tuple[HeaderSource, ...]:
    for row in reversed(prior):
        sources = tuple(
            HeaderSource(row.index, column + 1, value)
            for column, value in row.values
            if _UNIT_LABEL.match(value)
        )
        if sources:
            return sources
    return ()


def _column_headers(
    column: int, rows: tuple[XlsxRow, ...], merges: tuple[XlsxMerge, ...]
) -> tuple[HeaderSource, ...]:
    sources: dict[tuple[int, int], HeaderSource] = {}
    row_values = {row.index: dict(row.values) for row in rows}
    for row in rows:
        source_row, source_column = row.index, column
        text = row_values[row.index].get(column, "")
        merge = next(
            (
                item
                for item in merges
                if item.first_row <= row.index - 1 <= item.last_row
                and item.first_column <= column <= item.last_column
            ),
            None,
        )
        if not text.strip() and merge is not None:
            source_row, source_column = merge.first_row + 1, merge.first_column
            text = row_values.get(source_row, {}).get(source_column, "")
        if text.strip():
            sources[(source_row, source_column)] = HeaderSource(
                source_row,
                source_column + 1,
                text,
                None if merge is None else merge.ref,
            )
    return tuple(sources.values())


def worksheet_matches(
    worksheet: WorksheetRows, key: str, columns: tuple[str, ...]
) -> list[JsonValue]:
    prior: list[XlsxRow] = []
    headers: tuple[XlsxRow, ...] | None = None
    units: tuple[HeaderSource, ...] = ()
    matches: list[JsonValue] = []
    normalized_key = _normalized_district_key(key)
    notes: list[JsonValue] = [
        dict(HeaderSource(row.index, column + 1, value).to_json())
        for row in worksheet.rows
        for column, value in row.values
        if _NOTE_LABEL.search(value)
    ]
    for row in worksheet.rows:
        if not any(
            _normalized_district_key(value) == normalized_key for _, value in row.values
        ):
            prior.append(row)
            continue
        if headers is None:
            key_columns = frozenset(
                column
                for column, value in row.values
                if _normalized_district_key(value) == normalized_key
            )
            headers = _header_rows(prior, key_columns)
            units = _unit_sources(prior)
        sparse = dict(row.values)
        last_column = min(
            worksheet.column_limit - 1,
            max(
                max(
                    (
                        column
                        for source in (*headers, row)
                        for column, _ in source.values
                    ),
                    default=-1,
                ),
                max(
                    (
                        merge.last_column
                        for merge in worksheet.merges
                        if any(
                            merge.first_row <= source.index - 1 <= merge.last_row
                            for source in (*headers, row)
                        )
                    ),
                    default=-1,
                ),
            ),
        )
        values: dict[str, JsonValue] = {}
        ordered_values: list[JsonValue] = []
        details: list[JsonValue] = []
        header_ids: dict[HeaderSource, int] = {}
        merged_labels: list[JsonValue] = []
        duplicate_label = False
        for column in range(last_column + 1):
            sources = _column_headers(column, headers, worksheet.merges)
            label = sources[-1].text if sources else f"column_{column + 1}"
            if columns and label not in columns:
                continue
            value = sparse.get(column, "")
            for source in sources:
                if source not in header_ids:
                    header_ids[source] = len(header_ids) + 1
            details.append(
                {
                    "column": column + 1,
                    "value": value,
                    "header_refs": [header_ids[source] for source in sources],
                }
            )
            if not value:
                continue
            duplicate_label = duplicate_label or label in values
            values[label] = value
            ordered_values.append([label, value])
            inherited = sources[-1] if sources else None
            if (
                inherited is not None
                and inherited.column != column + 1
                and inherited.merged_from is not None
            ):
                merged_labels.append(
                    {
                        "column": column + 1,
                        "label": label,
                        "merged_from": inherited.merged_from,
                    }
                )
        match: dict[str, JsonValue] = {
            "sheet": worksheet.sheet,
            "row": row.index,
            "values": values,
            "values_ambiguous": duplicate_label,
            "coordinates_authoritative": True,
            "header_rows": [header.index for header in headers],
            "header_sources": [dict(source.to_json()) for source in header_ids],
            "column_details": details,
            "unit_evidence": [dict(source.to_json()) for source in units],
            "note_evidence": notes,
        }
        if duplicate_label:
            match["ordered_values"] = ordered_values
        if merged_labels:
            match["merged_labels"] = merged_labels
        matches.append(match)
        prior.append(row)
    return matches
