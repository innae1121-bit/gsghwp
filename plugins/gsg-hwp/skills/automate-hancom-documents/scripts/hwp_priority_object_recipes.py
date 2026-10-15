from __future__ import annotations

from dataclasses import replace

from hwp_errors import HwpLiveError
from hwp_image_fit import (
    cover_crop_in_box,
    fit_cropped_image_in_box,
    fit_image_in_box,
)
from hwp_live_api import LiveHwpApplication
from hwp_live_native_action_models import (
    BooleanValue,
    CaptionCommand,
    CaptureTableCommand,
    InsertPictureCommand,
    IntegerValue,
    MillimeterValue,
    MoveDocumentEndCommand,
    MovePositionCommand,
    NativeActionCommand,
    NativeDetailedCaption,
    NativeDetailedControl,
    NativePageInspection,
    NativePosition,
    NativeSnapshot,
    NativeSetter,
    ParameterActionCommand,
    PictureCrop,
    RunCommand,
    SelectControlCommand,
    TextValue,
)
from hwp_live_native_batch import (
    inspect_native_page,
    inspect_native_structure,
    read_native_paragraph_styles,
    read_native_snapshot,
)
from hwp_live_native_template_repeat import (
    caption_literal_text,
    replace_table_caption_commands,
)
from hwp_live_native_text_format import style_command
from hwp_live_rot import HwpDocumentCandidate
from hwp_object_control_types import (
    CAPTIONABLE_CONTROL_TYPES,
    PICTURE_CONTROL_TYPES,
    is_picture_control_type,
    is_table_control_type,
)
from hwp_operation_contract import (
    HwpOperateAssets,
    HwpOperateTarget,
    HwpWorkflowId,
    OperationResult,
    WorkflowResolution,
)
from hwp_priority_object_inputs import (
    ControlTargetFailure,
    ControlTargetRequest,
    ResolvedObjectControl,
    native_position,
    resolve_control_target,
    resolve_style_position,
    single_image,
)
from hwp_priority_object_runtime import (
    PictureCaptionExpectation,
    PictureCaptionProbe,
    PictureInsertExpectation,
    PictureReplaceExpectation,
    execute_object_commands as _execute,
    object_recipe_result as _result,
    picture_caption_commands as build_picture_caption_commands,
    probe_picture_caption,
    verify_inserted_picture,
    verify_picture_caption,
    verify_replaced_picture,
)
from hwp_priority_recipe_contract import HwpPriorityRecipeInputs


_OBJECT_WORKFLOWS = frozenset[HwpWorkflowId](
    ("image.insert", "image.replace", "caption.add", "style.copy", "style.apply")
)


def _style_verification_scope_is_sufficient(
    recipe_inputs: HwpPriorityRecipeInputs,
    before: NativeSnapshot | None,
) -> bool:
    if recipe_inputs.target_position is not None:
        return True
    if before is None:
        return False
    selection = before.selection
    return (
        selection.selected
        and selection.base_mode == 1
        and selection.start.list_id == selection.end.list_id
        and selection.start.paragraph == selection.end.paragraph
    )


def _selection_snapshot(
    candidate: HwpDocumentCandidate,
    target: HwpOperateTarget | None,
) -> NativeSnapshot | None:
    if target is None or target.binding != "selection":
        return None
    snapshot = read_native_snapshot(candidate.window_handle)
    if snapshot is None:
        raise HwpLiveError("한컴 네이티브 선택 상태를 읽지 못했습니다")
    return snapshot


def _numbered_shape(
    candidate: HwpDocumentCandidate, snapshot: NativeSnapshot
) -> NativeSnapshot:
    scan = read_native_paragraph_styles(
        candidate.window_handle,
        list_id=snapshot.cursor.list_id,
        start=snapshot.cursor.paragraph,
        limit=1,
        paragraph_shape=True,
    )
    if (
        scan is None
        or scan.document_id != snapshot.document_id
        or scan.full_name != snapshot.full_name
        or scan.list_id != snapshot.cursor.list_id
    ):
        return snapshot
    observed = next(
        (p for p in scan.paragraphs if p.paragraph == snapshot.cursor.paragraph), None
    )
    if observed is None or observed.heading_type is None:
        return snapshot
    return replace(
        snapshot,
        paragraph_format=replace(
            snapshot.paragraph_format,
            heading_type=observed.heading_type,
            heading_level=observed.heading_level,
            marker_is_automatic=observed.heading_type > 0,
        ),
    )


