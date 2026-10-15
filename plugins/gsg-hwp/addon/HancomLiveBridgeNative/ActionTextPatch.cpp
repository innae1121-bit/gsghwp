#include "ActionExecutorInternal.h"
#include "ParagraphText.h"
#include "TextPatchReadback.h"

#include <algorithm>
#include <chrono>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace hancom::actions::detail {

using hancom::text::SameParagraphText;
using hancom::text_patch::MatchesExpectedReadback;
using hancom::text_patch::MatchesReplacementReadback;
using hancom::text_patch::ReadbackPolicy;

bool FormatLongProperty(
    IDispatch* const object,
    const wchar_t* const name,
    LONG* const value) {
    CComVariant raw;
    return SUCCEEDED(PropertyGet(object, name, &raw)) &&
        SUCCEEDED(AsLong(raw, value));
}

bool FormatBooleanProperty(
    IDispatch* const object,
    const wchar_t* const name,
    bool* const value) {
    CComVariant raw;
    return SUCCEEDED(PropertyGet(object, name, &raw)) &&
        SUCCEEDED(AsBool(raw, value));
}

bool FormatTextProperty(
    IDispatch* const object,
    const wchar_t* const name,
    std::wstring* const value) {
    CComVariant raw;
    return SUCCEEDED(PropertyGet(object, name, &raw)) &&
        SUCCEEDED(AsString(raw, value));
}

bool DefaultTextFormatParameter(
    Context* const context,
    const wchar_t* const actionName,
    const wchar_t* const parameterName,
    CComPtr<IDispatch>& parameter,
    CComPtr<IDispatch>& set,
    const std::wstring& location) {
    CComPtr<IDispatch> parameterSets;
    if (!GetDispatchProperty(
            context->hwp,
            L"HParameterSet",
            parameterSets,
            context->result,
            location) ||
        !GetDispatchProperty(
            parameterSets,
            parameterName,
            parameter,
            context->result,
            location) ||
        !GetDispatchProperty(parameter, L"HSet", set, context->result, location)) {
        return false;
    }
    CComVariant ignored;
    const HRESULT status = Method(
        context->action,
        L"GetDefault",
        {CComVariant(actionName), CComVariant(set)},
        &ignored);
    return SUCCEEDED(status) ||
        SetError(
            context->result,
            L"TEXT_FORMAT_READBACK",
            location,
            FormatHResult(actionName, status));
}

bool ReadTextFormatFingerprint(
    IDispatch* const character,
    IDispatch* const paragraph,
    TextFormatFingerprint* const format) {
    return
        FormatTextProperty(character, L"FaceNameHangul", &format->faceName) &&
        FormatLongProperty(character, L"Height", &format->height) &&
        FormatBooleanProperty(character, L"Bold", &format->bold) &&
        FormatLongProperty(character, L"TextColor", &format->textColor) &&
        FormatLongProperty(character, L"RatioHangul", &format->widthRatio) &&
        FormatLongProperty(character, L"SpacingHangul", &format->letterSpacing) &&
        FormatLongProperty(paragraph, L"AlignType", &format->alignment) &&
        FormatLongProperty(
            paragraph,
            L"LineSpacingType",
            &format->lineSpacingType) &&
        FormatLongProperty(paragraph, L"LineSpacing", &format->lineSpacing) &&
        FormatLongProperty(paragraph, L"LeftMargin", &format->leftMargin) &&
        FormatLongProperty(paragraph, L"RightMargin", &format->rightMargin) &&
        FormatLongProperty(paragraph, L"Indentation", &format->indentation) &&
        FormatLongProperty(paragraph, L"PrevSpacing", &format->previousSpacing) &&
        FormatLongProperty(paragraph, L"NextSpacing", &format->nextSpacing);
}

bool CaptureTextFormat(
    Context* const context,
    PreservedTextFormat* const preserved,
    const std::wstring& location) {
    CComPtr<IDispatch> character;
    CComPtr<IDispatch> paragraph;
    return
        DefaultTextFormatParameter(
            context,
            L"CharShape",
            L"HCharShape",
            character,
            preserved->characterSet,
            location) &&
        DefaultTextFormatParameter(
            context,
            L"ParagraphShape",
            L"HParaShape",
            paragraph,
            preserved->paragraphSet,
            location) &&
        (ReadTextFormatFingerprint(character, paragraph, &preserved->fingerprint) ||
         SetError(
             context->result,
             L"TEXT_FORMAT_READBACK",
             location,
             L"character or paragraph format could not be captured"));
}

bool CapturePreparedTextFormat(
    Context* const context,
    const Request& request,
    const size_t patchIndex,
    PreparedTextPatchTarget* const captured,
    const std::wstring& location) {
    std::set<std::wstring> characterProperties;
    std::set<std::wstring> paragraphProperties;
    for (size_t index = patchIndex + 1; index < request.commands.size(); ++index) {
        const Command& command = request.commands[index];
        if (command.kind == CommandKind::TextPatch) {
            break;
        }
        if (command.kind != CommandKind::Action) {
            continue;
        }
        std::set<std::wstring>* properties = nullptr;
        if (command.name == L"CharShape") {
            properties = &characterProperties;
        } else if (command.name == L"ParagraphShape") {
            properties = &paragraphProperties;
        } else {
            continue;
        }
        for (const Setter& setter : command.setters) {
            properties->insert(setter.path);
        }
    }

    CComPtr<IDispatch> parameter;
    CComPtr<IDispatch> set;
    if (!characterProperties.empty()) {
        if (!DefaultTextFormatParameter(
                context,
                L"CharShape",
                L"HCharShape",
                parameter,
                set,
                location)) {
            return false;
        }
        for (const std::wstring& property : characterProperties) {
            const bool read =
                property == L"Bold"
                    ? FormatBooleanProperty(parameter, L"Bold", &captured->bold)
                : property == L"Height"
                    ? FormatLongProperty(parameter, L"Height", &captured->height)
                : property == L"TextColor"
                    ? FormatLongProperty(
                          parameter, L"TextColor", &captured->textColor)
                : property.rfind(L"FaceName", 0) == 0
                    ? FormatTextProperty(
                          parameter, L"FaceNameHangul", &captured->faceName)
                : property.rfind(L"FontType", 0) == 0;
            if (!read) {
                return SetError(
                    context->result,
                    L"TEXT_FORMAT_READBACK",
                    location,
                    property + L" could not be captured");
            }
        }
    }
    if (!paragraphProperties.empty()) {
        parameter.Release();
        set.Release();
        if (!DefaultTextFormatParameter(
                context,
                L"ParagraphShape",
                L"HParaShape",
                parameter,
                set,
                location)) {
            return false;
        }
        for (const std::wstring& property : paragraphProperties) {
            const bool read =
                property == L"AlignType"
                    ? FormatLongProperty(
                          parameter, L"AlignType", &captured->alignment)
                : property == L"LineSpacing"
                    ? FormatLongProperty(
                          parameter, L"LineSpacing", &captured->lineSpacing)
                : false;
            if (!read) {
                return SetError(
                    context->result,
                    L"TEXT_FORMAT_READBACK",
                    location,
                    property + L" could not be captured");
            }
        }
    }
    return true;
}

bool ExecuteCapturedFormat(
    Context* const context,
    const wchar_t* const actionName,
    IDispatch* const set,
    const std::wstring& location) {
    bool executed = false;
    return
        CallBooleanMethod(
            context->action,
            L"Execute",
            {CComVariant(actionName), CComVariant(set)},
            &executed,
            context->result,
            location) &&
        (executed ||
         SetError(
             context->result,
             L"TEXT_FORMAT_APPLY",
             location,
             std::wstring(actionName) + L" returned false"));
}

bool SameTextFormat(
    const TextFormatFingerprint& left,
    const TextFormatFingerprint& right) noexcept {
    return left.faceName == right.faceName &&
        left.height == right.height &&
        left.bold == right.bold &&
        left.textColor == right.textColor &&
        left.widthRatio == right.widthRatio &&
        left.letterSpacing == right.letterSpacing &&
        left.alignment == right.alignment &&
        left.lineSpacingType == right.lineSpacingType &&
        left.lineSpacing == right.lineSpacing &&
        left.leftMargin == right.leftMargin &&
        left.rightMargin == right.rightMargin &&
        left.indentation == right.indentation &&
        left.previousSpacing == right.previousSpacing &&
        left.nextSpacing == right.nextSpacing;
}

