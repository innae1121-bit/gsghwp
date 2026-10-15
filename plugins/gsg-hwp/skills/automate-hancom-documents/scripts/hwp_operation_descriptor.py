from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from hwp_operation_route_contract import HwpWorkflowId


OperationVerificationMode = Literal[
    "native_action_result",
    "native_routing_context",
    "native_snapshot_before_after",
    "native_detailed_structure_before_after",
    "native_structure_no_change",
    "native_operation_specific_readback",
    "native_save_result",
    "native_save_reopen_result",
]
ATOMIC_ACTION_VERIFICATION_MODES: Final[tuple[OperationVerificationMode, ...]] = (
    "native_snapshot_before_after",
)
DescriptorExecution = Literal["recipe", "official", "catalog_only"]
DescriptorAtomicity = Literal["native_batch", "transactional"]


class OperationDescriptorError(RuntimeError):
    workflow_id: HwpWorkflowId
    tool_name: str

    def __init__(self, workflow_id: HwpWorkflowId, tool_name: str) -> None:
        super().__init__(
            f"{workflow_id} operation descriptor has an invalid adapter: {tool_name}"
        )
        self.workflow_id = workflow_id
        self.tool_name = tool_name


@dataclass(frozen=True, slots=True)
class OperationRecipeDescriptor:
    recipe_id: str
    version: int
    steps: tuple[str, ...]
    atomicity: DescriptorAtomicity


@dataclass(frozen=True, slots=True)
class OperationDescriptor:
    workflow_id: HwpWorkflowId
    execution: DescriptorExecution
    routing_steps: tuple[str, ...]
    input_schema: tuple[str, ...]
    target_kinds: frozenset[str] = frozenset()
    recipe: OperationRecipeDescriptor | None = None
    public_tools: tuple[str, ...] = ()
    verification_modes: tuple[OperationVerificationMode, ...] = ()
    mutation: bool = True
    adapter_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for adapter in self.adapter_tools:
            if adapter not in self.public_tools:
                raise OperationDescriptorError(self.workflow_id, adapter)
        if self.public_tools and self.public_tools[0] in self.adapter_tools:
            raise OperationDescriptorError(self.workflow_id, self.public_tools[0])


def _recipe(
    recipe_id: str,
    steps: tuple[str, ...],
    atomicity: DescriptorAtomicity = "native_batch",
) -> OperationRecipeDescriptor:
    return OperationRecipeDescriptor(recipe_id, 1, steps, atomicity)