def _verify_copied_shape(
    source: NativeSnapshot, after: NativeSnapshot, copy_type: int
) -> bool:
    """Verify observed shape, including automatic paragraph-numbering status.

    The display number is intentionally not compared: following paragraphs
    must advance through HWP's sequence rather than inherit literal digits.
    """
    if copy_type not in (0, 1, 2) or not after.selection.selected:
        return False
    if copy_type in (0, 2) and source.character_format != after.character_format:
        raise HwpLiveError("모양 복사 후 원본과 대상의 글자 모양이 일치하지 않습니다")
    if copy_type in (1, 2) and source.paragraph_format != after.paragraph_format:
        raise HwpLiveError(
            "모양 복사 후 문단 모양 또는 자동번호 설정이 일치하지 않습니다"
        )
    return True


def operate_object_recipe(
    candidate: HwpDocumentCandidate,
    hwp: LiveHwpApplication,
    routing_page: NativePageInspection,
    resolution: WorkflowResolution,
    target: HwpOperateTarget | None,
    assets: HwpOperateAssets | None,
    recipe_inputs: HwpPriorityRecipeInputs | None,
    *,
    resolve_only: bool,
    allow_document_change: bool,
) -> OperationResult | None:
    _ = hwp
    workflow = resolution.workflow_id
    if workflow not in _OBJECT_WORKFLOWS:
        return None
    if resolve_only:
        return _result(
            resolution, "resolved", "인증된 프로토콜 9 개체 recipe를 확정했습니다"
        )
    if not allow_document_change:
        return _result(
            resolution, "confirmation_required", "문서 개체를 변경하는 작업입니다"
        )
    commands: tuple[NativeActionCommand, ...]
    caption_text: str | None = None
    caption_control_id: str | None = None
    caption_before: NativeDetailedCaption | None = None
    picture_caption_expectation: PictureCaptionExpectation | None = None
    picture_caption_probe: PictureCaptionProbe | None = None
    picture_caption_commands: tuple[NativeActionCommand, ...] | None = None
    resolved_target_id: str | None = None
    target_resolution_basis: str | None = None
    image_before_controls: tuple[NativeDetailedControl, ...] = ()
    image_before_page: int | None = None
    image_document_end = False
    image_fitted_width_mm: float | None = None
    image_fitted_height_mm: float | None = None
    image_uncropped_width_mm: float | None = None
    image_uncropped_height_mm: float | None = None
    image_crop: PictureCrop | None = None
    image_replace_before: NativeDetailedControl | None = None
    image_replace_expectation: PictureReplaceExpectation | None = None
    resolved_control_page: NativePageInspection | None = None
    style_expected_id: int | None = None
    copied_shape_source: NativeSnapshot | None = None
    shape_probe_commands = 0
    shape_probe_elapsed = 0
    style_target_position: NativePosition | None = None
    style_verification_scope_sufficient = False
    if workflow == "image.insert":
        image = single_image(assets)
        if image is None:
            return _result(
                resolution,
                "needs_input",
                "삽입할 그림 한 개가 필요합니다",
                required_inputs=("inputs.assets.images",),
            )
        target_position = (
            None if recipe_inputs is None else recipe_inputs.target_position
        )
        document_end = (
            target is not None
            and target.kind == "document"
            and target.scope == "document"
        )
        image_document_end = document_end
        prefix: tuple[NativeActionCommand, ...] = (
            (MovePositionCommand(native_position(target_position)),)
            if target_position is not None
            else ((MoveDocumentEndCommand(),) if document_end else ())
        )
        if target_position is not None:
            resolved_target_id = (
                f"position:{target_position.list_id}:"
                f"{target_position.paragraph}:"
                f"{target_position.character}"
            )
            target_resolution_basis = "explicit_recipe_target_position"
        if document_end and target_position is None:
            resolved_target_id = "document:end"
            target_resolution_basis = "target.document_end"
        width = None if recipe_inputs is None else recipe_inputs.picture_width_mm
        height = None if recipe_inputs is None else recipe_inputs.picture_height_mm
        if width is not None and height is not None:
            image_fitted_width_mm, image_fitted_height_mm = fit_image_in_box(
                image,
                width_mm=width,
                height_mm=height,
            )
            # 상자를 꽉 채우라고 한 경우에만 자르기가 붙는다. 자를지 말지는
            # 도구가 정하지 않는다. 어느 가장자리를 얼마나 감출지는 원본 픽셀
            # 비와 상자 비에서 정해지는 값이고, 실제 자르기는 C++/ATL 이 한/글
            # SkipLeft/SkipTop/SkipRight/SkipBottom 으로 수행한다. 여기서 만든
            # 값은 wire 로 넘길 비율과 결과 대조용 기대치일 뿐이다.
            if recipe_inputs is not None and recipe_inputs.picture_fit == "cover":
                image_crop = cover_crop_in_box(
                    image,
                    width_mm=width,
                    height_mm=height,
                )
                image_uncropped_width_mm = image_fitted_width_mm
                image_uncropped_height_mm = image_fitted_height_mm
                image_fitted_width_mm, image_fitted_height_mm = (
                    fit_cropped_image_in_box(
                        image,
                        width_mm=width,
                        height_mm=height,
                        crop=image_crop,
                    )
                )
        image_before_page = (
            routing_page.page_count if document_end else routing_page.page
        )
        image_before = inspect_native_structure(
            candidate.window_handle,
            image_before_page,
        )
        if image_before is None:
            raise HwpLiveError("네이티브 그림 삽입 전 상세 구조를 읽지 못했습니다")
        image_before_controls = image_before.controls
        commands = (*prefix, InsertPictureCommand(image, width, height, image_crop))
    elif workflow == "image.replace":
        image = single_image(assets)
        resolved_control = resolve_control_target(
            ControlTargetRequest(
                routing_page,
                target,
                PICTURE_CONTROL_TYPES,
                _selection_snapshot(candidate, target),
                candidate.window_handle,
            )
        )
        if image is not None and isinstance(resolved_control, ControlTargetFailure):
            return _result(
                resolution,
                resolved_control.status,
                resolved_control.message,
                required_inputs=resolved_control.required_inputs,
            )
        resolved_picture = (
            resolved_control
            if isinstance(resolved_control, ResolvedObjectControl)
            else None
        )
        resolved_control_page = (
            None if resolved_picture is None else resolved_picture.page
        )
        control_id = None if resolved_picture is None else resolved_picture.instance_id
        target_resolution_basis = (
            None if resolved_picture is None else resolved_picture.basis
        )
        if image is None or control_id is None or resolved_control_page is None:
            return _result(
                resolution,
                "needs_input",
                "교체할 그림 한 개와 고유한 그림 대상이 필요합니다",
                required_inputs=(
                    "inputs.target.control_instance_id",
                    "inputs.assets.images",
                ),
            )
        resolved_target_id = control_id
        before_structure = inspect_native_structure(
            candidate.window_handle,
            resolved_control_page.page,
        )
        if before_structure is None:
            raise HwpLiveError("네이티브 그림 교체 전 상세 구조를 읽지 못했습니다")
        matching = tuple(
            control
            for control in before_structure.controls
            if control.instance_id == control_id
            and is_picture_control_type(control.control_type)
        )
        if len(matching) != 1:
            raise HwpLiveError(
                "교체할 그림의 상세 구조를 정확히 하나 확인하지 못했습니다"
            )
        image_replace_before = matching[0]
        width = None if recipe_inputs is None else recipe_inputs.picture_width_mm
        height = None if recipe_inputs is None else recipe_inputs.picture_height_mm
        resize_commands: tuple[NativeActionCommand, ...] = ()
        if width is not None and height is not None:
            fitted_width, fitted_height = fit_image_in_box(
                image,
                width_mm=width,
                height_mm=height,
            )
            image_replace_expectation = PictureReplaceExpectation(
                control_id=control_id,
                fitted_width_mm=fitted_width,
                fitted_height_mm=fitted_height,
            )
            resize_commands = (
                SelectControlCommand(control_id),
                ParameterActionCommand(
                    "ShapeObjDialog",
                    "HShapeObject",
                    (NativeSetter("HSet/ProtectSize", BooleanValue(False)),),
                ),
                ParameterActionCommand(
                    "ShapeObjDialog",
                    "HShapeObject",
                    (
                        NativeSetter("HSet/WidthRelTo", IntegerValue(4)),
                        NativeSetter("HSet/Width", MillimeterValue(fitted_width)),
                        NativeSetter("HSet/HeightRelTo", IntegerValue(2)),
                        NativeSetter("HSet/Height", MillimeterValue(fitted_height)),
                    ),
                ),
            )
        commands = (
            *resize_commands,
            SelectControlCommand(control_id),
            ParameterActionCommand(
                "PictureChange",
                "HPictureChange",
                (
                    NativeSetter("PicturePath", TextValue(str(image))),
                    NativeSetter(
                        "PictureEmbed",
                        IntegerValue(
                            1
                            if recipe_inputs is None or recipe_inputs.picture_embed
                            else 0
                        ),
                    ),
                ),
            ),
        )
    elif workflow == "caption.add":
        resolved_control = resolve_control_target(
            ControlTargetRequest(
                routing_page,
                target,
                CAPTIONABLE_CONTROL_TYPES,
                _selection_snapshot(candidate, target),
                candidate.window_handle,
            )
        )
        if (
            recipe_inputs is not None
            and recipe_inputs.caption_text is not None
            and isinstance(resolved_control, ControlTargetFailure)
        ):
            return _result(
                resolution,
                resolved_control.status,
                resolved_control.message,
                required_inputs=resolved_control.required_inputs,
            )
        resolved_object = (
            resolved_control
            if isinstance(resolved_control, ResolvedObjectControl)
            else None
        )
        resolved_control_page = (
            None if resolved_object is None else resolved_object.page
        )
        control_id = None if resolved_object is None else resolved_object.instance_id
        target_resolution_basis = (
            None if resolved_object is None else resolved_object.basis
        )
        if (
            recipe_inputs is None
            or recipe_inputs.caption_text is None
            or control_id is None
            or resolved_object is None
            or resolved_control_page is None
        ):
            return _result(
                resolution,
                "needs_input",
                "캡션 텍스트와 고유한 표 또는 그림 대상이 필요합니다",
                required_inputs=(
                    "inputs.target.control_instance_id",
                    "inputs.recipe.caption_text",
                ),
            )
        caption_text = recipe_inputs.caption_text.strip()
        if not caption_text:
            return _result(
                resolution,
                "needs_input",
                "캡션 텍스트가 비어 있습니다",
                required_inputs=("inputs.recipe.caption_text",),
            )
        # 후보 집합(CAPTIONABLE_CONTROL_TYPES)과 같은 규칙으로 갈라야 한다.
        # 여기만 정확 비교로 두면 후보로는 통과한 표가 else 로 떨어져 그림
        # 캡션 경로를 타게 된다.
        if is_table_control_type(resolved_object.control_type):
            before_structure = inspect_native_structure(
                candidate.window_handle,
                resolved_control_page.page,
            )
            if before_structure is None:
                raise HwpLiveError("네이티브 캡션 변경 전 상세 구조를 읽지 못했습니다")
            existing = tuple(
                caption
                for caption in before_structure.captions
                if caption.table_instance_id == control_id
            )
            if len(existing) > 1:
                raise HwpLiveError("대상 표의 기존 캡션이 하나보다 많습니다")
            caption_before = existing[0] if existing else None
        else:
            picture_targets = tuple(
                control
                for control in resolved_control_page.controls
                if control.instance_id == control_id
                and is_picture_control_type(control.control_type)
            )
            if len(picture_targets) != 1:
                raise HwpLiveError(
                    "그림 캡션 대상의 개체 ID와 앵커를 확인하지 못했습니다"
                )
            picture_caption_probe = probe_picture_caption(candidate, control_id)
            automatic_prefix = (
                None
                if picture_caption_probe.caption is None
                else picture_caption_probe.caption.automatic_prefix
            )
            picture_caption_expectation = PictureCaptionExpectation(
                control_id=control_id,
                caption_text=caption_text,
                anchor=picture_targets[0].anchor,
                existing_auto_number_count=sum(
                    control.control_type == "atno"
                    for control in resolved_control_page.controls
                ),
                automatic_prefix=automatic_prefix,
            )
            picture_caption_commands = build_picture_caption_commands(
                control_id,
                picture_caption_probe.caption,
                style_id=recipe_inputs.caption_style_id,
                title=caption_text,
            )
        if picture_caption_commands is not None:
            commands = picture_caption_commands
        elif caption_before is None:
            commands = (
                SelectControlCommand(control_id),
                CaptureTableCommand(),
                CaptionCommand(recipe_inputs.caption_style_id, caption_text),
            )
        else:
            commands = replace_table_caption_commands(
                control_id,
                caption_literal_text(
                    caption_before.text,
                    automatic_number=caption_before.automatic_number,
                ),
                caption_text,
            )
        caption_control_id = control_id
        resolved_target_id = control_id
    elif workflow == "style.apply":
        style_before = _selection_snapshot(candidate, target)
        style_target = resolve_style_position(
            recipe_inputs,
            target,
            style_before,
        )
        if (
            recipe_inputs is None
            or style_target is None
            or recipe_inputs.style_id is None
        ):
            return _result(
                resolution,
                "needs_input",
                "스타일 ID와 적용 위치가 필요합니다",
                required_inputs=("inputs.recipe.style_id", "inputs.target"),
            )
        style_expected_id = recipe_inputs.style_id
        style_target_position = native_position(style_target.position)
        style_verification_scope_sufficient = _style_verification_scope_is_sufficient(
            recipe_inputs, style_before
        )
        commands = (
            MovePositionCommand(style_target_position),
            style_command(style_expected_id),
        )
        resolved_target_id = (
            f"position:{style_target.position.list_id}:"
            f"{style_target.position.paragraph}:"
            f"{style_target.position.character}"
        )
        target_resolution_basis = style_target.basis
    else:
        if (
            recipe_inputs is None
            or recipe_inputs.source_position is None
            or recipe_inputs.target_position is None
        ):
            return _result(
                resolution,
                "needs_input",
                "원본과 대상 텍스트 위치가 필요합니다",
                required_inputs=(
                    "inputs.recipe.source_position",
                    "inputs.recipe.target_position",
                ),
            )
        shape_probe_commands, shape_probe_elapsed, _, _, _, copied_shape_source = (
            _execute(
                candidate,
                (MovePositionCommand(native_position(recipe_inputs.source_position)),),
            )
        )
        if recipe_inputs.style_copy_type in (1, 2):
            copied_shape_source = _numbered_shape(candidate, copied_shape_source)
        transfer = ParameterActionCommand(
            "ShapeCopyPaste",
            "HShapeCopyPaste",
            (NativeSetter("Type", IntegerValue(recipe_inputs.style_copy_type)),),
        )
        commands = (
            MovePositionCommand(native_position(recipe_inputs.source_position)),
            transfer,
            MovePositionCommand(native_position(recipe_inputs.target_position)),
            RunCommand("MoveParaBegin"),
            RunCommand("MoveSelParaEnd"),
            transfer,
        )
        resolved_target_id = (
            f"position:{recipe_inputs.target_position.list_id}:"
            f"{recipe_inputs.target_position.paragraph}:"
            f"{recipe_inputs.target_position.character}"
        )
        target_resolution_basis = "explicit_recipe_target_position"
    executed, elapsed, current_page, page_count, modified, after_snapshot = _execute(
        candidate, commands
    )
    executed += shape_probe_commands
    elapsed += shape_probe_elapsed
    if picture_caption_probe is not None:
        executed += picture_caption_probe.commands_executed
        elapsed += picture_caption_probe.elapsed_microseconds
    image_replace_content_unverified = (
        workflow == "image.replace" and image_replace_expectation is None
    )
    operation_verified: bool | None = None
    operation_message = (
        "프로토콜 9 그림 교체 명령은 실행됐지만 새 그림 내용은 구조 및 렌더에서 "
        + "확인하지 못했습니다"
        if image_replace_content_unverified
        else "프로토콜 9 C++/ATL 네이티브 개체 recipe를 실행했습니다"
    )
    created_control_ids: tuple[str, ...] = ()
    if workflow == "image.insert":
        if image_before_page is None:
            raise HwpLiveError("그림 삽입 상세 검증의 기준 쪽이 없습니다")
        after_pages = frozenset(
            (
                image_before_page,
                page_count if image_document_end else current_page,
            )
        )
        image_after_controls: list[NativeDetailedControl] = []
        for page in sorted(after_pages):
            image_after = inspect_native_structure(candidate.window_handle, page)
            if image_after is None:
                raise HwpLiveError("네이티브 그림 삽입 후 상세 구조를 읽지 못했습니다")
            image_after_controls.extend(image_after.controls)
        verified_picture = verify_inserted_picture(
            image_before_controls,
            tuple(image_after_controls),
            PictureInsertExpectation(
                image_fitted_width_mm,
                image_fitted_height_mm,
                after_pages if image_document_end else frozenset(),
                image_uncropped_width_mm,
                image_uncropped_height_mm,
            ),
        )
        created_control_ids = (verified_picture.instance_id,)
        # 자르기를 요청했으면 실제로 잘렸는지까지 읽은 크기로 말한다. 브리지가
        # 자르지 못해 종전대로 넣은 경우를 잘랐다고 보고하면 안 된다.
        crop_note = (
            ""
            if verified_picture.cropped is None
            else (
                ", 한/글 자르기 적용"
                if verified_picture.cropped
                else ", 한/글 자르기 미적용 — 원본 비율로 넣음"
            )
        )
        operation_message = (
            "프로토콜 9 C++/ATL 네이티브 개체 recipe를 실행했습니다 "
            f"(실제 그림 크기 {verified_picture.width_mm:.3f}"
            f"×{verified_picture.height_mm:.3f} mm{crop_note})"
        )
        operation_verified = True
    if workflow == "image.replace" and image_replace_expectation is not None:
        if image_replace_before is None or resolved_control_page is None:
            raise HwpLiveError("그림 교체 상세 검증의 기준 개체가 없습니다")
        after_structure = inspect_native_structure(
            candidate.window_handle,
            resolved_control_page.page,
        )
        if after_structure is None:
            raise HwpLiveError("네이티브 그림 교체 후 상세 구조를 읽지 못했습니다")
        matching = tuple(
            control
            for control in after_structure.controls
            if control.instance_id == image_replace_expectation.control_id
            and is_picture_control_type(control.control_type)
        )
        if len(matching) != 1:
            raise HwpLiveError(
                "교체된 그림의 상세 구조를 정확히 하나 확인하지 못했습니다"
            )
        verified_picture = verify_replaced_picture(
            image_replace_before,
            matching[0],
            image_replace_expectation,
        )
        operation_message = (
            "프로토콜 9 C++/ATL 네이티브 그림 교체 크기를 확인했습니다 "
            f"({verified_picture.width_mm:.3f}×{verified_picture.height_mm:.3f} mm)"
        )
        operation_verified = True
    if workflow == "caption.add":
        if (
            caption_text is None
            or caption_control_id is None
            or resolved_control_page is None
        ):
            raise HwpLiveError(
                "caption recipe executed without caption verification inputs"
            )
        if picture_caption_expectation is not None:
            after_page = inspect_native_page(
                candidate.window_handle,
                resolved_control_page.page,
                include_cells=False,
            )
            if after_page is None:
                raise HwpLiveError("그림 캡션 변경 후 빠른 구조를 읽지 못했습니다")
            _ = verify_picture_caption(after_page, picture_caption_expectation)
        else:
            after = inspect_native_structure(
                candidate.window_handle,
                resolved_control_page.page,
            )
            if after is None:
                raise HwpLiveError("네이티브 캡션 변경 후 상세 구조를 읽지 못했습니다")
            captions = tuple(
                caption
                for caption in after.captions
                if caption.table_instance_id == caption_control_id
            )
            if len(captions) != 1:
                raise HwpLiveError(
                    "네이티브 캡션 결과를 상세 구조에서 하나로 확인하지 못했습니다"
                )
            caption_after = captions[0]
            actual_title = caption_literal_text(
                caption_after.text,
                automatic_number=caption_after.automatic_number,
            )
            if actual_title != caption_text:
                raise HwpLiveError(
                    f"네이티브 캡션 제목이 요청과 다릅니다: {actual_title!r}"
                )
            if caption_before is not None and (
                caption_after.automatic_number,
                caption_after.style_id,
                caption_after.style_name,
            ) != (
                caption_before.automatic_number,
                caption_before.style_id,
                caption_before.style_name,
            ):
                raise HwpLiveError("기존 캡션의 자동번호 또는 스타일이 변경되었습니다")
        operation_verified = True
    if workflow == "style.apply":
        if style_target_position is None or style_expected_id is None:
            raise HwpLiveError(
                "스타일 적용 결과를 확인할 위치 또는 스타일 ID가 없습니다"
            )
        if style_verification_scope_sufficient:
            operation_verified = (
                after_snapshot.cursor == style_target_position
                and after_snapshot.style_id == style_expected_id
            )
    if (
        workflow == "style.copy"
        and copied_shape_source is not None
        and recipe_inputs is not None
    ):
        if recipe_inputs.style_copy_type in (1, 2):
            after_snapshot = _numbered_shape(candidate, after_snapshot)
        operation_verified = _verify_copied_shape(
            copied_shape_source, after_snapshot, recipe_inputs.style_copy_type
        )
        operation_message = (
            "원본의 글자·문단 모양 복사를 실행하고 관측된 속성을 비교했습니다."
        )
        if copied_shape_source.paragraph_format.heading_type == 2:
            operation_message += (
                " 자동 문단번호를 복사했습니다. 표·그림 제목은 번호를 직접 입력하지 말고 "
                "제목 문구만 교체한 뒤 앞뒤 번호를 확인하세요."
                " 개체 부착 캡션과 별도 제목의 중복도 확인하세요. "
                "아래 조회에서 캡션이 나오지 않아도 숨은 캡션이 없다는 뜻은 아닙니다."
            )
            detail = inspect_native_structure(candidate.window_handle, current_page)
            if detail is None:
                operation_message += (
                    " 이 쪽의 개체 부착 캡션 유무는 확인하지 못했습니다."
                )
            elif detail.captions:
                operation_message += (
                    f" 이 쪽에 개체 부착 캡션 {len(detail.captions)}개가 있습니다. "
                    "별도 제목과 중복되면 숨은 번호가 끼어들 수 있으므로 함께 확인하세요."
                )
    return _result(
        resolution,
        "executed",
        operation_message,
    ).model_copy(
        update={
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": (
                "native_detailed_structure_before_after"
                if workflow in {"image.insert", "caption.add"}
                else "native_snapshot_before_after"
            ),
            "verified": operation_verified,
            "failure_stage": (
                "image_replace_content_verification"
                if image_replace_content_unverified
                else None
            ),
            "commands_executed": executed,
            "native_elapsed_microseconds": elapsed,
            "current_page": current_page,
            "page_count": page_count,
            "modified": modified,
            "resolved_target_id": resolved_target_id,
            "target_resolution_basis": target_resolution_basis,
            "created_control_ids": created_control_ids,
        }
    )
