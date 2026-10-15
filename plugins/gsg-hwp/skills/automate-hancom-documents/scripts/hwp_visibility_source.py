from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from hwp_live_structure_contract import StructureCell, StructureTable
from hwp_visibility_series_contract import (
    CIRCLED_NUMBERS,
    VisibilityRecord,
    VisibilitySeriesPlanError,
)
from hwp_visibility_table_cells import (
    compact_cell_text,
    logical_cells,
    logical_value,
    owner_cells,
)


_IMAGE_EXTENSIONS: Final = frozenset(
    (".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp")
)
_VIEWPOINT_NUMBER: Final = re.compile(r"(?:(?:예비)?조망점)?([0-9]+)")


def extract_visibility_records(table: StructureTable) -> tuple[VisibilityRecord, ...]:
    owners = owner_cells(table)
    numbered_by_column: dict[int, list[tuple[StructureCell, int]]] = {}
    for cell in owners:
        match = _VIEWPOINT_NUMBER.fullmatch(compact_cell_text(cell.text))
        if (
            match is None
            or cell.column < 1
            or cell.column + 3 >= table.columns
        ):
            continue
        numbered_by_column.setdefault(cell.column, []).append(
            (cell, int(match.group(1)))
        )
    if not numbered_by_column:
        raise VisibilitySeriesPlanError(
            "원본 표에서 번호 열과 뒤따르는 세 값 열을 추론하지 못했습니다"
        )
    largest_group = max(len(items) for items in numbered_by_column.values())
    number_column = min(
        column
        for column, items in numbered_by_column.items()
        if len(items) == largest_group
    )
    category_column = number_column - 1
    location_column = number_column + 1
    distance_column = number_column + 2
    elevation_column = number_column + 3
    logical = logical_cells(table)
    numbered_cells = tuple(numbered_by_column[number_column])
    records: list[VisibilityRecord] = []
    seen: set[int] = set()
    for cell, number in sorted(numbered_cells, key=lambda item: item[0].row):
        if number < 1 or number > len(CIRCLED_NUMBERS):
            raise VisibilitySeriesPlanError(
                f"조망점 번호는 1~{len(CIRCLED_NUMBERS)}여야 합니다: {number}"
            )
        if number in seen:
            raise VisibilitySeriesPlanError(f"조망점 {number}번이 중복됩니다")
        seen.add(number)
        records.append(
            VisibilityRecord(
                number=number,
                category=logical_value(logical, cell.row, category_column, "구분"),
                location=logical_value(
                    logical, cell.row, location_column, "조망위치"
                ),
                distance=logical_value(
                    logical, cell.row, distance_column, "이격거리"
                ),
                elevation=logical_value(
                    logical, cell.row, elevation_column, "표고"
                ),
            )
        )
    if not records:
        raise VisibilitySeriesPlanError(
            "원본 표에서 숫자 조망점 레코드를 찾지 못했습니다"
        )
    return tuple(records)


def numbered_image(folder: Path, number: int) -> Path:
    path = optional_numbered_image(folder, number)
    if path is None:
        if not folder.is_dir():
            raise VisibilitySeriesPlanError(f"사진 폴더가 없습니다: {folder}")
        raise VisibilitySeriesPlanError(f"{number}번 사진을 찾지 못했습니다: {folder}")
    return path


def optional_numbered_image(folder: Path, number: int) -> Path | None:
    if not folder.is_dir():
        return None
    matches: list[Path] = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.casefold() not in _IMAGE_EXTENSIONS:
            continue
        numbers = (
            int(match.group(1))
            for match in re.finditer(r"(?<![0-9])0*([0-9]+)(?![0-9])", path.stem)
        )
        if number in numbers:
            matches.append(path.resolve())
    if not matches:
        return None
    if len(matches) > 1:
        raise VisibilitySeriesPlanError(
            f"{number}번 사진이 여러 개입니다: {tuple(path.name for path in matches)}"
        )
    return matches[0]
