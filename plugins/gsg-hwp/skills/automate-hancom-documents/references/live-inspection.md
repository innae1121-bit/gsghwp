# Live inspection

## Connect

Call the selected read tool directly. Production inspection auto-connects the single active, saved, editable HWP and does not require a session ID. Pass `document_path` only when more than one open document makes the target ambiguous. `hwp_connect` remains available for explicit session diagnostics; repeated calls for the same connected document return the existing session instead of failing.

## Choose the smallest read

| Need | Tool |
|---|---|
| Page text, controls, table anchors, dimensions | `hwp_inspect_page_fast` |
| The same plus cell addresses and values | `hwp_inspect_page_fast(include_cells=true)` |
| Paragraph and cell targets, original text, and document guards for a multi-page batch | `hwp_inspect_patch_plan(pages=...)` |
| Bounded source XLSX rows, physical coordinates, headers, units, notes, and path provenance | `hwp_read_workbook_rows(source_paths=..., key=..., sheet_queries=...)` |
| Current-or-last HWP cursor, selected text/cells/table, current paragraph and character formatting | `hwp_inspect` |
| Native style names and IDs | `hwp_list_styles` |
| Table refs, merged cells, captions, page span, optimistic token | `hwp_inspect_structure` |
| Complete immutable document graph inventory | `hwp_get_graph_manifest` |
| Paged graph records | `hwp_query_graph` |
| One public node/property/asset | `hwp_get_graph_node`, `hwp_get_graph_property`, `hwp_get_graph_asset` |
| HWP page analysis or result verification | `hwp_render_page` (`CreatePageImage`) |
| Wait for cursor, structure, window, or dialog changes in QA | `hwp_watch_state` |
| Read an HWP popup's complete current control structure | `hwp_inspect_dialog` |
| Invoke one model-selected popup button | `hwp_invoke_dialog_action` |

Fast inspection runs C++/ATL in-process. Detailed structure inspection is a separate native control traversal — an edit preflight, not a polling API — and answers a narrower set of questions (table refs, merged cells, captions, page span, optimistic token) than its name suggests.

For a 1–3 page XLSX-driven text/table patch, use this source-driven sequence:

1. Call one `hwp_inspect_patch_plan(pages=...)` for exactly the requested pages. Read the returned paragraph and table headings and derive only relevant workbook `sheet_queries` from those actual document headings. Do not substitute remembered domain names or reread the HWP to list the same targets.
2. Call `hwp_read_workbook_rows` once with the user's file or directory in `source_paths`, the requested row `key`, optional `columns`, and those `sheet_queries`. Each query is a Unicode-whitespace-insensitive, case-insensitive substring OR-match against actual sheet names; blank queries are invalid. Pass the user path as supplied: the tool tries it exactly first and, only for the first missing component below the deepest existing parent, may resolve one unique whitespace-equivalent sibling while requiring the remaining path tail to match exactly. Directory inputs include immediate `.xlsx` children only. Review `actual_sheets` and `matched_sheets`, plus each source's `requested_path`, absolute `resolved_path`, `resolution`, and `discovery`; no sheet match returns those inventories with empty matches. Do not perform a filesystem walk, invoke `uv`/Python or CLI help, or filter a saved JSON response in the shell. An empty or ambiguous result is evidence to report, not permission to guess another sheet or value.
3. Project only actual changes into one `hwp_patch_text_batch` — or one `hwp_fill_table` per target table when every target is a cell of that table. Copy `expected_document_id`, `expected_document_full_name`, and `expected_content_revision` from the same patch-plan result. When purple is requested, use the batch's typed common `formatting` field as `{"text_color":"#800080"}`. When the request says only changed places should be purple, also set `format_changed_only=true` and pass the complete inspected before/after text for each changed paragraph or cell; do not calculate UTF-16 subranges in the model.
4. Accept the completed write's operation-specific readback as the final verification when it establishes every requested change. Use one final scoped read only for a fact absent from that readback, and use `hwp_render_page` last only when visual appearance must be established. Do not reread a complete, unchanged result.