LONG ReferenceHwpAlignment(const LONG alignment) noexcept {
    switch (alignment) {
    case 0: return 1;
    case 1: return 3;
    case 2: return 2;
    default: return 0;
    }
}

bool MatchesExpectedReferenceTextFormat(
    const TextFormatFingerprint& actual,
    const hancom::reference_layout::Style& expected) noexcept {
    return
        (expected.fontName.empty() || actual.faceName == expected.fontName) &&
        (expected.fontSize < 0 || actual.height == expected.fontSize) &&
        (expected.bold < 0 || actual.bold == (expected.bold != 0)) &&
        (expected.textColor < 0 || actual.textColor == expected.textColor) &&
        (expected.widthRatio < 0 || actual.widthRatio == expected.widthRatio) &&
        (expected.letterSpacing < -50 ||
         actual.letterSpacing == expected.letterSpacing) &&
        (expected.alignment < 0 ||
         actual.alignment == ReferenceHwpAlignment(expected.alignment)) &&
        (expected.lineSpacingType < 0 ||
         actual.lineSpacingType == expected.lineSpacingType) &&
        (expected.lineSpacing < 0 ||
         actual.lineSpacing == expected.lineSpacing) &&
        (expected.previousSpacing < 0 ||
         actual.previousSpacing == expected.previousSpacing) &&
        (expected.nextSpacing < 0 ||
         actual.nextSpacing == expected.nextSpacing);
}

bool ApplyAndVerifyTextFormat(
    Context* const context,
    const PreservedTextFormat& preserved,
    const std::wstring& location) {
    if (!ExecuteCapturedFormat(
            context,
            L"CharShape",
            preserved.characterSet,
            location) ||
        !ExecuteCapturedFormat(
            context,
            L"ParagraphShape",
            preserved.paragraphSet,
            location)) {
        return false;
    }
    PreservedTextFormat after;
    if (!CaptureTextFormat(context, &after, location)) {
        return false;
    }
    return SameTextFormat(preserved.fingerprint, after.fingerprint) ||
        SetError(
            context->result,
            L"TEXT_FORMAT_READBACK",
            location,
            L"character or paragraph format changed after text replacement");
}

bool SetCellText(Context* const context, const Command& command) {
    const std::wstring address = NormalizeAddress(command.first);
    if (!GoToCell(context, address)) {
        return false;
    }
    Position insertionStart;
    if (!command.second.empty() &&
        !GetPosition(context->hwp, &insertionStart, context->result)) {
        return false;
    }
    if (!RunAction(context->action, L"SelectAll", context->result, address)) {
        return false;
    }
    Selection before;
    std::wstring current;
    if (!GetSelection(context->hwp, &before, context->result) ||
        !ReadSelectedText(context, &current, address)) {
        return false;
    }
    if (command.hasExpectedText &&
        !SameParagraphText(current, command.expectedText)) {
        return SetError(
            context->result,
            L"STALE_CELL_TEXT",
            address,
            L"cell text changed before the style-preserving replacement");
    }
    const bool preserveExistingFormat =
        command.preserveFormat && !current.empty();
    PreservedTextFormat preserved;
    if (preserveExistingFormat &&
        !CaptureTextFormat(context, &preserved, address)) {
        return false;
    }
    if (!command.second.empty() &&
        (before.mode & kSelectionModeMask) == kSelectionText &&
        before.start.list == insertionStart.list &&
        before.end.list == insertionStart.list &&
        !SamePosition(before.start, before.end)) {
        insertionStart = before.start;
    }
    const bool replaced = command.second.empty()
        ? RunAction(context->action, L"Delete", context->result, address)
        : InsertText(context, command.second, address);
    if (!replaced) {
        return false;
    }
    context->result->partialMutation = true;
    if (command.second.empty()) {
        if (!GoToCell(context, address) ||
            !RunAction(context->action, L"SelectAll", context->result, address)) {
            return false;
        }
        std::wstring after;
        return
            ReadSelectedText(context, &after, address) &&
            (after.empty() ||
             SetError(
                 context->result,
                 L"TEXT_PATCH_READBACK",
                 address,
                 L"cell text is not empty after replacement"));
    }
    Position end;
    if (!GetPosition(context->hwp, &end, context->result) ||
        !SelectTextRange(context, insertionStart, end, address)) {
        return false;
    }
    if (preserveExistingFormat &&
        !ApplyAndVerifyTextFormat(context, preserved, address)) {
        return false;
    }
    std::wstring inserted;
    return
        ReadSelectedText(context, &inserted, address) &&
        (SameParagraphText(inserted, command.second) ||
         SetError(
             context->result,
             L"TEXT_PATCH_READBACK",
             address,
             L"cell replacement readback does not match the requested text"));
}

bool ReplaceSelection(Context* const context, const Command& command) {
    Selection before;
    if (!GetSelection(context->hwp, &before, context->result)) {
        return false;
    }
    if (!before.selected) {
        return SetError(
            context->result,
            L"NO_SELECTION",
            L"selection",
            L"the replacement command requires an active text selection");
    }
    if (before.start.list != before.end.list) {
        return SetError(
            context->result,
            L"CROSS_CONTROL_SELECTION",
            L"selection",
            L"the replacement selection crosses HWP controls");
    }

    CComVariant raw;
    std::wstring selected;
    HRESULT status = Method(
        context->hwp,
        L"GetTextFile",
        {CComVariant(L"UNICODE"), CComVariant(L"saveblock:true")},
        &raw);
    if (SUCCEEDED(status)) {
        status = AsString(raw, &selected);
    }
    if (FAILED(status)) {
        return SetError(
            context->result,
            L"SELECTION_TEXT",
            L"selection",
            FormatHResult(L"GetTextFile", status));
    }
    if (selected != command.first) {
        return SetError(
            context->result,
            L"STALE_SELECTION_TEXT",
            L"selection",
            L"selected text changed after the request was prepared");
    }

    Selection confirmed;
    if (!GetSelection(context->hwp, &confirmed, context->result)) {
        return false;
    }
    if (confirmed.selected != before.selected ||
        confirmed.start.list != before.start.list ||
        confirmed.start.paragraph != before.start.paragraph ||
        confirmed.start.character != before.start.character ||
        confirmed.end.list != before.end.list ||
        confirmed.end.paragraph != before.end.paragraph ||
        confirmed.end.character != before.end.character) {
        return SetError(
            context->result,
            L"STALE_SELECTION",
            L"selection",
            L"selection changed while its text was verified");
    }
    return InsertText(context, command.second, L"selection");
}

bool TextMatchesAt(
    const std::wstring& actual,
    const std::wstring& expected,
    const size_t offset,
    const bool matchCase) {
    if (offset > actual.size() || expected.size() > actual.size() - offset) {
        return false;
    }
    for (size_t index = 0; index < actual.size(); ++index) {
        if (index == expected.size()) {
            return true;
        }
        const wchar_t current = actual[offset + index];
        if ((matchCase && current != expected[index]) ||
            (!matchCase && towlower(current) != towlower(expected[index]))) {
            return false;
        }
    }
    return expected.size() == actual.size() - offset;
}

// The number HWP draws in front of the caret's paragraph, or an empty
// string when the paragraph has none and when the call is unavailable. Read
// only after a plain readback comparison already failed, because that is
// the only case in which the drawn number can be what made it fail.
std::wstring ReadAutomaticNumber(Context* const context) {
    CComVariant raw;
    std::wstring automaticNumber;
    HRESULT status = Method(context->hwp, L"GetHeadingString", {}, &raw);
    if (SUCCEEDED(status)) {
        status = AsString(raw, &automaticNumber);
    }
    return SUCCEEDED(status) ? automaticNumber : std::wstring();
}

