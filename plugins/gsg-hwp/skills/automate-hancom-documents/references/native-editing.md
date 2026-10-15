# Native editing

In production, use the most specific public tool. `hwp_operate`, `hwp_replace_selection`, `hwp_apply_layout`, `hwp_update_table_cells`, and `hwp_insert_table_images` are QA-only internal surfaces.

## Text

To copy the actual character and paragraph shapes of an existing paragraph, use `hwp_apply_style(source_position=..., target_position=..., style_copy_type=2)` without `style_id`. This copies the live paragraph's numbering definition as well as its visual formatting; a registered style ID may contain an older numbering definition. Type 1 copies paragraph shape only, type 0 character shape only. Inspect the resulting caption sequence after copying. The destination paragraph must contain text so the native copy action has a text selection to paste over.

### Table and figure title paragraphs

1. Inspect the intended title and a correct nearby title of the **same kind**. Table numbering and figure numbering are distinct. Check `heading_type=2` / `marker_is_automatic`, the visible prefix, and the object's attached caption. If the candidate only contains manually typed numbers, choose a genuinely numbered reference; copying plain text cannot create automatic numbering.
2. For an existing title paragraph, call `hwp_apply_style(source_position=SOURCE, target_position=TARGET, style_copy_type=2)`. Supply inspected positions, never example coordinates. For a new title, first insert one ordinary paragraph containing only its wording at the intended location using `hwp_insert_layout`, inspect its resulting position, then copy the reference shape onto it. Do not pass `caption_style_id` or invoke `hwp_add_caption` for this route.
3. The shape-copy call leaves the destination paragraph selected. If its wording needs changing, use `hwp_replace_selected_text(replacement=TITLE_WORDING)`; do not include `(그림 …)`, `<표 …>` or computed digits. Copying shapes and preserving HWP automatic numbering is what advances the number.
4. Verify the previous/current/next displayed titles, automatic paragraph-numbering status, and page render. A selected-block text export may display an automatic number as 1: do not overwrite it with the expected digits. Use page text and structural numbering as the evidence.
5. If a number skips, inspect `hwp_inspect_structure.tables[].caption` as well as body paragraphs. Hidden object-attached captions count too. A visually hidden caption is not proof of absence. Do not add a second numbering mechanism or manually renumber following titles; identify the duplicate precisely and use a supported edit to resolve it. Report an unresolved duplicate distinctly from successful shape copying.

### Local letter spacing

`hwp_format_text(letter_spacing_percent=-2, target=RANGE, expected_text=EXACT_TEXT)` sets **absolute** spacing to -2 percent only within the inspected range. The range may be in the body or a table-cell text list. `hwp_inspect.character_style.letter_spacing_percent` observes current Hangul spacing when available; null means unknown/mixed, not zero. Existing spacing may already be negative, so -2 can widen text that was -6. Read the existing value and adjust it slightly, rather than assuming a baseline. Render before and after, judge the exact broken word, and revisit neighbouring lines after each change. The default correction should be 1–2 percentage points from the observed value; avoid conspicuous squeezing/stretching. Do not select an entire multi-line paragraph or table cell just because its final line is short.

Spacing is exposed through `hwp_format_text`, not the batch patch formatting schema: the current prepared-batch inverse protocol does not record per-script spacing. The single formatting path retains its existing checkpoint policy and the native executor verifies all seven `Spacing*` properties. Other optional fields remain unchanged when omitted.

Use `hwp_replace_selected_text(replacement=...)` to replace the complete text that the user currently dragged. Pass `replacement=""` to delete that selected text. The public tool reads the current endpoints and original text internally and the native bridge validates both immediately before mutation; do not ask the model to reproduce either value. Use `hwp_format_text` or `hwp_apply_style` for selection formatting. The separate `hwp_replace_selection` surface remains QA-only because it accepts low-level expected endpoints and original text explicitly.

For insertion without a text selection, `hwp_insert_layout(layout={"target": "current", ...})` uses the current-or-last HWP caret. This is the HWP caret even while focus is in Codex and the HWP caret is no longer blinking; it works both in body text and inside a table cell.