For workbook matches, use `column_details` as the authoritative physical-column values, including blanks. Each `column` is 1-based, and each `header_refs` entry is a 1-based index into that match's `header_sources`, which preserves raw header text and source coordinates. Preserve returned raw values, units, header references, and coordinates in the evidence used to decide each replacement. When `values_ambiguous=true`, the legacy `values` map has repeated leaf labels and keeps only the last value for each label; use `column_details` and the full header sources for mapping and totals, and surface the ambiguity instead of silently choosing. Read `unit_evidence` and `note_evidence` as source text with their coordinates, and keep a separate note column separate from its neighbouring numeric value. Do not infer a unit, year, header meaning, or relationship absent from the source evidence.

By default, the batch applies common `formatting` to each complete `replacement`; `format_changed_only=false` does not compute a character diff and preserves the existing full-replacement behavior. For “바꾼 곳만 보라색”, set `format_changed_only=true`: it accepts inspected same-paragraph ranges and table cells, and every item must include `expected_text`. Paragraph replacements format only changed whitespace-delimited non-whitespace tokens, including multiple nonoverlapping changes in one paragraph; Unequal token counts are supported through exact nonoverlapping replacement spans; insertions need an existing interior whitespace anchor. A changed table cell formats only its changed display-token span. Multiple cell changes can share one span only when unchanged interior text is whitespace; the expected substring must be unique. Unsupported spans are refused before mutation: report the limitation or use a verified narrower exact target, without disabling changed-only formatting to color unchanged text. Supply the complete inspected paragraph or cell as `expected_text` and its complete desired result as `replacement`. Identical cell values are omitted; a batch with no text changes is refused before mutation. Do not duplicate the same cell target in one batch.

For example, changing `공원은 100개` to `공원은 167개` with only the changed value colored uses the inspected complete string as `expected_text`, the complete updated string as `replacement`, common `formatting={"text_color":"#800080"}`, and `format_changed_only=true`. Do not derive a character offset, split multiple paragraph changes into separate model-made ranges, or submit unchanged cells. Journal the caller's operation ID on that write. Check its operation-specific readback once; use a final scoped read or `hwp_render_page` only for requested facts that the result does not establish. No blanket re-inspection follows.

An identical fast inspection is cached for the current live-change revision, with at most 32 page/mode entries. Any MCP mutation, external HWP change event, reconnect, or disconnect invalidates it, so repeated reasoning reads avoid a second full control scan without serving pre-edit structure after a change.

`hwp_list_styles` likewise reuses one style scan for the same document and `state_token`. A local mutation, external HWP change event, document change, reconnect, or disconnect invalidates that result.

When `hwp_inspect_structure` receives an explicit nonzero `page`, native control traversal is limited to that requested page instead of scanning unrelated pages. Use `page=0` only for the current-page form.

`hwp_inspect`, `hwp_list_styles`, `hwp_inspect_page_fast`, `hwp_inspect_structure`, and `hwp_render_page` return a short MCP text summary and keep the complete result once in the structured payload. Read the structured result; do not expect or parse a duplicate JSON copy from the text summary.

## Complete graph reads

Graph tools are additive and do not replace the compact inspection tools above. Start with `hwp_get_graph_manifest`: its response remains at most 32 KiB and reports one immutable generation/version plus an index of content-addressed source frames. It never compacts, truncates, or drops graph records. Use `hwp_query_graph` until `continuation=null`; each receipt identifies a digest-bound immutable record artifact. Use `hwp_fetch_graph_artifact` with the receipt's exact `document_id`, `store_id`, and 64-digit `expected_version`, then follow `continuation_offset` to exhaust large property, text, or asset bytes. The same `hwp-graph://artifact/...` URI is readable through the MCP resource surface only by the server/session capability that created it.