bool PatchSelectedText(
    Context* const context,
    const std::wstring& expected,
    const bool requireExpected,
    const bool matchCase,
    const std::wstring& replacement,
    const std::wstring& location,
    const bool preserveFormat,
    const ReadbackPolicy readbackPolicy) {
    Selection before;
    if (!GetSelection(context->hwp, &before, context->result)) {
        return false;
    }
    if ((before.mode & kSelectionModeMask) != kSelectionText) {
        return SetError(
            context->result,
            L"NON_TEXT_SELECTION",
            location,
            L"text.patch cannot replace a non-text selection");
    }
    if (!before.selected || before.start.list != before.end.list) {
        return SetError(
            context->result,
            before.selected ? L"CROSS_CONTROL_SELECTION" : L"NO_SELECTION",
            location,
            L"text.patch requires one active text range");
    }
    std::wstring selected;
    if (!ReadSelectedText(context, &selected, location)) {
        return false;
    }
    if (requireExpected &&
        !MatchesExpectedReadback(
            selected,
            expected,
            matchCase,
            readbackPolicy) &&
        !MatchesExpectedReadback(
            selected,
            expected,
            ReadAutomaticNumber(context),
            matchCase,
            readbackPolicy)) {
        return SetError(
            context->result,
            L"STALE_SELECTION_TEXT",
            location,
            L"selected text changed before text.patch");
    }
    Selection confirmed;
    if (!GetSelection(context->hwp, &confirmed, context->result)) {
        return false;
    }
    if (!SameSelection(before, confirmed)) {
        return SetError(
            context->result,
            L"STALE_SELECTION",
            location,
            L"selection changed while text.patch verified its text");
    }
    PreservedTextFormat preserved;
    if (preserveFormat &&
        !CaptureTextFormat(context, &preserved, location)) {
        return false;
    }
    const bool replaced = replacement.empty()
        ? RunAction(context->action, L"Delete", context->result, location)
        : InsertText(context, replacement, location);
    if (!replaced) {
        return false;
    }
    context->result->partialMutation = true;
    Position end;
    if (!GetPosition(context->hwp, &end, context->result)) {
        return false;
    }
    if (replacement.empty()) {
        Selection afterDeletion;
        if (!SamePosition(before.start, end) ||
            !GetSelection(context->hwp, &afterDeletion, context->result) ||
            afterDeletion.selected) {
            return SetError(
                context->result,
                L"TEXT_PATCH_READBACK",
                location,
                L"deleted range did not collapse to its verified start");
        }
        return true;
    }
    if (!SelectTextRange(context, before.start, end, location)) {
        return false;
    }
    if (preserveFormat &&
        !ApplyAndVerifyTextFormat(context, preserved, location)) {
        return false;
    }
    std::wstring inserted;
    if (!ReadSelectedText(context, &inserted, location) ||
        (!MatchesReplacementReadback(
             inserted,
             replacement,
             readbackPolicy) &&
         !MatchesReplacementReadback(
             inserted,
             replacement,
             ReadAutomaticNumber(context),
             readbackPolicy))) {
        return SetError(
            context->result,
            L"TEXT_PATCH_READBACK",
            location,
            L"reselected text does not match the requested replacement");
    }
    return true;
}

bool PatchCurrentText(Context* const context, const Command& command) {
    Selection before;
    if (!GetSelection(context->hwp, &before, context->result)) {
        return false;
    }
    if (before.selected) {
        return PatchSelectedText(
            context,
            command.first,
            command.hasExpectedText,
            true,
            command.second,
            L"current",
            command.preserveFormat,
            ReadbackPolicy::Exact);
    }
    if ((before.mode & kSelectionModeMask) != kSelectionNone) {
        return SetError(
            context->result,
            L"NON_TEXT_SELECTION",
            L"current",
            L"text.patch cannot insert into a non-text selection");
    }
    if (command.hasExpectedText && !command.first.empty()) {
        return SetError(
            context->result,
            L"NO_SELECTION",
            L"current",
            L"expected old text was supplied but the current cursor has no selection");
    }
    Position start;
    if (!GetPosition(context->hwp, &start, context->result) ||
        !InsertText(context, command.second, L"current")) {
        return false;
    }
    context->result->partialMutation = true;
    Position end;
    if (!GetPosition(context->hwp, &end, context->result)) {
        return false;
    }
    if (command.second.empty()) {
        return SamePosition(start, end)
            ? true
            : SetError(
                  context->result,
                  L"TEXT_PATCH_READBACK",
                  L"current",
                  L"empty insertion changed the current position");
    }
    if (!SelectTextRange(context, start, end, L"current")) {
        return false;
    }
    std::wstring inserted;
    if (!ReadSelectedText(context, &inserted, L"current") ||
        !SameParagraphText(inserted, command.second)) {
        return SetError(
            context->result,
            L"TEXT_PATCH_READBACK",
            L"current",
            L"reselected insertion does not match the requested text");
    }
    return true;
}

struct TextMatch {
    Selection selection;
};

std::wstring TextMatchKey(const Selection& match) {
    return std::to_wstring(match.start.list) + L":" +
        std::to_wstring(match.start.paragraph) + L":" +
        std::to_wstring(match.start.character) + L"-" +
        std::to_wstring(match.end.list) + L":" +
        std::to_wstring(match.end.paragraph) + L":" +
        std::to_wstring(match.end.character);
}

std::wstring TextMatchCandidates(const std::vector<TextMatch>& matches) {
    std::wostringstream encoded;
    for (size_t index = 0; index < matches.size(); ++index) {
        if (index != 0) {
            encoded << L'|';
        }
        encoded << TextMatchKey(matches[index].selection);
    }
    return encoded.str();
}

Position TextPositionAtOffset(
    const Position& start,
    const std::wstring& text,
    const size_t offset) {
    Position position = start;
    for (size_t index = 0; index < offset; ++index) {
        if (text[index] == L'\r' &&
            index + 1 < offset &&
            text[index + 1] == L'\n') {
            ++position.paragraph;
            position.character = 0;
            ++index;
        } else if (text[index] == L'\r' || text[index] == L'\n') {
            ++position.paragraph;
            position.character = 0;
        } else {
            ++position.character;
        }
    }
    return position;
}

bool SelectCellTextPatchMatch(
    Context* const context,
    const Command& command,
    const Selection& original,
    const std::wstring& location) {
    constexpr size_t kMaximumCandidates = 24;
    if (!RunAction(context->action, L"SelectAll", context->result, location)) {
        return false;
    }
    Selection cellSelection;
    std::wstring cellText;
    if (!GetSelection(context->hwp, &cellSelection, context->result) ||
        cellSelection.start.list != cellSelection.end.list ||
        !ReadSelectedText(context, &cellText, location)) {
        return SetError(
            context->result,
            L"TEXT_FIND_STATE",
            location,
            L"table cell did not expose one text selection");
    }
    if (cellText.empty()) {
        if ((command.hasExpectedText && !command.first.empty()) ||
            command.occurrence > 1) {
            return SetError(context->result, L"STALE_CELL_TEXT", location,
                            L"empty target cell does not match expected text");
        }
        Position cursor;
        return GetPosition(context->hwp, &cursor, context->result) &&
            SetPosition(context->hwp, cursor, context->result, location);
    }
    if (!cellSelection.selected ||
        (command.hasExpectedText && command.first.empty())) {
        return SetError(context->result, L"STALE_CELL_TEXT", location,
                        L"expected an empty cell but target contains text");
    }
    if (!command.hasExpectedText) {
        return true;
    }
    std::vector<TextMatch> matches;
    for (size_t offset = 0;
         offset + command.first.size() <= cellText.size();) {
        if (!TextMatchesAt(cellText, command.first, offset, command.matchCase)) {
            ++offset;
            continue;
        }
        const Position start =
            TextPositionAtOffset(cellSelection.start, cellText, offset);
        const Position end = TextPositionAtOffset(
            cellSelection.start,
            cellText,
            offset + command.first.size());
        matches.push_back(TextMatch{
            Selection{true, kSelectionText, start, end},
        });
        if (command.occurrence > 0 &&
            matches.size() == static_cast<size_t>(command.occurrence)) {
            return SelectTextRange(context, start, end, location);
        }
        if (command.occurrence == 0 && matches.size() == kMaximumCandidates) {
            break;
        }
        offset += command.first.size();
    }
    if (!RestoreTextPosition(context, original, location + L".restore")) {
        return false;
    }
    if (matches.empty()) {
        return SetError(
            context->result,
            command.occurrence > 0
                ? L"TEXT_OCCURRENCE_NOT_FOUND"
                : L"TEXT_NOT_FOUND",
            location,
            L"text.patch found no matching text in the target cell before mutation");
    }
    if (command.occurrence > 0) {
        return SetError(
            context->result,
            L"TEXT_OCCURRENCE_NOT_FOUND",
            location,
            L"requested text occurrence was not found in the target cell before mutation");
    }
    if (matches.size() > 1) {
        return SetError(
            context->result,
            L"AMBIGUOUS_TEXT_MATCH",
            TextMatchCandidates(matches),
            L"multiple text matches in the target cell require an explicit occurrence");
    }
    return SelectTextRange(
        context,
        matches.front().selection.start,
        matches.front().selection.end,
        location);
}

