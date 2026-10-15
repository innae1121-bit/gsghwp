#pragma once

#include "ActionExecutor.h"
#include "ComError.h"
#include "ComState.h"
#include "DispatchInvoke.h"
#include "ParagraphFormatting.h"
#include "ReferenceLayoutSpec.h"
#include "TableInspection.h"

#include <atlbase.h>
#include <atlcomcli.h>

#include <string>
#include <vector>

namespace hancom::actions::detail {

using hancom::com::FormatHResult;
using hancom::com_state::Position;
using hancom::com_state::SamePosition;
using hancom::com_state::SameSelection;
using hancom::com_state::Selection;
using hancom::com_state::SelectionCapturePolicy;
using hancom::com_state::kSelectionModeMask;
using hancom::com_state::kSelectionNone;
using hancom::com_state::kSelectionText;
using hancom::dispatch::AsBool;
using hancom::dispatch::AsDispatch;
using hancom::dispatch::AsLong;
using hancom::dispatch::AsString;
using hancom::dispatch::Method;
using hancom::dispatch::PropertyGet;
using hancom::dispatch::PropertyPut;

struct Context {
    CComPtr<IDispatch> hwp;
    CComPtr<IDispatch> action;
    CComPtr<IDispatch> table;
    std::wstring tableId;
    std::wstring currentCell;
    hancom::inspection::CellTopology topology;
    bool reuseTextPatchTopology = false;
    hancom::formatting::ParagraphFormat copiedTableAnchorFormat;
    bool hasCopiedTableAnchorFormat = false;
    std::wstring copiedTableBlock;
    bool hasCopiedTableBlock = false;
    const Request* request = nullptr;
    ExecutionResult* result = nullptr;
    // Where an append actually starts: the caret right after MoveDocEnd and
    // before the paragraph break that MOVE_DOC_END may add. An atomic rollback
    // deletes from here, so the break is inside what it removes.
    Position appendAnchor;
    bool hasAppendAnchor = false;
};

struct TextFormatFingerprint {
    std::wstring faceName;
    LONG height = 0;
    bool bold = false;
    LONG textColor = 0;
    LONG widthRatio = 0;
    LONG letterSpacing = 0;
    LONG alignment = 0;
    LONG lineSpacingType = 0;
    LONG lineSpacing = 0;
    LONG leftMargin = 0;
    LONG rightMargin = 0;
    LONG indentation = 0;
    LONG previousSpacing = 0;
    LONG nextSpacing = 0;
};

struct PreservedTextFormat {
    CComPtr<IDispatch> characterSet;
    CComPtr<IDispatch> paragraphSet;
    TextFormatFingerprint fingerprint;
};

bool SetError(
    ExecutionResult* result,
    std::wstring code,
    std::wstring location,
    std::wstring message);
CComVariant BooleanVariant(bool value);
bool GetDispatchProperty(
    IDispatch* object,
    const wchar_t* name,
    CComPtr<IDispatch>& value,
    ExecutionResult* result,
    const std::wstring& location = L"");
bool CallBooleanMethod(
    IDispatch* object,
    const wchar_t* name,
    const std::vector<CComVariant>& arguments,
    bool* returned,
    ExecutionResult* result,
    const std::wstring& location = L"");
bool RunAction(
    IDispatch* action,
    const std::wstring& name,
    ExecutionResult* result,
    const std::wstring& location = L"");
bool RunVerifiedNavigationAction(
    IDispatch* action,
    const std::wstring& name,
    ExecutionResult* result,
    const std::wstring& location = L"");
bool GetPosition(IDispatch* hwp, Position* position, ExecutionResult* result);
bool SetPosition(
    IDispatch* hwp,
    const Position& position,
    ExecutionResult* result,
    const std::wstring& location = L"");
bool CreateSet(
    IDispatch* hwp,
    const wchar_t* name,
    CComPtr<IDispatch>& set,
    ExecutionResult* result);
bool ItemLong(
    IDispatch* set,
    const wchar_t* name,
    LONG* value,
    ExecutionResult* result);
bool GetSelection(
    IDispatch* hwp,
    Selection* selection,
    ExecutionResult* result,
    SelectionCapturePolicy policy = SelectionCapturePolicy::Basic);
bool SelectTextRange(
    Context* context,
    const Position& start,
    const Position& end,
    const std::wstring& location);
bool RestoreTextPosition(
    Context* context,
    const Selection& original,
    const std::wstring& location);

bool ValidateDocumentIdentity(
    IDispatch* hwp,
    const Request& request,
    ExecutionResult* result);
bool ReadSelectedText(
    Context* context,
    std::wstring* text,
    const std::wstring& location);
bool ConvertValue(
    IDispatch* hwp,
    const Value& value,
    CComVariant* converted,
    ExecutionResult* result,
    const std::wstring& location);
bool PutItemOrProperty(
    IDispatch* object,
    const std::wstring& name,
    const CComVariant& value,
    ExecutionResult* result,
    const std::wstring& location);
bool ApplySetter(
    IDispatch* hwp,
    IDispatch* root,
    const Setter& setter,
    ExecutionResult* result);
bool ExecuteParameterAction(
    Context* context,
    const Command& command,
    bool verifyFormat = true);
bool MoveToPage(Context* context, LONG requestedPage);

bool SelectControl(
    Context* context,
    const std::wstring& instanceId,
    CComPtr<IDispatch>& selectedControl);
bool SelectControl(Context* context, const std::wstring& instanceId);
bool CaptureCurrentTable(Context* context);
bool DeleteControl(Context* context, const std::wstring& instanceId);
bool CopyControl(Context* context, const std::wstring& instanceId);
bool ApplyCopiedTableAnchor(Context* context, const std::wstring& instanceId);
bool PasteTable(Context* context);
bool SetTableTreatAsCharacter(Context* context, bool treatAsCharacter = true);
std::wstring GetCellAddress(IDispatch* hwp);
bool BuildCellTopology(Context* context);
std::wstring NormalizeAddress(std::wstring address);
bool GoToCell(Context* context, const std::wstring& requested);
bool MergeCellsUsingTopology(
    Context* context,
    const std::wstring& first,
    const std::wstring& second,
    bool clearTopology);
bool MergeCells(
    Context* context,
    const std::wstring& first,
    const std::wstring& second);
bool InsertText(
    Context* context,
    const std::wstring& text,
    const std::wstring& location);

bool CaptureTextFormat(
    Context* context,
    PreservedTextFormat* preserved,
    const std::wstring& location);
bool MatchesExpectedReferenceTextFormat(
    const TextFormatFingerprint& actual,
    const hancom::reference_layout::Style& expected) noexcept;
bool SetCellText(Context* context, const Command& command);
bool ReplaceSelection(Context* context, const Command& command);
bool PatchText(Context* context, const Command& command);
bool PreflightTableTextCommands(Context* context, const Request& request);

bool InsertPicture(Context* context, const Command& command);
bool AttachCaption(Context* context, const Command& command);
bool AnchorPosition(
    IDispatch* control,
    Position* position,
    ExecutionResult* result);
bool LeaveTable(Context* context, bool appendParagraph = true);

bool ReadTail(Context* context, const Position& start, std::wstring* selected);
bool DeleteTail(Context* context, const Command& command);
bool RollbackAppendTail(Context* context, const Position& start);
bool ExecuteCall(Context* context, const Command& command);
bool SaveDocumentFile(Context* context, const std::wstring& pathText);
bool ProbeDocumentBlock(Context* context, const std::wstring& pathText);
bool RestoreDocumentFile(
    Context* context,
    const std::wstring& pathText,
    LONG expectedPageCount);

bool ExecuteReferenceLayout(Context* context, const Command& command);

bool ExecuteCommand(Context* context, const Command& command);
bool ValidateCommandOrder(const Request& request, ExecutionResult* result);
std::wstring StructureDigest(IDispatch* hwp);
std::wstring CommandStep(const Command& command);
bool CommandMayMutate(const Command& command);

}