Node IDs and graph facts are only those returned by the graph. `hwp_get_graph_node` and related tools never mint IDs or infer unavailable values. `NOT_EXPOSED`, `NOT_APPLICABLE`, and `READ_FAILED` are explicit observations, not empty values. Every continuation and artifact is cryptographically bound to its server/session authorization scope, exact document/session lineage, store, graph version, immutable cache generation, content digest, media type, and kind. URI possession alone grants no access. A wrong document, stale version, foreign server/session, retired generation, altered URI/digest, or invalid NodeId returns a typed structured error and exposes no artifact bytes. Artifacts remain available while their authorized generation is active; replacement generation retirement and server/session shutdown remove only that owner's generation tree deterministically. Public responses do not expose native automation action/member names.

Every top-level `instance_id` returned by fast inspection is immediately reusable. Pass it directly as `target_id` to `hwp_add_caption`, `hwp_replace_image`, or `hwp_fill_table`. For a tool whose schema has a `target` object, including `hwp_format_table`, `hwp_merge_table_cells`, and `hwp_split_table_cell`, pass `target: {"target_id": instance_id}`. Include it in `control_instance_ids` for `hwp_delete_control`. After a worker reload, inspect again before relying on cached page and index metadata.

A render establishes visual facts — how a page looks, and whether something is clipped, distorted, or in the wrong place. The structural reads above establish IDs, cell addresses, merges, and exact dimensions. Neither substitutes for the other, and a question about text or structure alone does not need a render. Reconstruction is not such a question: rebuilding a page from an image is a visual claim, so its closed loop in `native-layout.md` renders the changed page, compares it with the source, and renders again after a corrective patch.

## State discipline

- Moving focus from HWP to Codex stops the visual caret blink but does not invalidate HWP's last caret. Treat `active_target.basis=current_or_last_hwp_position` as the live edit anchor until an HWP state change is observed.
- Use `active_target.kind`, `selection_mode_raw`, `control_instance_id`, and `cell_address` instead of inferring selection type from whether `selected_text` is empty. A cell block can have empty selected text, and a selected table is a control selection.
- Page numbers are current results, not durable anchors. Prefer control identity and structure.
- Pass exact cursor, selection, original text, `table_ref`, and `state_token` values back to writes.
- If any expected value changes, discard the pending write and inspect again.
- Do not use screenshots to infer cell addresses, merges, captions, or styles.

## Structured popup interaction

Never infer a popup action from a fixed title, translated label, button order, or assumed button count.

1. Discover the popup handle from the current window/dialog diagnostic.
2. Call `hwp_inspect_dialog(dialog_window_handle=...)`.
3. Read every returned control's source (`win32` or `uia`), title, class, `control_id`, role, visibility, enabled state, default state, and accelerator.
4. Let the model select any enabled `actionable=true` control from that current structure. Do not apply label, ordinal, button-count, or default-button routing rules.
5. Call `hwp_invoke_dialog_action` with the same dialog handle and selected `control_id`.
6. Verify `message_delivered`, `dialog_present_after`, and the returned post-action structure.

Inspection is read-only. Invocation re-enumerates and revalidates the selected child immediately before one bounded Win32 `BM_CLICK` or WPF UI Automation `InvokePattern`; it fails closed for missing, duplicate, changed, hidden, disabled, or non-actionable controls. A delivery timeout is an uncertain result and must not be retried automatically.

The same structure applies to normal tool dialogs, error dialogs, overwrite confirmations, and future HWP popups. Labels such as `설정`, `취소`, `예`, or `아니요` are observations, never routing rules. If a popup has no child action and only a title-bar `X`, or its visible WPF `PART_Close` has no InvokePattern, inspection adds an actionable `source=window`, `role=window_close`, `control_id=-1` entry; the model may select it exactly like any other returned control. Explicit popup recovery runs outside the document COM lane so a dialog blocking that lane cannot also block its own recovery action.