bool SelectVerifiedScannedText(
    Context* const context,
    const Command& command,
    const Selection& candidate) {
    const std::wstring location = L"text.find";
    const auto stale = [context, &location](const wchar_t* message) {
        return SetError(context->result, L"STALE_SELECTION_TEXT", location, message);
    };
    std::wstring readback;
    const bool selected = SelectTextRange(
        context, candidate.start, candidate.end, location);
    if (!selected && context->result->error.code != L"TEXT_RANGE") {
        return false;
    }
    if (selected && !ReadSelectedText(context, &readback, location)) {
        return false;
    }
    if (selected && MatchesExpectedReadback(readback, command.first,
            command.matchCase, ReadbackPolicy::Exact)) {
        return true;
    }
    std::wstring automaticNumber = selected ? ReadAutomaticNumber(context) : L"";
    if (selected && MatchesExpectedReadback(readback, command.first,
            automaticNumber, command.matchCase, ReadbackPolicy::Exact)) {
        return true;
    }
    context->result->error = {};
    // A scan offset is a hint. Recover inside this exact paragraph, never by
    // rescanning the document or choosing a different repeated occurrence.
    const Position begin{candidate.start.list, candidate.start.paragraph, 0};
    Position end;
    if (!SetPosition(context->hwp, begin, context->result, location) ||
        !RunVerifiedNavigationAction(context->action, L"MoveParaEnd", context->result, location) ||
        !GetPosition(context->hwp, &end, context->result)) {
        return false;
    }
    if (end.list != begin.list || end.paragraph != begin.paragraph || end.character <= 0) {
        return stale(L"could not read the original paragraph for coordinate recovery");
    }
    automaticNumber = ReadAutomaticNumber(context);
    const auto read = [&](const Position& first, const Position& last, std::wstring* text) {
        if (!SelectTextRange(context, first, last, location) ||
            !ReadSelectedText(context, text, location)) {
            return false;
        }
        const std::wstring prefix = automaticNumber.empty() ? L"" : automaticNumber + L' ';
        if (!prefix.empty() && text->compare(0, prefix.size(), prefix) == 0) {
            text->erase(0, prefix.size());
        }
        return true;
    };
    std::wstring paragraph;
    if (!read(begin, end, &paragraph)) { return false; }
    if (paragraph.size() > static_cast<size_t>(end.character) ||
        paragraph.find_first_of(L"\r\n") != std::wstring::npos) {
        return stale(L"coordinate recovery did not expose one paragraph");
    }
    std::wstring folded = paragraph;
    std::wstring needle = command.first;
    if (!command.matchCase) {
        const auto lower = [](wchar_t c) { return static_cast<wchar_t>(towlower(c)); };
        std::transform(folded.begin(), folded.end(), folded.begin(), lower);
        std::transform(needle.begin(), needle.end(), needle.begin(), lower);
    }
    const size_t offset = folded.find(needle);
    if (offset == std::wstring::npos || folded.find(needle, offset + 1) != std::wstring::npos) {
        return stale(L"coordinate recovery requires one literal in the original paragraph");
    }
    LONG character = static_cast<LONG>(offset);
    if (paragraph.size() != static_cast<size_t>(end.character)) {
        // Hidden controls can occupy native cells without contributing visible
        // text. Locate the visible prefix boundary in O(log paragraph length)
        // native reads instead of probing every nearby coordinate over IPC.
        LONG low = 0;
        LONG high = end.character;
        while (low < high) {
            const LONG middle = low + (high - low) / 2 + 1;
            std::wstring prefix;
            if (!read(begin, Position{begin.list, begin.paragraph, middle}, &prefix)) {
                return false;
            }
            if (paragraph.compare(0, prefix.size(), prefix) != 0) {
                return stale(L"paragraph changed while recovering its coordinate");
            }
            if (prefix.size() <= offset) { low = middle; }
            else { high = middle - 1; }
        }
        character = low;
    }
    if (command.first.size() > static_cast<size_t>(end.character - character)) {
        return stale(L"recovered literal exceeds its paragraph");
    }
    const Position from{begin.list, begin.paragraph, character};
    const Position to{begin.list, begin.paragraph,
        character + static_cast<LONG>(command.first.size())};
    if (!read(from, to, &readback)) { return false; }
    if (!MatchesExpectedReadback(readback, command.first, command.matchCase, ReadbackPolicy::Exact)) {
        return stale(L"recovered range does not contain the exact requested literal");
    }
    return true;
}

// GetText/MovePos(201) expose native scan positions without invoking a find
// action or inheriting the user's last regex/whole-word/search-dialog settings.
class TextPatchScan final {
public:
    TextPatchScan(IDispatch* const hwp, const bool acquired, HRESULT* const releaseStatus)
        : hwp_(hwp), acquired_(acquired), releaseStatus_(releaseStatus) {}
    ~TextPatchScan() { Close(); }
    HRESULT Close() {
        if (!acquired_) {
            return *releaseStatus_;
        }
        acquired_ = false;
        *releaseStatus_ = Method(hwp_, L"ReleaseScan", {}, nullptr);
        return *releaseStatus_;
    }
private:
    IDispatch* hwp_;
    bool acquired_;
    HRESULT* releaseStatus_;
};