For a user-authorized document-wide literal replacement, prefer the native `find` target over reconstructed scan offsets. Inspect all relevant pages, require complete results, and deduplicate page-spanning paragraph/cell targets before counting occurrences. If the replacement does not contain the old literal, a batch can contain that many identical patches with `target={"kind":"find","occurrence":1}`: each mutation removes the first remaining occurrence, and shared `formatting` applies only to the matched replacement. Pass the inspected document identity and content revision, then verify no old occurrences remain. Do not use this technique for a partial selection of occurrences or when the replacement contains the search literal. Native FIND resolves hidden caret offsets and automatic-number readback; exact RANGE/CELL preflight can reject those even when the scan text looks correct.

## Layout

Use `hwp_insert_layout` for editable paragraphs, tables, and individual images at a collapsed live cursor (`target=current`) or on a new page immediately after a numbered page (`target=after_page`, `page=N`). The `after_page` target brackets the inserted content with page breaks, so pass content blocks without adding another page break. Use `hwp_append_layout` only for the document end. QA-only `hwp_apply_layout` is reserved for inspected insertion-position diagnostics.

- Reuse styles returned by `hwp_list_styles`.
- For an explicitly requested **object-attached caption**, provide only the title text. The caption engine creates/preserves its native automatic field. `hwp_add_caption` is for that object-caption mechanism. Ordinary table/figure title paragraphs use the shape-copy workflow above; do not substitute an attached caption for them. Never include a rendered number prefix in replacement text.
- Initialize the table anchor and cells from the document's base style. Set left margin, right margin, and indentation explicitly to zero when inherited offsets are not intended.
- Define merges, borders, cell padding, alignment, and vertical alignment as native properties.
- A requested 1 mm row or column remains explicit through the MCP and native command path. For an actual 1 mm cell, also use 1 pt cell text, 50% line spacing, and 0 mm padding; otherwise HWP expands the cell to fit its content. Read back the table dimensions and report HWP's 283-HWPUNIT rounding (about 0.998 mm per equal row or column).
- `hwp_insert_image` and `hwp_replace_image` accept optional `width_mm` and `height_mm`. Omit both to leave size unspecified, or provide both as a bounding box; a one-sided size is rejected. Fitting preserves the image's original aspect ratio.
- `hwp_insert_image` also accepts `fit`. The default `contain` is that aspect-preserving fit, which leaves an empty margin on one axis when the picture and the box disagree. `fit=cover` fills the box instead and hides the overflowing edges with HWP's own picture crop (`ShapeDrawImageAttr` `SkipLeft`/`SkipTop`/`SkipRight`/`SkipBottom`), split evenly around the centre; it requires `width_mm` and `height_mm`. Never pre-crop a copy of the source file to fill a box: both modes read the image file and never rewrite it, and a cropped picture keeps its full original inside the document, so the crop stays adjustable in HWP. Whether to crop is the caller's decision, never the tool's. If the attached HWP build does not expose the picture's `OriginalSizeX`/`OriginalSizeY`, the bridge inserts the picture the `contain` way rather than failing, and the result message says the crop was not applied.

## Existing tables

Use `hwp_fill_table` for existing cell values and `hwp_fill_table_images` for pictures in existing cells. With `rows`, omit `start_cell` to use the live current table cell, the upper-left owner of a contiguous selected cell block, or A1 of a selected table. A row matrix that would leave a selected multi-cell block is rejected before mutation. The lower-level `hwp_update_table_cells` and `hwp_insert_table_images` tools are QA-only.

Every write is bounded but a multi-action HWP operation is not a database transaction. Check the completed tool's operation-specific readback. Inspect the affected structure separately only when the requested result is not established by that readback.

For an existing table, `hwp_format_table` can resize complete rows/columns and apply text, fill, border width/color/style, and vertical alignment. Omit `cell` to use the live current table cell, every cell in a contiguous selected block, or every cell in a selected table. Pass `row_height_mm` and `column_width_mm` in the 1–250mm range. Use an explicit `cell` selector only when the requested target is not the live HWP selection. Use `hwp_inspect_page_fast(include_cells=true)` when the result does not report the requested dimensions.

### Body text formatting

`hwp_format_text(whole_body=true, ...)` selects the top-level body with native SelectAll and changes only the supplied formatting fields. It does not rewrite text and verifies selection, text, and requested properties. Hancom applies this body selection separately from nested table text; use `hwp_format_table(target={target_id: verified_table_id}, text_color=...)` for each verified table containing text. Table formatting leaves picture pixels unchanged.