_DESCRIPTORS: Final = (
    OperationDescriptor(
        "document.inspect_structure",
        "recipe",
        ("InspectStructure",),
        (),
        public_tools=("hwp_inspect_structure",),
        verification_modes=("native_routing_context",),
        mutation=False,
    ),
    OperationDescriptor(
        "document.delete_page",
        "recipe",
        ("ResolvePage", "DeletePage", "VerifyStructure"),
        ("inputs.target",),
        frozenset(("page",)),
        _recipe(
            "document.delete_page.v1",
            ("ResolvePage", "DeletePage", "VerifyStructure"),
        ),
        ("hwp_delete_page",),
        ("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "document.undo",
        "recipe",
        ("ResolveHistory", "Undo", "VerifySnapshot"),
        (),
        recipe=_recipe(
            "document.undo.v1",
            ("ResolveHistory", "Undo", "VerifySnapshot"),
        ),
        public_tools=("hwp_undo",),
        verification_modes=("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "document.redo",
        "recipe",
        ("ResolveHistory", "Redo", "VerifySnapshot"),
        (),
        recipe=_recipe(
            "document.redo.v1",
            ("ResolveHistory", "Redo", "VerifySnapshot"),
        ),
        public_tools=("hwp_redo",),
        verification_modes=("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "control.delete",
        "recipe",
        ("ResolveControl", "DeleteControl", "VerifyStructure"),
        ("inputs.target",),
        frozenset(("control",)),
        _recipe(
            "control.delete.v1",
            ("ResolveControl", "DeleteControl", "VerifyStructure"),
        ),
        ("hwp_delete_control",),
        ("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "document.append_layout",
        "recipe",
        ("MoveDocEnd", "ApplyLayout"),
        ("inputs.layout",),
        recipe=_recipe(
            "page.append_from_template.v1",
            ("MoveDocEnd", "ApplyLayout"),
            "transactional",
        ),
        public_tools=(
            "hwp_append_layout",
            "hwp_append_report",
            "hwp_append_excel_table",
        ),
        verification_modes=("native_snapshot_before_after",),
        adapter_tools=("hwp_append_report", "hwp_append_excel_table"),
    ),
    OperationDescriptor(
        "document.insert_layout",
        "recipe",
        ("ApplyLayout",),
        ("inputs.layout",),
        recipe=_recipe("document.insert_layout.v1", ("ApplyLayout",)),
        public_tools=("hwp_insert_layout",),
        verification_modes=("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "document.replace_selection",
        "recipe",
        ("ResolveTextPatch", "PatchText", "VerifyTextPatch"),
        ("inputs.target", "inputs.data"),
        frozenset(("selection",)),
        _recipe(
            "document.replace_selection.v1",
            ("ResolveTextPatch", "PatchText", "VerifyTextPatch"),
        ),
        ("hwp_replace_selected_text",),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "text.replace",
        "recipe",
        ("ResolveTextPatch", "PatchText", "VerifyTextPatch"),
        ("inputs.target", "inputs.data"),
        frozenset(("selection", "range", "find", "table_cell")),
        _recipe(
            "text.replace.v1",
            ("ResolveTextPatch", "PatchText", "VerifyTextPatch"),
        ),
        (
            "hwp_patch_text",
            "hwp_patch_text_batch",
            "hwp_patch_text_from_xlsx",
        ),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "text.insert",
        "recipe",
        ("ResolveTextPatch", "PatchText", "VerifyTextPatch"),
        ("inputs.data",),
        frozenset(("cursor", "selection")),
        _recipe(
            "text.insert.v1",
            ("ResolveTextPatch", "PatchText", "VerifyTextPatch"),
        ),
        ("hwp_patch_text",),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "text.format",
        "recipe",
        ("ResolveTextRange", "ApplyTextFormat"),
        ("inputs.target", "inputs.parameters"),
        frozenset(("selection",)),
        _recipe(
            "text.format.v1",
            ("ResolveTextRange", "ApplyTextFormat"),
        ),
        ("hwp_format_text",),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "table.fill_existing",
        "recipe",
        ("ResolveTable", "MapRowsToCells", "FillCells", "VerifyStructure"),
        ("inputs.target", "inputs.data"),
        frozenset(("table",)),
        _recipe(
            "table.fill_existing.v1",
            ("ResolveTable", "MapRowsToCells", "FillCells", "VerifyStructure"),
        ),
        ("hwp_fill_table",),
        ("native_snapshot_before_after", "native_structure_no_change"),
    ),
    OperationDescriptor(
        "table.expand_and_fill",
        "recipe",
        (
            "ResolveTable",
            "ExpandRows",
            "MapRowsToCells",
            "FillCells",
            "VerifyStructure",
        ),
        ("inputs.target", "inputs.data"),
        frozenset(("table",)),
        _recipe(
            "table.expand_and_fill.v1",
            (
                "ResolveTable",
                "ExpandRows",
                "MapRowsToCells",
                "FillCells",
                "VerifyStructure",
            ),
        ),
        ("hwp_expand_and_fill_table",),
        ("native_snapshot_before_after", "native_structure_no_change"),
    ),
    OperationDescriptor(
        "table.repeat_template",
        "recipe",
        ("ResolveTable", "RepeatTemplate", "VerifyStructure"),
        ("inputs.recipe",),
        frozenset(("table",)),
        _recipe(
            "table.repeat_template.v1",
            ("ResolveTable", "RepeatTemplate", "VerifyStructure"),
        ),
        ("hwp_repeat_table_template",),
        ("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "table.build_series",
        "recipe",
        (
            "ResolveTable",
            "RepeatTemplate",
            "FillCells",
            "InsertImages",
            "VerifyStructure",
        ),
        ("inputs.recipe",),
        frozenset(("table",)),
        _recipe(
            "table.build_series.v1",
            (
                "ResolveTable",
                "RepeatTemplate",
                "FillCells",
                "InsertImages",
                "VerifyStructure",
            ),
        ),
        ("hwp_build_table_series", "hwp_sync_visibility_analysis_tables"),
        ("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "table.insert_images",
        "recipe",
        ("ResolveTable", "MapImagesToCells", "InsertImages", "VerifyStructure"),
        ("inputs.target", "inputs.assets"),
        frozenset(("table",)),
        _recipe(
            "table.fill_with_images.v1",
            (
                "ResolveTable",
                "MapImagesToCells",
                "InsertImages",
                "VerifyStructure",
            ),
        ),
        ("hwp_fill_table_images", "hwp_insert_table_images"),
        ("native_snapshot_before_after", "native_structure_no_change"),
    ),
    OperationDescriptor(
        "image.insert",
        "recipe",
        ("ResolvePosition", "InsertImages", "VerifyStructure"),
        ("inputs.assets",),
        recipe=_recipe(
            "image.insert_or_replace.v1",
            ("ResolvePosition", "InsertImages", "VerifyStructure"),
        ),
        public_tools=("hwp_insert_image",),
        verification_modes=(
            "native_snapshot_before_after",
            "native_detailed_structure_before_after",
        ),
    ),
    OperationDescriptor(
        "image.replace",
        "recipe",
        ("ResolvePicture", "PictureChange", "VerifyStructure"),
        ("inputs.target", "inputs.assets"),
        frozenset(("picture",)),
        _recipe(
            "image.insert_or_replace.v1",
            ("ResolvePicture", "PictureChange", "VerifyStructure"),
        ),
        ("hwp_replace_image",),
        ("native_snapshot_before_after",),
    ),
    OperationDescriptor(
        "caption.add",
        "recipe",
        ("ResolveControl", "ApplyCaption", "VerifyStructure"),
        ("inputs.target", "inputs.recipe"),
        frozenset(("table", "control")),
        _recipe(
            "caption.add_or_update.v1",
            ("ResolveControl", "ApplyCaption", "VerifyStructure"),
        ),
        ("hwp_add_caption",),
        (
            "native_snapshot_before_after",
            "native_detailed_structure_before_after",
        ),
    ),
    OperationDescriptor(
        "style.copy",
        "recipe",
        ("ResolvePosition", "CopyAndApplyStyle", "VerifyStructure"),
        ("inputs.recipe",),
        recipe=_recipe(
            "style.copy_and_apply.v1",
            ("ResolvePosition", "CopyAndApplyStyle", "VerifyStructure"),
        ),
        public_tools=("hwp_copy_style",),
        verification_modes=(
            "native_snapshot_before_after",
            "native_operation_specific_readback",
        ),
    ),
    OperationDescriptor(
        "style.apply",
        "recipe",
        ("ResolvePosition", "ApplyStyle", "VerifyStructure"),
        ("inputs.recipe",),
        recipe=_recipe(
            "style.copy_and_apply.v1",
            ("ResolvePosition", "ApplyStyle", "VerifyStructure"),
        ),
        public_tools=("hwp_apply_style",),
        verification_modes=(
            "native_snapshot_before_after",
            "native_operation_specific_readback",
        ),
    ),
    OperationDescriptor(
        "table.format",
        "recipe",
        ("ResolveTable", "ApplyTableFormat", "VerifyStructure"),
        ("inputs.target", "inputs.parameters"),
        frozenset(("table",)),
        _recipe(
            "table.format.v1",
            ("ResolveTable", "ApplyTableFormat", "VerifyStructure"),
        ),
        ("hwp_format_table", "hwp_set_table_size_fixed"),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "table.merge_cells",
        "recipe",
        ("ResolveTable", "ResolveCells", "TableMergeCell", "VerifyStructure"),
        ("inputs.target",),
        frozenset(("table",)),
        _recipe(
            "table.merge_cells.v1",
            (
                "ResolveTable",
                "ResolveCells",
                "TableMergeCell",
                "VerifyStructure",
            ),
        ),
        ("hwp_merge_table_cells",),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "table.split_cells",
        "recipe",
        ("ResolveTable", "ResolveCells", "TableSplitCell", "VerifyStructure"),
        ("inputs.target", "inputs.parameters"),
        frozenset(("table",)),
        _recipe(
            "table.split_cells.v1",
            (
                "ResolveTable",
                "ResolveCells",
                "TableSplitCell",
                "VerifyStructure",
            ),
        ),
        ("hwp_split_table_cell",),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "document.navigate",
        "official",
        ("ResolveNavigation", "RunAction"),
        (),
        mutation=False,
    ),
    OperationDescriptor(
        "document.rebuild",
        "recipe",
        ("InspectStructure", "RebuildDocument", "VerifyStructure"),
        ("inputs.layout",),
    ),
    OperationDescriptor(
        "document.page_break",
        "official",
        ("BreakPage",),
        (),
    ),
    OperationDescriptor(
        "document.insert_page",
        "recipe",
        ("ResolvePosition", "InsertPageLayout"),
        ("inputs.target", "inputs.layout"),
    ),
    OperationDescriptor(
        "table.inspect",
        "recipe",
        ("InspectPage", "ResolveTable"),
        ("inputs.target",),
        mutation=False,
    ),
    OperationDescriptor(
        "table.create",
        "recipe",
        ("ResolvePosition", "TableCreate", "VerifyTable"),
        ("inputs.target", "inputs.layout"),
    ),
    OperationDescriptor(
        "table.resize",
        "recipe",
        ("ResolveTable", "ResizeTable", "VerifyStructure"),
        ("inputs.target", "inputs.data"),
    ),
    OperationDescriptor(
        "table.propagate",
        "recipe",
        ("ResolveSourceTable", "ResolveTargetTables", "PropagateCells"),
        ("inputs.target", "inputs.data"),
    ),
    OperationDescriptor(
        "table.import_data",
        "recipe",
        ("ReadDataSource", "ResolveTable", "MapRowsToCells", "FillCells"),
        ("inputs.target", "inputs.data"),
    ),
    OperationDescriptor(
        "image.resize",
        "recipe",
        ("ResolvePicture", "PictureChange", "VerifyStructure"),
        ("inputs.target",),
        frozenset(("picture",)),
        _recipe(
            "image.edit.v1",
            ("ResolvePicture", "PictureChange", "VerifyStructure"),
        ),
        ("hwp_edit_picture", "hwp_copy_picture"),
        ("native_operation_specific_readback",),
    ),
    OperationDescriptor(
        "hyperlink.modify",
        "official",
        ("ResolveHyperlink", "ModifyHyperlink"),
        (),
    ),
    OperationDescriptor(
        "document.save",
        "recipe",
        ("SaveDocument", "VerifySavedDocument"),
        (),
        recipe=_recipe(
            "document.save.v1",
            ("SaveDocument", "VerifySavedDocument"),
        ),
        public_tools=("hwp_save",),
        verification_modes=("native_save_result",),
    ),
    OperationDescriptor(
        "document.save_reopen_verify",
        "recipe",
        ("SaveDocument", "ReopenDiagnostic", "VerifySavedDocument"),
        (),
        recipe=_recipe(
            "document.save_reopen_verify.v1",
            ("SaveDocument", "ReopenDiagnostic", "VerifySavedDocument"),
        ),
        public_tools=("hwp_save_reopen_verify",),
        verification_modes=("native_save_reopen_result",),
    ),
)

_BY_WORKFLOW: Final = MappingProxyType(
    {descriptor.workflow_id: descriptor for descriptor in _DESCRIPTORS}
)
_BY_TOOL: Final = MappingProxyType(
    {
        tool: tuple(
            descriptor for descriptor in _DESCRIPTORS if tool in descriptor.public_tools
        )
        for tool in {
            tool for descriptor in _DESCRIPTORS for tool in descriptor.public_tools
        }
    }
)
_ADAPTER_TARGET_BY_TOOL: Final = MappingProxyType(
    {
        adapter: descriptor.public_tools[0]
        for descriptor in _DESCRIPTORS
        for adapter in descriptor.adapter_tools
    }
)


def operation_descriptors() -> tuple[OperationDescriptor, ...]:
    return _DESCRIPTORS


def operation_descriptor(
    workflow_id: HwpWorkflowId,
) -> OperationDescriptor | None:
    return _BY_WORKFLOW.get(workflow_id)


def descriptor_workflows_for_tool(
    tool_name: str,
) -> tuple[HwpWorkflowId, ...]:
    return tuple(descriptor.workflow_id for descriptor in _BY_TOOL.get(tool_name, ()))


def descriptor_adapter_target(tool_name: str) -> str | None:
    return _ADAPTER_TARGET_BY_TOOL.get(tool_name)