bool SelectTextPatchMatch(
    Context* const context,
    const Command& command,
    const Selection& original,
    const LONG listFilter) {
    constexpr size_t kMaximumSearches = 20'000;
    constexpr size_t kMaximumScanCharacters = 16 * 1024 * 1024;
    const auto fail = [context](const wchar_t* code, const wchar_t* message) {
        return SetError(context->result, code, L"text.find", message);
    };
    if (command.first.empty() ||
        command.first.find_first_of(L"\r\n") != std::wstring::npos) {
        return fail(L"TEXT_FIND_STATE",
            L"text lookup requires one paragraph literal; use an exact range "
            L"or table cell target for multiple paragraphs");
    }
    std::vector<TextMatch> matches;
    bool exhausted = false;
    bool occurrenceFound = false;
    HRESULT releaseStatus = S_OK;
    const bool scanned = [&]() {
        if (!SetPosition(context->hwp,
                Position{listFilter < 0 ? 0 : listFilter, 0, 0},
                context->result, L"text.find")) {
            return false;
        }
        CComVariant raw;
        // A scoped cell stays in its list. Document lookup includes sublists
        // and control boundaries; text is never joined across those boundaries.
        const HRESULT initialized = Method(context->hwp, L"InitScan",
            {CComVariant(listFilter < 0 ? 0x0FL : 0L),
             CComVariant(listFilter < 0 ? 0x0077L : 0x0055L),
             CComVariant(0L), CComVariant(0L), CComVariant(0L), CComVariant(0L)},
            &raw);
        TextPatchScan scan(context->hwp, SUCCEEDED(initialized), &releaseStatus);
        bool started = false;
        if (FAILED(initialized) || FAILED(AsBool(raw, &started)) || !started) {
            return fail(L"TEXT_SCAN_STATE", L"native text scan could not start");
        }
        // Linear literal matching, including a literal split across contiguous
        // scan chunks. Repeated prefixes in a long paragraph must not cause
        // quadratic comparisons or a separate document scan per occurrence.
        std::wstring needle = command.first;
        if (!command.matchCase) {
            std::transform(needle.begin(), needle.end(), needle.begin(),
                [](const wchar_t character) { return static_cast<wchar_t>(towlower(character)); });
        }
        std::vector<size_t> prefix(needle.size(), 0);
        for (size_t index = 1, matched = 0; index < needle.size(); ++index) {
            while (matched > 0 && needle[index] != needle[matched]) {
                matched = prefix[matched - 1];
            }
            if (needle[index] == needle[matched]) { ++matched; }
            prefix[index] = matched;
        }
        size_t matched = 0;
        Position previousEnd{-1, -1, -1};
        size_t characters = 0;
        size_t occurrences = 0;
        std::set<std::wstring> seen;
        for (size_t step = 0; step < kMaximumSearches; ++step) {
            CComBSTR chunk;
            CComVariant argument;
            argument.vt = VT_BSTR | VT_BYREF;
            argument.pbstrVal = &chunk.m_str;
            LONG state = 0;
            if (FAILED(Method(context->hwp, L"GetText", {argument}, &raw)) ||
                FAILED(AsLong(raw, &state)) || state < 0 || state > 5) {
                return fail(L"TEXT_SCAN_STATE", L"native text scan failed before mutation");
            }
            const size_t length = chunk.Length();
            if (length > kMaximumScanCharacters - characters) {
                return fail(L"TEXT_SEARCH_LIMIT", L"native text scan exceeded its character budget");
            }
            characters += length;
            if (length != 0) {
                bool moved = false;
                Position start;
                if (!CallBooleanMethod(context->hwp, L"MovePos",
                        {CComVariant(201L), CComVariant(0L), CComVariant(0L)},
                        &moved, context->result, L"text.find") || !moved ||
                    !GetPosition(context->hwp, &start, context->result)) {
                    return fail(L"TEXT_SCAN_STATE", L"native scan position is unavailable");
                }
                if (listFilter >= 0 && start.list != listFilter) {
                    return fail(L"TEXT_SCAN_STATE", L"native scan left the requested cell list");
                }
                std::wstring_view text(chunk.m_str, length);
                while (!text.empty() && (text.back() == L'\r' || text.back() == L'\n')) {
                    text.remove_suffix(1);
                }
                if (start.character < 0 || text.size() > static_cast<size_t>(
                        (std::numeric_limits<LONG>::max)() - start.character)) {
                    return fail(L"TEXT_SCAN_STATE", L"native scan position exceeds the text range limit");
                }
                // Only contiguous chunks of the SAME native paragraph can
                // contribute to one literal, never text across a control.
                if (!SamePosition(previousEnd, start) || state == 4 || state == 5) {
                    matched = 0;
                }
                previousEnd = Position{start.list, start.paragraph,
                    start.character + static_cast<LONG>(text.size())};
                for (size_t offset = 0; offset < text.size(); ++offset) {
                    const wchar_t character = command.matchCase ? text[offset] :
                        static_cast<wchar_t>(towlower(text[offset]));
                    while (matched > 0 && character != needle[matched]) {
                        matched = prefix[matched - 1];
                    }
                    if (character == needle[matched]) { ++matched; }
                    if (matched != needle.size()) {
                        continue;
                    }
                    const Position to{start.list, start.paragraph,
                        start.character + static_cast<LONG>(offset + 1)};
                    const Position from{to.list, to.paragraph,
                        to.character - static_cast<LONG>(needle.size())};
                    const Selection selection{true, kSelectionText, from, to};
                    matched = 0; // Preserve non-overlapping occurrence semantics.
                    if (seen.insert(TextMatchKey(selection)).second) {
                        ++occurrences;
                        if (command.occurrence == 0 ||
                            occurrences == static_cast<size_t>(command.occurrence)) {
                            matches.push_back(TextMatch{selection});
                        }
                        occurrenceFound = command.occurrence > 0 &&
                            occurrences == static_cast<size_t>(command.occurrence);
                        if (occurrenceFound || matches.size() == 2) {
                            break;
                        }
                    }
                }
                if (state != 2 || text.size() != length) {
                    matched = 0;
                }
            } else {
                matched = 0;
            }
            // State 0 means no text, not end of document (e.g. empty controls).
            exhausted = state == 1;
            if (exhausted || occurrenceFound || matches.size() == 2) {
                break;
            }
        }
        if (FAILED(scan.Close())) {
            return fail(L"TEXT_SCAN_STATE", L"native text scan could not be released");
        }
        return true;
    }();
    if (FAILED(releaseStatus)) {
        context->result->retrySafe = false;
        fail(L"TEXT_SCAN_STATE", L"native text scan could not be released");
    }
    if (!scanned || FAILED(releaseStatus) ||
        (!occurrenceFound && (matches.size() != 1 || !exhausted))) {
        if (!RestoreTextPosition(context, original, L"text.find.restore")) {
            context->result->retrySafe = false;
            return false;
        }
        if (!scanned || FAILED(releaseStatus)) {
            return false;
        }
        if (matches.size() > 1) {
            return SetError(context->result, L"AMBIGUOUS_TEXT_MATCH",
                TextMatchCandidates(matches),
                L"multiple text matches require an explicit range, cell, or occurrence");
        }
        return fail(!exhausted ? L"TEXT_SEARCH_LIMIT" :
                command.occurrence > 0 ? L"TEXT_OCCURRENCE_NOT_FOUND" : L"TEXT_NOT_FOUND",
            L"native text lookup did not resolve a unique target before mutation");
    }
    // Scanning never mutates or selects text. Release it before selecting the
    // exact literal span; PatchSelectedText / preflight still verify readback.
    if (SelectVerifiedScannedText(context, command, matches.front().selection)) {
        return true;
    }
    const Error error = context->result->error;
    if (!RestoreTextPosition(context, original, L"text.find.restore")) {
        context->result->retrySafe = false;
        return false;
    }
    context->result->error = error;
    return false;
}

bool PatchSearchedText(
    Context* const context,
    const Command& command,
    const LONG listFilter,
    const Selection& original,
    const std::wstring& location) {
    if (command.occurrence < 0 || command.occurrence > 20'000) {
        return SetError(
            context->result,
            L"TEXT_OCCURRENCE",
            location,
            L"text occurrence must be between 1 and 20000 when supplied");
    }
    return SelectTextPatchMatch(context, command, original, listFilter) &&
        PatchSelectedText(
            context,
            command.first,
            true,
            command.matchCase,
            command.second,
            location,
            command.preserveFormat,
            ReadbackPolicy::Exact);
}

using ShadowCellKey = std::pair<std::wstring, std::wstring>;

bool SelectTableForCellPatch(
    Context* const context,
    const std::wstring& tableId) {
    CComPtr<IDispatch> selectedControl;
    if (!SelectControl(context, tableId, selectedControl)) {
        return false;
    }
    // SelectControl verifies the live instance id; GoToCell still verifies the
    // cached list's parent and address. COM wrapper identity is not stable.
    const bool safeToReuse =
        context->reuseTextPatchTopology &&
        context->table != nullptr &&
        context->tableId == tableId;
    if (safeToReuse) {
        // Keep the cell map; only refresh the wrapper to the live selection.
        context->table = selectedControl;
        return true;
    }
    if (!CaptureCurrentTable(context)) {
        return false;
    }
    return context->tableId == tableId ||
        SetError(
            context->result,
            L"WRONG_CONTROL",
            tableId,
            L"captured table identity does not match the text.patch target");
}

