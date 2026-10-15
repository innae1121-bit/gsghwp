#include "ActionProtocol.h"

#include "ProtocolEncoding.h"

#include <WinCrypt.h>

#include <cerrno>
#include <cmath>
#include <cwctype>
#include <cwchar>
#include <limits>
#include <map>
#include <utility>

namespace hancom::actions {
namespace {

using hancom::encoding::EncodeUtf8Base64;

constexpr size_t kMaximumPayloadCharacters = 8'000'000;
constexpr size_t kMaximumCommands = 20'000;

std::vector<std::wstring> Split(const std::wstring& value, const wchar_t separator) {
    std::vector<std::wstring> fields;
    size_t start = 0;
    for (;;) {
        const size_t end = value.find(separator, start);
        fields.push_back(value.substr(start, end - start));
        if (end == std::wstring::npos) {
            return fields;
        }
        start = end + 1;
    }
}

bool DecodeUtf8Base64(const std::wstring& encoded, std::wstring* const decoded) {
    if (decoded == nullptr) {
        return false;
    }
    if (encoded.empty()) {
        decoded->clear();
        return true;
    }
    DWORD byteCount = 0;
    if (!CryptStringToBinaryW(
            encoded.c_str(),
            static_cast<DWORD>(encoded.size()),
            CRYPT_STRING_BASE64,
            nullptr,
            &byteCount,
            nullptr,
            nullptr)) {
        return false;
    }
    std::vector<BYTE> bytes(byteCount);
    if (byteCount != 0 && !CryptStringToBinaryW(
            encoded.c_str(),
            static_cast<DWORD>(encoded.size()),
            CRYPT_STRING_BASE64,
            bytes.data(),
            &byteCount,
            nullptr,
            nullptr)) {
        return false;
    }
    if (byteCount == 0) {
        decoded->clear();
        return true;
    }
    const int characterCount = MultiByteToWideChar(
        CP_UTF8,
        MB_ERR_INVALID_CHARS,
        reinterpret_cast<LPCCH>(bytes.data()),
        static_cast<int>(byteCount),
        nullptr,
        0);
    if (characterCount <= 0) {
        return false;
    }
    decoded->resize(static_cast<size_t>(characterCount));
    return MultiByteToWideChar(
               CP_UTF8,
               MB_ERR_INVALID_CHARS,
               reinterpret_cast<LPCCH>(bytes.data()),
               static_cast<int>(byteCount),
               decoded->data(),
               characterCount) == characterCount;
}


bool Fail(
    Error* const error,
    const wchar_t* const code,
    std::wstring location,
    std::wstring message) {
    if (error != nullptr) {
        error->code = code;
        error->location = std::move(location);
        error->message = std::move(message);
    }
    return false;
}

bool ParseLong(const std::wstring& value, LONG* const result, const bool allowNegative = false) {
    if (value.empty() || result == nullptr) {
        return false;
    }
    wchar_t* end = nullptr;
    errno = 0;
    const long long parsed = wcstoll(value.c_str(), &end, 10);
    const long long minimum = allowNegative ? (std::numeric_limits<LONG>::min)() : 0;
    if (errno != 0 || end == nullptr || *end != L'\0' || parsed < minimum ||
        parsed > (std::numeric_limits<LONG>::max)()) {
        return false;
    }
    *result = static_cast<LONG>(parsed);
    return true;
}

bool ParseBoolean(const std::wstring& value, bool* const result) {
    if (value == L"0") {
        *result = false;
        return true;
    }
    if (value == L"1") {
        *result = true;
        return true;
    }
    return false;
}

bool ParsePositiveMillimeter(const std::wstring& value, double* const result) {
    if (value.empty() || result == nullptr) {
        return false;
    }
    wchar_t* end = nullptr;
    errno = 0;
    const double parsed = wcstod(value.c_str(), &end);
    if (errno != 0 || end == nullptr || *end != L'\0' || !std::isfinite(parsed) ||
        parsed <= 0.0 || parsed > 1'000.0) {
        return false;
    }
    *result = parsed;
    return true;
}

// A crop fraction is the part of one source edge Hancom hides, so it is a
// ratio in [0, 1) and never a length. Keeping it unitless is deliberate: the
// unit SkipLeft/SkipTop/SkipRight/SkipBottom expect is the picture control's
// own OriginalSizeX/OriginalSizeY, which only the bridge can read at runtime.
bool ParseCropFraction(const std::wstring& value, double* const result) {
    if (value.empty() || result == nullptr) {
        return false;
    }
    wchar_t* end = nullptr;
    errno = 0;
    const double parsed = wcstod(value.c_str(), &end);
    if (errno != 0 || end == nullptr || *end != L'\0' || !std::isfinite(parsed) ||
        parsed < 0.0 || parsed >= 1.0) {
        return false;
    }
    *result = parsed;
    return true;
}

bool PlainName(const std::wstring& value) {
    if (value.empty() || value.size() > 128) {
        return false;
    }
    for (const wchar_t character : value) {
        if (!(iswalnum(character) || character == L'_' || character == L'.')) {
            return false;
        }
    }
    return true;
}

bool PropertyPath(const std::wstring& value) {
    if (value.empty() || value.size() > 512 || value.front() == L'/' || value.back() == L'/') {
        return false;
    }
    const std::vector<std::wstring> segments = Split(value, L'/');
    for (const std::wstring& segment : segments) {
        if (!PlainName(segment)) {
            return false;
        }
    }
    return true;
}

bool ParseValue(
    const std::vector<std::wstring>& fields,
    const size_t typeIndex,
    Value* const value) {
    if (value == nullptr || typeIndex + 1 >= fields.size()) {
        return false;
    }
    const std::wstring& type = fields[typeIndex];
    const std::wstring& encoded = fields[typeIndex + 1];
    if (type == L"I4") {
        value->kind = ValueKind::Integer;
        return ParseLong(encoded, &value->integer, true);
    }
    if (type == L"BOOL") {
        value->kind = ValueKind::Boolean;
        return ParseBoolean(encoded, &value->boolean);
    }
    if (type == L"BSTR") {
        value->kind = ValueKind::Text;
        return DecodeUtf8Base64(encoded, &value->text);
    }
    if (type == L"MM") {
        value->kind = ValueKind::Millimeter;
        wchar_t* end = nullptr;
        errno = 0;
        value->millimeter = wcstod(encoded.c_str(), &end);
        return errno == 0 && end != nullptr && *end == L'\0' &&
            std::isfinite(value->millimeter) && value->millimeter >= -1'000.0 &&
            value->millimeter <= 10'000.0;
    }
    if (type == L"ENUM" && typeIndex + 2 < fields.size() && PlainName(encoded)) {
        value->kind = ValueKind::Enumeration;
        value->converter = encoded;
        return DecodeUtf8Base64(fields[typeIndex + 2], &value->text);
    }
    return false;
}

bool ParseDocument(const std::vector<std::wstring>& fields, Request* const request) {
    return fields.size() == 3 && fields[0] == L"DOC" &&
        ParseLong(fields[1], &request->documentId) &&
        DecodeUtf8Base64(fields[2], &request->fullName);
}

}

bool ParseRequest(
    const std::wstring& payload,
    Request* const request,
    Error* const error) noexcept {
    try {
        if (request == nullptr || error == nullptr) {
            return false;
        }
        *request = Request{};
        *error = Error{};
        if (payload.empty() || payload.size() > kMaximumPayloadCharacters) {
            return Fail(error, L"BAD_REQUEST", L"", L"payload is empty or exceeds the 8MB limit");
        }
        std::vector<std::wstring> lines = Split(payload, L'\n');
        for (std::wstring& line : lines) {
            if (!line.empty() && line.back() == L'\r') {
                line.pop_back();
            }
        }
        if (lines.size() < 4 || lines.front() != L"HCA1" ||
            !ParseDocument(Split(lines[1], L'\t'), request)) {
            return Fail(error, L"BAD_REQUEST", L"", L"HCA1 header or DOC record is invalid");
        }
        size_t index = 2;
        if (index < lines.size()) {
            const std::vector<std::wstring> policy = Split(lines[index], L'\t');
            if (policy.size() == 3 && policy[0] == L"POLICY" && policy[1] == L"ATOMIC") {
                if (!ParseBoolean(policy[2], &request->atomic)) {
                    return Fail(error, L"BAD_REQUEST", L"POLICY", L"atomic policy is invalid");
                }
                ++index;
            }
        }
        if (index < lines.size()) {
            const std::vector<std::wstring> cursor = Split(lines[index], L'\t');
            if (cursor.size() == 4 && cursor[0] == L"EXPECT_CURSOR") {
                request->hasExpectedCursor =
                    ParseLong(cursor[1], &request->expectedList) &&
                    ParseLong(cursor[2], &request->expectedParagraph) &&
                    ParseLong(cursor[3], &request->expectedCharacter);
                if (!request->hasExpectedCursor) {
                    return Fail(error, L"BAD_REQUEST", L"EXPECT_CURSOR", L"cursor expectation is invalid");
                }
                ++index;
            }
        }
        if (index < lines.size()) {
            const std::vector<std::wstring> selection = Split(lines[index], L'\t');
            if (selection.size() == 8 && selection[0] == L"EXPECT_SELECTION") {
                request->hasExpectedSelection =
                    ParseBoolean(selection[1], &request->expectedSelected) &&
                    ParseLong(selection[2], &request->selectionStartList) &&
                    ParseLong(selection[3], &request->selectionStartParagraph) &&
                    ParseLong(selection[4], &request->selectionStartCharacter) &&
                    ParseLong(selection[5], &request->selectionEndList) &&
                    ParseLong(selection[6], &request->selectionEndParagraph) &&
                    ParseLong(selection[7], &request->selectionEndCharacter);
                if (!request->hasExpectedSelection) {
                    return Fail(error, L"BAD_REQUEST", L"EXPECT_SELECTION", L"selection expectation is invalid");
                }
                ++index;
            }
        }

        while (index < lines.size() && lines[index] != L"END") {
            const std::vector<std::wstring> fields = Split(lines[index], L'\t');
            if (fields.empty()) {
                return Fail(error, L"BAD_REQUEST", std::to_wstring(index + 1), L"empty command record");
            }
            Command command;
            if (fields[0] == L"RUN" && fields.size() == 2 && PlainName(fields[1])) {
                command.kind = CommandKind::Run;
                command.name = fields[1];
            } else if (fields[0] == L"MOVE_PAGE" && fields.size() == 2 &&
                ParseLong(fields[1], &command.page) && command.page > 0) {
                command.kind = CommandKind::MovePage;
            } else if (fields[0] == L"MOVE_POSITION" && fields.size() == 4 &&
                ParseLong(fields[1], &command.list) &&
                ParseLong(fields[2], &command.paragraph) &&
                ParseLong(fields[3], &command.character)) {
                command.kind = CommandKind::MovePosition;
            } else if (fields[0] == L"SELECT_CONTROL" && fields.size() == 2 &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::SelectControl;
            } else if (fields[0] == L"DELETE_CONTROL" && fields.size() == 2 &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::DeleteControl;
            } else if (fields[0] == L"COPY_CONTROL" && fields.size() == 2 &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::CopyControl;
            } else if (fields[0] == L"SAVE_DOCUMENT_FILE" && fields.size() == 2 &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::SaveDocumentFile;
            } else if (fields[0] == L"CAPTURE_DOCUMENT_BLOCK_PROBE" &&
                fields.size() == 2 &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::CaptureDocumentBlockProbe;
            } else if (fields[0] == L"RESTORE_DOCUMENT_FILE" && fields.size() == 3 &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty() &&
                ParseLong(fields[2], &command.page) && command.page > 0) {
                command.kind = CommandKind::RestoreDocumentFile;
            } else if (fields[0] == L"APPLY_COPIED_TABLE_ANCHOR" && fields.size() == 2 &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::ApplyCopiedTableAnchor;
            } else if (fields[0] == L"PASTE_TABLE" && fields.size() == 1) {
                command.kind = CommandKind::PasteTable;
            } else if (fields[0] == L"CAPTURE_TABLE" && fields.size() == 1) {
                command.kind = CommandKind::CaptureTable;
            } else if (fields[0] == L"MOVE_DOC_END" && fields.size() == 1) {
                command.kind = CommandKind::MoveDocumentEnd;
            } else if (fields[0] == L"DELETE_TAIL" && fields.size() == 5 &&
                ParseLong(fields[1], &command.list) &&
                ParseLong(fields[2], &command.paragraph) &&
                ParseLong(fields[3], &command.character) &&
                DecodeUtf8Base64(fields[4], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::DeleteTail;
            } else if (fields[0] == L"INSERT_TEXT" && fields.size() == 2 &&
                DecodeUtf8Base64(fields[1], &command.first)) {
                command.kind = CommandKind::InsertText;
            } else if (fields[0] == L"REPLACE_SELECTION" && fields.size() == 3 &&
                DecodeUtf8Base64(fields[1], &command.first) &&
                DecodeUtf8Base64(fields[2], &command.second)) {
                command.kind = CommandKind::ReplaceSelection;
            } else if (fields[0] == L"PREPARE_TEXT" &&
                fields.size() == 9 && fields[1] == L"LITERAL_RANGE" &&
                ParseLong(fields[2], &command.list) && command.list >= 0 &&
                ParseLong(fields[3], &command.paragraph) && command.paragraph >= 0 &&
                ParseLong(fields[4], &command.character) && command.character >= 0 &&
                ParseLong(fields[5], &command.endCharacter) &&
                DecodeUtf8Base64(fields[6], &command.paragraphText) &&
                DecodeUtf8Base64(fields[7], &command.first) && !command.first.empty() &&
                DecodeUtf8Base64(fields[8], &command.second) &&
                command.endCharacter > command.character &&
                static_cast<size_t>(command.endCharacter) <= command.paragraphText.size() &&
                command.paragraphText.find_first_of(L"\r\n") == std::wstring::npos &&
                command.paragraphText.substr(command.character,
                    command.endCharacter - command.character) == command.first) {
                command.kind = CommandKind::TextPatch;
                command.name = L"RANGE";
                command.endList = command.list;
                command.endParagraph = command.paragraph;
                command.preflightOnly = true;
                command.hasExpectedText = true;
                command.matchCase = true;
                command.literalRange = true;
            } else if (fields[0] == L"PREPARE_TEXT" &&
                fields.size() == 9 &&
                fields[1] == L"RANGE" &&
                ParseLong(fields[2], &command.list) &&
                ParseLong(fields[3], &command.paragraph) &&
                ParseLong(fields[4], &command.character) &&
                ParseLong(fields[5], &command.endList) &&
                ParseLong(fields[6], &command.endParagraph) &&
                ParseLong(fields[7], &command.endCharacter) &&
                DecodeUtf8Base64(fields[8], &command.second)) {
                command.kind = CommandKind::TextPatch;
                command.name = fields[1];
                command.preflightOnly = true;
            } else if (fields[0] == L"PREPARE_TEXT" &&
                fields.size() == 7 &&
                fields[1] == L"CELL" &&
                DecodeUtf8Base64(fields[2], &command.tableInstanceId) &&
                !command.tableInstanceId.empty() && !fields[3].empty() &&
                ParseLong(fields[4], &command.occurrence) &&
                ParseBoolean(fields[5], &command.matchCase) &&
                DecodeUtf8Base64(fields[6], &command.second)) {
                command.kind = CommandKind::TextPatch;
                command.name = fields[1];
                command.cellAddress = fields[3];
                command.preflightOnly = true;
            } else if (fields[0] == L"PATCH_TEXT" &&
                (fields.size() == 5 || fields.size() == 6) &&
                fields[1] == L"CURRENT" &&
                ParseBoolean(fields[2], &command.hasExpectedText) &&
                DecodeUtf8Base64(fields[3], &command.first) &&
                DecodeUtf8Base64(fields[4], &command.second) &&
                (command.hasExpectedText || command.first.empty()) &&
                (fields.size() == 5 ||
                 ParseBoolean(fields[5], &command.preserveFormat))) {
                command.kind = CommandKind::TextPatch;
                command.name = fields[1];
            } else if (fields[0] == L"PATCH_TEXT" &&
                (fields.size() == 10 || fields.size() == 11) &&
                fields[1] == L"RANGE" &&
                ParseLong(fields[2], &command.list) &&
                ParseLong(fields[3], &command.paragraph) &&
                ParseLong(fields[4], &command.character) &&
                ParseLong(fields[5], &command.endList) &&
                ParseLong(fields[6], &command.endParagraph) &&
                ParseLong(fields[7], &command.endCharacter) &&
                DecodeUtf8Base64(fields[8], &command.first) && !command.first.empty() &&
                DecodeUtf8Base64(fields[9], &command.second) &&
                (fields.size() == 10 ||
                 ParseBoolean(fields[10], &command.preserveFormat))) {
                command.kind = CommandKind::TextPatch;
                command.name = fields[1];
                command.hasExpectedText = true;
            } else if (fields[0] == L"PATCH_TEXT" &&
                (fields.size() == 6 || fields.size() == 7) &&
                fields[1] == L"FIND" &&
                ParseLong(fields[2], &command.occurrence) &&
                ParseBoolean(fields[3], &command.matchCase) &&
                DecodeUtf8Base64(fields[4], &command.first) && !command.first.empty() &&
                DecodeUtf8Base64(fields[5], &command.second) &&
                (fields.size() == 6 ||
                 ParseBoolean(fields[6], &command.preserveFormat))) {
                command.kind = CommandKind::TextPatch;
                command.name = fields[1];
                command.hasExpectedText = true;
            } else if (fields[0] == L"PATCH_TEXT" &&
                (fields.size() == 8 || fields.size() == 9) &&
                fields[1] == L"FIND" &&
                DecodeUtf8Base64(fields[2], &command.tableInstanceId) &&
                !command.tableInstanceId.empty() && !fields[3].empty() &&
                ParseLong(fields[4], &command.occurrence) &&
                ParseBoolean(fields[5], &command.matchCase) &&
                DecodeUtf8Base64(fields[6], &command.first) && !command.first.empty() &&
                DecodeUtf8Base64(fields[7], &command.second) &&
                (fields.size() == 8 ||
                 ParseBoolean(fields[8], &command.preserveFormat))) {
                command.kind = CommandKind::TextPatch;
                command.name = fields[1];
                command.cellAddress = fields[3];
                command.hasExpectedText = true;
            } else if (fields[0] == L"PATCH_TEXT" &&
                (fields.size() == 8 || fields.size() == 9) &&
                fields[1] == L"CELL" &&
                DecodeUtf8Base64(fields[2], &command.tableInstanceId) &&
                !command.tableInstanceId.empty() && !fields[3].empty() &&
                ParseLong(fields[4], &command.occurrence) &&
                ParseBoolean(fields[5], &command.matchCase) &&
                DecodeUtf8Base64(fields[6], &command.first) &&
                DecodeUtf8Base64(fields[7], &command.second) &&
                (fields.size() == 8 ||
                 ParseBoolean(fields[8], &command.preserveFormat))) {
                command.kind = CommandKind::TextPatch;
                command.name = fields[1];
                command.cellAddress = fields[3];
                command.hasExpectedText = true;
            } else if (fields[0] == L"INSERT_PICTURE" &&
                (fields.size() == 2 || fields.size() == 4 || fields.size() == 8) &&
                DecodeUtf8Base64(fields[1], &command.first) && !command.first.empty()) {
                if (fields.size() >= 4 &&
                    (!ParsePositiveMillimeter(fields[2], &command.pictureWidthMm) ||
                     !ParsePositiveMillimeter(fields[3], &command.pictureHeightMm))) {
                    return Fail(error, L"BAD_REQUEST", L"INSERT_PICTURE", L"picture box is invalid");
                }
                if (fields.size() == 8 &&
                    (!ParseCropFraction(fields[4], &command.pictureCrop.left) ||
                     !ParseCropFraction(fields[5], &command.pictureCrop.top) ||
                     !ParseCropFraction(fields[6], &command.pictureCrop.right) ||
                     !ParseCropFraction(fields[7], &command.pictureCrop.bottom) ||
                     command.pictureCrop.left + command.pictureCrop.right >= 1.0 ||
                     command.pictureCrop.top + command.pictureCrop.bottom >= 1.0)) {
                    return Fail(error, L"BAD_REQUEST", L"INSERT_PICTURE", L"picture crop is invalid");
                }
                command.kind = CommandKind::InsertPicture;
                command.hasPictureBox = fields.size() >= 4;
                command.hasPictureCrop = fields.size() == 8;
            } else if (fields[0] == L"CELL" && fields.size() == 2 && !fields[1].empty()) {
                command.kind = CommandKind::Cell;
                command.first = fields[1];
            } else if (fields[0] == L"SET_CELL_TEXT" && fields.size() == 5 &&
                !fields[1].empty() &&
                DecodeUtf8Base64(fields[2], &command.expectedText) &&
                DecodeUtf8Base64(fields[3], &command.second) &&
                ParseBoolean(fields[4], &command.preserveFormat)) {
                command.kind = CommandKind::SetCellText;
                command.first = fields[1];
                command.hasExpectedText = true;
            } else if (fields[0] == L"SET_CELL_TEXT" && fields.size() == 3 &&
                !fields[1].empty() && DecodeUtf8Base64(fields[2], &command.second)) {
                command.kind = CommandKind::SetCellText;
                command.first = fields[1];
            } else if (fields[0] == L"MERGE" && fields.size() == 3 &&
                !fields[1].empty() && !fields[2].empty()) {
                command.kind = CommandKind::Merge;
                command.first = fields[1];
                command.second = fields[2];
            } else if (fields[0] == L"CAPTION" &&
                (fields.size() == 3 || fields.size() == 6 ||
                 fields.size() == 14 || fields.size() == 17) &&
                ParseLong(fields[1], &command.styleId) &&
                DecodeUtf8Base64(fields[2], &command.first) && !command.first.empty()) {
                command.kind = CommandKind::Caption;
                const bool hasDirectFormat = fields.size() == 14 || fields.size() == 17;
                const bool hasFormatSource = fields.size() == 6 || fields.size() == 17;
                if (hasDirectFormat) {
                    if (!DecodeUtf8Base64(fields[3], &command.captionFaceName) ||
                        command.captionFaceName.empty() ||
                        !ParseLong(fields[4], &command.captionHeight) ||
                        command.captionHeight <= 0 ||
                        !ParseBoolean(fields[5], &command.captionBold) ||
                        !ParseLong(fields[6], &command.captionTextColor) ||
                        !ParseLong(fields[7], &command.captionAlignment) ||
                        command.captionAlignment > 3 ||
                        !ParseLong(fields[8], &command.captionLineSpacing) ||
                        command.captionLineSpacing <= 0 ||
                        !ParseLong(fields[9], &command.captionLeftMargin, true) ||
                        !ParseLong(fields[10], &command.captionRightMargin, true) ||
                        !ParseLong(fields[11], &command.captionIndentation, true) ||
                        !ParseLong(fields[12], &command.captionPreviousSpacing, true) ||
                        !ParseLong(fields[13], &command.captionNextSpacing, true)) {
                        return Fail(
                            error,
                            L"BAD_REQUEST",
                            L"CAPTION",
                            L"caption direct format is invalid");
                    }
                    command.hasCaptionFormat = true;
                }
                if (hasFormatSource) {
                    const size_t source = hasDirectFormat ? 14 : 3;
                    if (!ParseLong(fields[source], &command.captionFormatSourceList) ||
                        !ParseLong(
                            fields[source + 1],
                            &command.captionFormatSourceParagraph) ||
                        !ParseLong(
                            fields[source + 2],
                            &command.captionFormatSourceCharacter)) {
                        return Fail(
                            error,
                            L"BAD_REQUEST",
                            L"CAPTION",
                            L"caption format source is invalid");
                    }
                    command.hasCaptionFormatSource = true;
                }
            } else if (fields[0] == L"LEAVE_TABLE" && fields.size() == 1) {
                command.kind = CommandKind::LeaveTable;
            } else if (fields[0] == L"ACTION" && fields.size() == 3 &&
                PlainName(fields[1]) && PlainName(fields[2])) {
                command.kind = CommandKind::Action;
                command.name = fields[1];
                command.parameterSet = fields[2];
                ++index;
                std::map<std::wstring, LONG> arrays;
                while (index < lines.size() && lines[index] != L"ENDACTION") {
                    const std::vector<std::wstring> item = Split(lines[index], L'\t');
                    if (item.size() >= 4 && item[0] == L"SET" && PropertyPath(item[1])) {
                        Setter setter;
                        setter.path = item[1];
                        if (!ParseValue(item, 2, &setter.value) ||
                            (setter.value.kind == ValueKind::Enumeration && item.size() != 5) ||
                            (setter.value.kind != ValueKind::Enumeration && item.size() != 4)) {
                            return Fail(error, L"BAD_REQUEST", item[1], L"ACTION SET value is invalid");
                        }
                        command.setters.push_back(std::move(setter));
                    } else if (item.size() == 3 && item[0] == L"ARRAY" && PlainName(item[1])) {
                        LONG count = 0;
                        if (!ParseLong(item[2], &count) || count < 1 || count > 10'000 ||
                            !arrays.emplace(item[1], count).second) {
                            return Fail(error, L"BAD_REQUEST", item[1], L"ACTION ARRAY is invalid");
                        }
                        command.arrays.emplace_back(item[1], count);
                    } else if (item.size() >= 5 && item[0] == L"ASET" && PlainName(item[1])) {
                        ArrayValue value;
                        value.name = item[1];
                        const auto array = arrays.find(value.name);
                        if (array == arrays.end() ||
                            !ParseLong(item[2], &value.index) ||
                            value.index < 0 || value.index >= array->second ||
                            !ParseValue(item, 3, &value.value) ||
                            (value.value.kind == ValueKind::Enumeration && item.size() != 6) ||
                            (value.value.kind != ValueKind::Enumeration && item.size() != 5)) {
                            return Fail(error, L"BAD_REQUEST", item[1], L"ACTION ASET is invalid");
                        }
                        command.arrayValues.push_back(std::move(value));
                    } else {
                        return Fail(error, L"BAD_REQUEST", std::to_wstring(index + 1), L"ACTION item is invalid");
                    }
                    ++index;
                }
                if (index >= lines.size()) {
                    return Fail(error, L"BAD_REQUEST", command.name, L"ACTION has no ENDACTION");
                }
            } else if (fields[0] == L"CALL" && fields.size() == 2 && PlainName(fields[1])) {
                command.kind = CommandKind::Call;
                command.name = fields[1];
                ++index;
                while (index < lines.size() && lines[index] != L"ENDCALL") {
                    const std::vector<std::wstring> item = Split(lines[index], L'\t');
                    Value value;
                    if (item.size() < 3 || item[0] != L"ARG" || !ParseValue(item, 1, &value) ||
                        (value.kind == ValueKind::Enumeration && item.size() != 4) ||
                        (value.kind != ValueKind::Enumeration && item.size() != 3)) {
                        return Fail(error, L"BAD_REQUEST", command.name, L"CALL argument is invalid");
                    }
                    command.arguments.push_back(std::move(value));
                    ++index;
                }
                if (index >= lines.size()) {
                    return Fail(error, L"BAD_REQUEST", command.name, L"CALL has no ENDCALL");
                }
            } else {
                return Fail(error, L"BAD_REQUEST", std::to_wstring(index + 1), L"command record is invalid");
            }
            request->commands.push_back(std::move(command));
            if (request->commands.size() > kMaximumCommands) {
                return Fail(error, L"BAD_REQUEST", L"", L"script exceeds the 20000-command limit");
            }
            ++index;
        }
        if (request->commands.empty() || index != lines.size() - 1 || lines[index] != L"END") {
            return Fail(error, L"BAD_REQUEST", L"", L"script has no commands or malformed END");
        }
        return true;
    } catch (...) {
        return Fail(error, L"BAD_REQUEST", L"", L"action request parsing failed unexpectedly");
    }
}

std::wstring ErrorResponse(const Error& error) {
    return L"HCA1\tERROR\t" + error.code + L'\t' + EncodeUtf8Base64(error.location) +
        L'\t' + EncodeUtf8Base64(error.message);
}

}
