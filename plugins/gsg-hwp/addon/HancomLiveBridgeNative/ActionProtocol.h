#pragma once

#include <Windows.h>

#include <string>
#include <vector>

namespace hancom::actions {

enum class ValueKind { Integer, Boolean, Text, Millimeter, Enumeration };

struct Value {
    ValueKind kind = ValueKind::Integer;
    LONG integer = 0;
    bool boolean = false;
    double millimeter = 0.0;
    std::wstring text;
    std::wstring converter;
};

struct Setter {
    std::wstring path;
    Value value;
};

struct ArrayValue {
    std::wstring name;
    LONG index = 0;
    Value value;
};

// Fractions of the source image hidden by Hancom's own picture crop
// (DrawImageAttr SkipLeft/SkipTop/SkipRight/SkipBottom). Each value is in
// [0, 1) and opposite edges add up to less than 1. The image file on disk is
// only read, never rewritten.
struct PictureCrop {
    double left = 0.0;
    double top = 0.0;
    double right = 0.0;
    double bottom = 0.0;
};

enum class CommandKind {
    Run,
    Action,
    Call,
    MovePage,
    MovePosition,
    SelectControl,
    DeleteControl,
    CopyControl,
    SaveDocumentFile,
    RestoreDocumentFile,
    CaptureDocumentBlockProbe,
    ApplyCopiedTableAnchor,
    PasteTable,
    CaptureTable,
    MoveDocumentEnd,
    DeleteTail,
    InsertText,
    ReplaceSelection,
    TextPatch,
    InsertPicture,
    Cell,
    SetCellText,
    Merge,
    Caption,
    LeaveTable,
};

struct Command {
    CommandKind kind = CommandKind::Run;
    std::wstring name;
    std::wstring parameterSet;
    std::vector<Setter> setters;
    std::vector<std::pair<std::wstring, LONG>> arrays;
    std::vector<ArrayValue> arrayValues;
    std::vector<Value> arguments;
    LONG list = 0;
    LONG paragraph = 0;
    LONG character = 0;
    LONG endList = 0;
    LONG endParagraph = 0;
    LONG endCharacter = 0;
    LONG occurrence = 0;
    LONG page = 0;
    LONG styleId = -1;
    bool hasCaptionFormat = false;
    std::wstring captionFaceName;
    LONG captionHeight = 0;
    bool captionBold = false;
    LONG captionTextColor = 0;
    LONG captionAlignment = 0;
    LONG captionLineSpacing = 0;
    LONG captionLeftMargin = 0;
    LONG captionRightMargin = 0;
    LONG captionIndentation = 0;
    LONG captionPreviousSpacing = 0;
    LONG captionNextSpacing = 0;
    bool hasCaptionFormatSource = false;
    LONG captionFormatSourceList = 0;
    LONG captionFormatSourceParagraph = 0;
    LONG captionFormatSourceCharacter = 0;
    bool hasPictureBox = false;
    bool hasPictureCrop = false;
    bool hasExpectedText = false;
    bool matchCase = false;
    bool preserveFormat = false;
    bool preflightOnly = false;
    bool literalRange = false;
    std::wstring paragraphText;
    double pictureWidthMm = 0.0;
    double pictureHeightMm = 0.0;
    PictureCrop pictureCrop;
    std::wstring first;
    std::wstring second;
    std::wstring expectedText;
    std::wstring tableInstanceId;
    std::wstring cellAddress;
};

struct Request {
    LONG documentId = -1;
    std::wstring fullName;
    bool atomic = false;
    bool hasExpectedCursor = false;
    LONG expectedList = 0;
    LONG expectedParagraph = 0;
    LONG expectedCharacter = 0;
    bool hasExpectedSelection = false;
    bool expectedSelected = false;
    LONG selectionStartList = 0;
    LONG selectionStartParagraph = 0;
    LONG selectionStartCharacter = 0;
    LONG selectionEndList = 0;
    LONG selectionEndParagraph = 0;
    LONG selectionEndCharacter = 0;
    // Supplied out-of-band by ExecuteActionsChecked. It is deliberately not
    // part of HCA1, so existing action payloads and public schemas stay stable.
    std::wstring expectedContentSignature;
    bool requiresContentAuthorization = false;
    std::vector<Command> commands;
};

struct Error {
    std::wstring code;
    std::wstring location;
    std::wstring message;
};

bool ParseRequest(const std::wstring& payload, Request* request, Error* error) noexcept;
std::wstring ErrorResponse(const Error& error);

}