bool PatchText(Context* const context, const Command& command) {
    if (command.preflightOnly) {
        return SetError(
            context->result,
            L"TEXT_PATCH_PREFLIGHT_ONLY",
            command.name,
            L"prepared text target records cannot execute as mutations");
    }
    if (command.name == L"CURRENT") {
        return PatchCurrentText(context, command);
    }
    if (command.name == L"RANGE") {
        const Position start{command.list, command.paragraph, command.character};
        const Position end{
            command.endList,
            command.endParagraph,
            command.endCharacter};
        return SelectTextRange(context, start, end, L"range") &&
            PatchSelectedText(
                context,
                command.first,
                true,
                true,
                command.second,
                L"range",
                command.preserveFormat,
                ReadbackPolicy::Exact);
    }
    Selection original;
    if (!GetSelection(context->hwp, &original, context->result)) {
        return false;
    }
    if (command.name == L"FIND") {
        LONG listFilter = -1;
        std::wstring location = L"text.find";
        if (!command.tableInstanceId.empty()) {
            if (command.cellAddress.empty() ||
                !SelectTableForCellPatch(context, command.tableInstanceId) ||
                !GoToCell(context, command.cellAddress)) {
                return false;
            }
            Position cell;
            if (!GetPosition(context->hwp, &cell, context->result)) {
                return false;
            }
            listFilter = cell.list;
            location = NormalizeAddress(command.cellAddress);
        }
        return PatchSearchedText(
            context,
            command,
            listFilter,
            original,
            location);
    }
    if (command.name == L"CELL") {
        if (!SelectTableForCellPatch(context, command.tableInstanceId) ||
            !GoToCell(context, command.cellAddress)) {
            return false;
        }
        const std::wstring address = NormalizeAddress(command.cellAddress);
        if (!SelectCellTextPatchMatch(context, command, original, address)) {
            return false;
        }
        Selection cellSelection;
        if (!GetSelection(context->hwp, &cellSelection, context->result)) {
            return false;
        }
        if (!cellSelection.selected) {
            return PatchCurrentText(context, command);
        }
        return
            PatchSelectedText(
                context,
                command.first,
                true,
                command.matchCase,
                command.second,
                address,
                command.preserveFormat,
                ReadbackPolicy::Exact);
    }
    return SetError(
        context->result,
        L"TEXT_PATCH_TARGET",
        command.name,
        L"unsupported text.patch target");
}

enum class TableTextBatchKind {
    None,
    Fill,
    Expand,
};

bool IsTextPatchFormatBatch(const Request& request) {
    bool hasTextPatch = false;
    for (const Command& command : request.commands) {
        if (command.kind == CommandKind::TextPatch) {
            hasTextPatch = true;
            continue;
        }
        if (command.kind == CommandKind::Action &&
            ((command.name == L"CharShape" &&
              command.parameterSet == L"HCharShape") ||
             (command.name == L"ParagraphShape" &&
              command.parameterSet == L"HParaShape"))) {
            continue;
        }
        return false;
    }
    return hasTextPatch;
}

TableTextBatchKind ClassifyTableTextBatch(const Request& request) {
    if (request.commands.size() < 3 ||
        request.commands[0].kind != CommandKind::SelectControl ||
        request.commands[1].kind != CommandKind::CaptureTable) {
        return TableTextBatchKind::None;
    }
    const std::wstring& selectedTableId = request.commands[0].first;
    bool hasTextMutation = false;
    bool hasExpansionAnchor = false;
    bool hasAppendRow = false;
    for (size_t index = 2; index < request.commands.size(); ++index) {
        const Command& command = request.commands[index];
        if (!hasTextMutation && !hasExpansionAnchor && index == 2 &&
            command.kind == CommandKind::Cell) {
            hasExpansionAnchor = true;
            continue;
        }
        if (!hasTextMutation && hasExpansionAnchor &&
            command.kind == CommandKind::Run &&
            command.name == L"TableAppendRow") {
            hasAppendRow = true;
            continue;
        }
        if (hasExpansionAnchor && !hasAppendRow) {
            return TableTextBatchKind::None;
        }
        if (command.kind == CommandKind::SetCellText) {
            hasTextMutation = true;
            continue;
        }
        if (command.kind == CommandKind::TextPatch &&
            command.name == L"CELL" &&
            command.tableInstanceId == selectedTableId) {
            hasTextMutation = true;
            continue;
        }
        return TableTextBatchKind::None;
    }
    if (!hasTextMutation) {
        return TableTextBatchKind::None;
    }
    return hasAppendRow ? TableTextBatchKind::Expand : TableTextBatchKind::Fill;
}

bool ValidateCellTextPatchLimit(
    ExecutionResult* const result,
    const Request& request) {
    constexpr size_t kMaximumCellTextPatches = 100;
    if (request.commands.size() <= kMaximumCellTextPatches) {
        return true;
    }
    std::map<ShadowCellKey, size_t> patchCounts;
    for (const Command& command : request.commands) {
        if (command.kind != CommandKind::TextPatch ||
            command.name != L"CELL") {
            continue;
        }
        const std::wstring address = NormalizeAddress(command.cellAddress);
        const ShadowCellKey key{command.tableInstanceId, address};
        const size_t count = ++patchCounts[key];
        if (count <= kMaximumCellTextPatches) {
            continue;
        }
        result->failedStep = CommandStep(command);
        return SetError(
            result,
            L"TEXT_PATCH_LIMIT",
            command.tableInstanceId + L":" + address,
            L"a single table cell cannot contain more than 100 text.patch commands");
    }
    return true;
}

bool ReadShadowCell(
    Context* const context,
    const std::wstring& tableId,
    const std::wstring& requestedAddress,
    std::map<ShadowCellKey, std::wstring>* const cells,
    std::wstring** const value) {
    const std::wstring address = NormalizeAddress(requestedAddress);
    const ShadowCellKey key{tableId, address};
    const auto existing = cells->find(key);
    if (existing != cells->end()) {
        *value = &existing->second;
        return true;
    }
    if (!SelectTableForCellPatch(context, tableId) ||
        !GoToCell(context, address) ||
        !RunAction(context->action, L"SelectAll", context->result, address)) {
        return false;
    }
    std::wstring current;
    if (!ReadSelectedText(context, &current, address)) {
        return false;
    }
    const auto inserted = cells->emplace(key, std::move(current));
    *value = &inserted.first->second;
    return true;
}

bool ApplyShadowPatch(
    ExecutionResult* const result,
    const Command& command,
    const std::wstring& address,
    std::wstring* const value) {
    if (command.occurrence < 0 || command.occurrence > 20'000) {
        return SetError(
            result,
            L"TEXT_OCCURRENCE",
            address,
            L"text occurrence must be between 1 and 20000 when supplied");
    }
    if (command.first.empty()) {
        if (command.name == L"CELL" && value->empty() && command.occurrence <= 1) {
            *value = command.second;
            return true;
        }
        return SetError(
            result,
            L"STALE_CELL_TEXT",
            address,
            L"empty expected text requires an empty target cell");
    }
    std::vector<size_t> matches;
    for (size_t offset = 0;
         offset + command.first.size() <= value->size();) {
        if (TextMatchesAt(
                *value,
                command.first,
                offset,
                command.matchCase)) {
            matches.push_back(offset);
            offset += command.first.size();
        } else {
            ++offset;
        }
    }
    if (matches.empty()) {
        return SetError(
            result,
            command.occurrence > 0
                ? L"TEXT_OCCURRENCE_NOT_FOUND"
                : L"TEXT_NOT_FOUND",
            address,
            L"text.patch found no matching text during table preflight");
    }
    size_t selected = 0;
    if (command.occurrence > 0) {
        const size_t requested =
            static_cast<size_t>(command.occurrence);
        if (requested > matches.size()) {
            return SetError(
                result,
                L"TEXT_OCCURRENCE_NOT_FOUND",
                address,
                L"requested occurrence was not found during table preflight");
        }
        selected = matches[requested - 1];
    } else {
        if (matches.size() != 1) {
            return SetError(
                result,
                L"AMBIGUOUS_TEXT_MATCH",
                address,
                L"multiple table text matches require an explicit occurrence");
        }
        selected = matches.front();
    }
    value->replace(selected, command.first.size(), command.second);
    return true;
}

bool PreflightTableTextCommands(
    Context* const context,
    const Request& request) {
    context->reuseTextPatchTopology = false;
    if (!ValidateCellTextPatchLimit(context->result, request)) {
        return false;
    }
    const TableTextBatchKind batchKind = ClassifyTableTextBatch(request);
    context->reuseTextPatchTopology =
        batchKind == TableTextBatchKind::Fill || IsTextPatchFormatBatch(request);
    if (batchKind == TableTextBatchKind::None) {
        return true;
    }
    Position cursor;
    Selection selection;
    if (!GetPosition(context->hwp, &cursor, context->result) ||
        !GetSelection(
            context->hwp,
            &selection,
            context->result,
            SelectionCapturePolicy::RequiredControl)) {
        return false;
    }
    if (!hancom::com_state::CanRestoreSelection(selection)) {
        return SetError(
            context->result,
            L"UNSUPPORTED_SELECTION",
            L"table.preflight",
            L"the active HWP selection cannot be restored after table preflight");
    }
    std::map<ShadowCellKey, std::wstring> cells;
    const std::wstring selectedTableId = request.commands[0].first;
    bool validated = true;
    for (size_t index = 2; index < request.commands.size(); ++index) {
        const Command& command = request.commands[index];
        if (command.kind == CommandKind::Cell ||
            command.kind == CommandKind::Run) {
            continue;
        }
        if (batchKind == TableTextBatchKind::Expand &&
            command.kind == CommandKind::SetCellText &&
            !command.hasExpectedText) {
            // TableAppendRow creates these cells later. Existing cells with
            // expected text are still checked before any row is appended.
            continue;
        }
        context->result->failedStep = CommandStep(command);
        const std::wstring tableId =
            command.kind == CommandKind::TextPatch
            ? command.tableInstanceId
            : selectedTableId;
        const std::wstring address = NormalizeAddress(
            command.kind == CommandKind::TextPatch
            ? command.cellAddress
            : command.first);
        std::wstring* current = nullptr;
        if (!ReadShadowCell(
                context,
                tableId,
                address,
                &cells,
                &current)) {
            validated = false;
            break;
        }
        if (command.kind == CommandKind::SetCellText) {
            if (command.hasExpectedText &&
                !SameParagraphText(*current, command.expectedText)) {
                SetError(
                    context->result,
                    L"STALE_CELL_TEXT",
                    address,
                    L"cell text changed before the table fill batch");
                validated = false;
                break;
            }
            *current = command.second;
        } else if (!ApplyShadowPatch(
                       context->result,
                       command,
                       address,
                       current)) {
            validated = false;
            break;
        }
    }
    const Error validationError = context->result->error;
    const std::wstring validationStep = context->result->failedStep;
    const bool restored = hancom::com_state::RestoreSelection(
        context->hwp,
        cursor,
        selection);
    context->table = nullptr;
    context->tableId.clear();
    context->currentCell.clear();
    context->topology.Clear();
    if (!restored) {
        context->result->retrySafe = false;
        context->result->failedStep = L"table.preflight";
        return SetError(
            context->result,
            L"STATE_RESTORE",
            L"table.preflight",
            L"cursor or selection restoration failed after table preflight");
    }
    if (!validated) {
        context->result->error = validationError;
        context->result->failedStep = validationStep;
        return false;
    }
    context->result->failedStep.clear();
    return true;
}

bool PositionBefore(const Position& left, const Position& right) noexcept {
    if (left.list != right.list) {
        return left.list < right.list;
    }
    if (left.paragraph != right.paragraph) {
        return left.paragraph < right.paragraph;
    }
    return left.character < right.character;
}

bool SelectionsOverlap(const Selection& left, const Selection& right) noexcept {
    return left.start.list == right.start.list &&
        PositionBefore(left.start, right.end) &&
        PositionBefore(right.start, left.end);
}

bool VerifyPreflightSelection(
    Context* const context,
    const Command& command,
    const ReadbackPolicy policy,
    const std::wstring& location) {
    if (!command.hasExpectedText) {
        return true;
    }
    std::wstring selected;
    return ReadSelectedText(context, &selected, location) &&
        (MatchesExpectedReadback(
             selected,
             command.first,
             command.matchCase,
             policy) ||
         MatchesExpectedReadback(
             selected,
             command.first,
             ReadAutomaticNumber(context),
             command.matchCase,
             policy) ||
         SetError(
             context->result,
             L"STALE_SELECTION_TEXT",
             location,
             L"selected text changed before text.patch preflight"));
}

struct LiteralParagraph {
    Position end;
    std::wstring text;
    std::wstring automaticNumber;
    std::map<LONG, size_t> prefixLengths;
    std::map<std::pair<LONG, bool>, LONG> boundaries;
};

using LiteralParagraphs = std::map<std::pair<LONG, LONG>, LiteralParagraph>;

// Inspected text offsets are not HWP cursor positions. Resolve both boundaries
// inside the observed paragraph, before any mutation, and share reads between
// targets. No guessed coordinates, document searches, or IPC retry loop.
bool SelectInspectedLiteral(
    Context* const context, const Command& command, LiteralParagraphs* const cache) {
    const std::wstring location = L"text.patch.literal_range";
    const auto stale = [&](const wchar_t* message) {
        return SetError(context->result, L"STALE_SELECTION_TEXT", location, message);
    };
    const Position begin{command.list, command.paragraph, 0};
    const auto key = std::make_pair(begin.list, begin.paragraph);
    auto entry = cache->find(key);
    if (entry == cache->end()) {
        LiteralParagraph paragraph;
        if (!SetPosition(context->hwp, begin, context->result, location) ||
            !RunVerifiedNavigationAction(context->action, L"MoveParaEnd", context->result, location) ||
            !GetPosition(context->hwp, &paragraph.end, context->result)) {
            return false;
        }
        if (paragraph.end.list != begin.list || paragraph.end.paragraph != begin.paragraph ||
            paragraph.end.character <= 0) {
            return stale(L"native paragraph extent is unavailable");
        }
        paragraph.automaticNumber = ReadAutomaticNumber(context);
        if (!SelectTextRange(context, begin, paragraph.end, location) ||
            !ReadSelectedText(context, &paragraph.text, location)) {
            return false;
        }
        const std::wstring prefix = paragraph.automaticNumber.empty() ? L"" :
            paragraph.automaticNumber + L' ';
        // Strip only a number actually reported by HWP and only when it is
        // absent from the inspected source. Never guess a numbering prefix.
        if (paragraph.text != command.paragraphText && !prefix.empty() &&
            paragraph.text.compare(0, prefix.size(), prefix) == 0) {
            paragraph.text.erase(0, prefix.size());
        } else {
            paragraph.automaticNumber.clear();
        }
        if (paragraph.text != command.paragraphText ||
            paragraph.text.size() > static_cast<size_t>(paragraph.end.character)) {
            return stale(L"inspected paragraph no longer matches the native paragraph");
        }
        paragraph.prefixLengths.emplace(0, 0);
        paragraph.prefixLengths.emplace(paragraph.end.character, paragraph.text.size());
        entry = cache->emplace(key, std::move(paragraph)).first;
    }
    LiteralParagraph& paragraph = entry->second;
    if (paragraph.text != command.paragraphText) {
        return stale(L"conflicting inspected text for one paragraph");
    }
    const auto prefixLength = [&](LONG character, size_t* length) {
        const auto prior = paragraph.prefixLengths.find(character);
        if (prior != paragraph.prefixLengths.end()) {
            *length = prior->second;
            return true;
        }
        std::wstring text;
        if (!SelectTextRange(context, begin, Position{begin.list, begin.paragraph, character}, location) ||
            !ReadSelectedText(context, &text, location)) {
            return false;
        }
        const std::wstring number = paragraph.automaticNumber.empty() ? L"" :
            paragraph.automaticNumber + L' ';
        if (!number.empty() && text.compare(0, number.size(), number) == 0) {
            text.erase(0, number.size());
        }
        if (paragraph.text.compare(0, text.size(), text) != 0) {
            return stale(L"native prefix does not match inspected paragraph");
        }
        *length = text.size();
        paragraph.prefixLengths.emplace(character, *length);
        return true;
    };
    const auto boundary = [&](LONG offset, bool start, LONG* position) {
        const auto boundaryKey = std::make_pair(offset, start);
        const auto prior = paragraph.boundaries.find(boundaryKey);
        if (prior != paragraph.boundaries.end()) {
            *position = prior->second;
            return true;
        }
        LONG low = 0;
        LONG high = paragraph.end.character;
        if (paragraph.text.size() == static_cast<size_t>(high)) {
            low = high = offset;
        }
        // Start excludes invisible cells before the literal; end excludes
        // invisible cells after it. They are deliberately resolved separately.
        while (low < high) {
            const LONG middle = low + (high - low) / 2 + (start ? 1 : 0);
            size_t length = 0;
            if (!prefixLength(middle, &length)) { return false; }
            if (start) {
                if (length <= static_cast<size_t>(offset)) { low = middle; }
                else { high = middle - 1; }
            } else {
                if (length < static_cast<size_t>(offset)) { low = middle + 1; }
                else { high = middle; }
            }
        }
        size_t length = 0;
        if (!prefixLength(low, &length)) { return false; }
        if (length != static_cast<size_t>(offset)) {
            return stale(L"literal boundary is not representable as a native position");
        }
        *position = low;
        paragraph.boundaries.emplace(boundaryKey, low);
        return true;
    };
    LONG first = 0;
    LONG last = 0;
    if (!boundary(command.character, true, &first) ||
        !boundary(command.endCharacter, false, &last)) {
        return false;
    }
    if (last - first != command.endCharacter - command.character) {
        return SetError(context->result, L"TEXT_RANGE_CONTROL", location,
            L"literal spans an embedded control; split at the control before editing");
    }
    return SelectTextRange(context, Position{begin.list, begin.paragraph, first},
        Position{begin.list, begin.paragraph, last}, location);
}

bool ResolveTextPatchTarget(
    Context* const context,
    const Command& command,
    const Selection& original,
    Selection* const resolved,
    LiteralParagraphs* const paragraphs) {
    ReadbackPolicy policy = ReadbackPolicy::Exact;
    std::wstring location = L"current";
    if (command.name == L"CURRENT") {
        Selection current;
        if (!GetSelection(context->hwp, &current, context->result)) {
            return false;
        }
        if (current.selected) {
            *resolved = current;
        } else if ((current.mode & kSelectionModeMask) != kSelectionNone) {
            return SetError(
                context->result,
                L"NON_TEXT_SELECTION",
                location,
                L"text.patch cannot use a non-text selection");
        } else if (command.hasExpectedText && !command.first.empty()) {
            return SetError(
                context->result,
                L"NO_SELECTION",
                location,
                L"expected old text requires a current selection");
        } else {
            Position cursor;
            if (!GetPosition(context->hwp, &cursor, context->result)) {
                return false;
            }
            *resolved = Selection{false, kSelectionNone, cursor, cursor};
            return true;
        }
    } else if (command.name == L"RANGE") {
        location = L"range";
        const Position start{command.list, command.paragraph, command.character};
        const Position end{
            command.endList,
            command.endParagraph,
            command.endCharacter};
        if (!(command.literalRange ? SelectInspectedLiteral(context, command, paragraphs) :
                SelectTextRange(context, start, end, location))) {
            return false;
        }
    } else if (command.name == L"FIND") {
        if (command.occurrence < 0 || command.occurrence > 20'000) {
            return SetError(
                context->result,
                L"TEXT_OCCURRENCE",
                L"text.find",
                L"text occurrence must be between 1 and 20000 when supplied");
        }
        LONG listFilter = -1;
        location = L"text.find";
        if (!command.tableInstanceId.empty()) {
            if (command.cellAddress.empty() ||
                !SelectTableForCellPatch(context, command.tableInstanceId) ||
                !GoToCell(context, command.cellAddress)) {
                return false;
            }
            Position cell;
            if (!GetPosition(context->hwp, &cell, context->result)) {
                return false;
            }
            listFilter = cell.list;
            location = NormalizeAddress(command.cellAddress);
        }
        policy = ReadbackPolicy::Exact;
        if (!SelectTextPatchMatch(context, command, original, listFilter)) {
            return false;
        }
    } else if (command.name == L"CELL") {
        if (command.occurrence < 0 || command.occurrence > 20'000) {
            return SetError(
                context->result,
                L"TEXT_OCCURRENCE",
                command.cellAddress,
                L"text occurrence must be between 1 and 20000 when supplied");
        }
        location = NormalizeAddress(command.cellAddress);
        if (!SelectTableForCellPatch(context, command.tableInstanceId) ||
            !GoToCell(context, command.cellAddress) ||
            !SelectCellTextPatchMatch(context, command, original, location)) {
            return false;
        }
    } else {
        return SetError(
            context->result,
            L"TEXT_PATCH_TARGET",
            command.name,
            L"unsupported text.patch preflight target");
    }
    if (!GetSelection(context->hwp, resolved, context->result)) {
        return false;
    }
    if (!resolved->selected && command.name == L"CELL") {
        Position cursor;
        if (!GetPosition(context->hwp, &cursor, context->result)) {
            return false;
        }
        *resolved = Selection{false, kSelectionNone, cursor, cursor};
        return true;  // SelectCellTextPatchMatch already verified the empty cell.
    }
    if (!resolved->selected) {
        return SetError(
            context->result,
            L"TEXT_FIND_STATE",
            location,
            L"text.patch preflight did not resolve one text selection");
    }
    return VerifyPreflightSelection(context, command, policy, location);
}

ExecutionResult PreflightTextPatchTargets(
    IDispatch* const hwp,
    const Request& request) noexcept {
    const auto started = std::chrono::steady_clock::now();
    ExecutionResult result;
    Context context;
    context.hwp = hwp;
    context.request = &request;
    context.result = &result;
    context.reuseTextPatchTopology = true;
    const auto finish = [&]() {
        result.elapsedMicroseconds = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - started).count();
        result.retrySafe = !result.partialMutation;
        return result;
    };
    try {
        if (hwp == nullptr ||
            !ValidateCommandOrder(request, &result) ||
            !ValidateDocumentIdentity(hwp, request, &result) ||
            !GetDispatchProperty(hwp, L"HAction", context.action, &result)) {
            return finish();
        }
        Position cursor;
        Selection original;
        if (!GetPosition(hwp, &cursor, &result) ||
            !GetSelection(
                hwp,
                &original,
                &result,
                SelectionCapturePolicy::RequiredControl) ||
            !hancom::com_state::CanRestoreSelection(original)) {
            SetError(
                &result,
                L"UNSUPPORTED_SELECTION",
                L"text.patch.preflight",
                L"the active selection cannot be restored after preflight");
            return finish();
        }
        std::vector<Selection> targets;
        LiteralParagraphs paragraphs;
        size_t requestIndex = 0;
        for (size_t commandIndex = 0;
             commandIndex < request.commands.size();
             ++commandIndex) {
            const Command& command = request.commands[commandIndex];
            if (command.kind != CommandKind::TextPatch) {
                continue;
            }
            result.failedRequestIndex = requestIndex;
            Selection target;
            bool resolved = ResolveTextPatchTarget(
                &context,
                command,
                original,
                &target,
                &paragraphs);
            PreparedTextPatchTarget captured;
            if (resolved) {
                captured.startList = target.start.list;
                captured.startParagraph = target.start.paragraph;
                captured.startCharacter = target.start.character;
                captured.endList = target.end.list;
                captured.endParagraph = target.end.paragraph;
                captured.endCharacter = target.end.character;
                resolved = ReadSelectedText(
                    &context,
                    &captured.text,
                    L"text.patch.preflight.inverse") &&
                    CapturePreparedTextFormat(
                        &context,
                        request,
                        commandIndex,
                        &captured,
                        L"text.patch.preflight.inverse");
            }
            const Error resolutionError = result.error;
            const bool restored = hancom::com_state::RestoreSelection(
                hwp,
                cursor,
                original);
            context.currentCell.clear();
            if (!restored) {
                result.retrySafe = false;
                SetError(
                    &result,
                    L"STATE_RESTORE",
                    L"text.patch.preflight",
                    L"cursor or selection restoration failed after preflight");
                return finish();
            }
            if (!resolved) {
                result.error = resolutionError;
                return finish();
            }
            if (std::any_of(
                    targets.begin(),
                    targets.end(),
                    [&target](const Selection& prior) {
                        return SelectionsOverlap(prior, target);
                    })) {
                SetError(
                    &result,
                    L"OVERLAPPING_TEXT_TARGET",
                    L"text.patch.preflight",
                    L"text.patch targets overlap in the prepared document state");
                return finish();
            }
            targets.push_back(target);
            result.preparedTextPatchTargets.push_back(std::move(captured));
            ++requestIndex;
        }
        result.preflightTargetCount = targets.size();
        result.succeeded = true;
    } catch (...) {
        SetError(
            &result,
            L"NATIVE_EXCEPTION",
            L"text.patch.preflight",
            L"text.patch preflight failed unexpectedly");
    }
    return finish();
}

}

namespace hancom::actions {

ExecutionResult PreflightTextPatches(
    IDispatch* const hwp,
    const Request& request) noexcept {
    return detail::PreflightTextPatchTargets(hwp, request);
}

}
