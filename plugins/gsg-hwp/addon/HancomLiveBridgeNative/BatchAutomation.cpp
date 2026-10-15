#include "BatchAutomation.h"

#include "ActionExecutor.h"
#include "ActionProtocol.h"
#include "BatchExecutor.h"
#include "BatchProtocol.h"
#include "DispatchInvoke.h"
#include "DocumentLifecycle.h"
#include "DocumentGraphProtocol.h"
#include "ForegroundGuard.h"
#include "LiveInspection.h"
#include "OfficialApiProbe.h"
#include "OfficialApiState.h"
#include "ProtocolBundle.h"
#include "ProtocolEncoding.h"

#include <chrono>
#include <cstdint>
#include <cwchar>
#include <new>
#include <sstream>
#include <string>

namespace {

constexpr DISPID kProtocolVersion = 1;
constexpr DISPID kPing = 2;
constexpr DISPID kExecute = 3;
constexpr DISPID kSnapshot = 4;
constexpr DISPID kInspectPage = 5;
constexpr DISPID kExecuteActions = 6;
constexpr DISPID kInspectPageV3 = 7;
constexpr DISPID kInspectPageSummary = 8;
constexpr DISPID kProbeOfficialApi = 9;
constexpr DISPID kInspectStructure = 10;
constexpr DISPID kInspectRoutingContext = 11;
constexpr DISPID kSaveReopenVerify = 12;
constexpr DISPID kInspectPagesV3 = 13;
constexpr DISPID kSaveVerify = 14;
constexpr DISPID kTargetDocumentId = 15;
constexpr DISPID kActivateDocument = 16;
constexpr DISPID kActivationStatus = 17;
// Additive member. ProtocolVersion stays 12 on purpose: an older bridge simply
// fails GetIDsOfNames for this name, which the Python caller already treats as
// "feature absent" and falls back from, so mixed builds keep working.
constexpr DISPID kInspectParagraphStyles = 18;
// Same additive rule as above. Three paths drive Hangul COM straight from
// Python and never reach Invoke: rendering a page, opening a document, and
// switching the active tab when native activation is unavailable or fails.
// They open and close the process wide foreground bracket with these two
// commands. Python only says where the region starts and ends; whether to arm,
// what to refuse, how to treat a modal, and whether to hand anything back are
// all decided in ForegroundGuard.h.
constexpr DISPID kBeginForegroundGuard = 19;
constexpr DISPID kEndForegroundGuard = 20;
constexpr DISPID kContentSignature = 21;
// Protocol-13 members. Complete content signatures now include the normalized
// HWPML document hash and length, so older bridges must fail closed.
constexpr DISPID kExecuteHistory = 22;
constexpr DISPID kExecuteActionsChecked = 23;
constexpr DISPID kExecuteProtocolBundle = 24;
// HGN1 is an independent binary ABI beside the frozen protocol-14 surface.
constexpr DISPID kGraphProtocolVersion = 25;
constexpr DISPID kGraphCapabilities = 26;
constexpr DISPID kGraphOpen = 27;
constexpr DISPID kGraphNext = 28;
constexpr DISPID kGraphCancel = 29;
constexpr DISPID kGraphClose = 30;
constexpr DISPID kPatchBegin = 31;
constexpr DISPID kPatchChunk = 32;
constexpr DISPID kPatchCommit = 33;
constexpr DISPID kPatchAbort = 34;
constexpr DISPID kPatchValidate = 35;
constexpr DISPID kPatchApply = 36;
constexpr DISPID kBlobRead = 37;
// Additive member, same rule as kInspectParagraphStyles above: ProtocolVersion
// stays 14, an older bridge simply fails GetIDsOfNames for the name, and the
// Python caller already reads that as "feature absent".
//
// It answers the freshness question ContentSignature was being asked for, at a
// cost that does not depend on what is embedded in the document. Measured on
// the same 34-page 82MB document: ContentSignature 6236ms, of which
// GetTextFile("HWPML2X") alone is 6020ms and 111,079,555 characters. This
// member never touches that call. ContentSignature is untouched and stays the
// answer wherever documents are compared rather than merely dated.
constexpr DISPID kContentRevision = 38;
// Additive read-only prepared text-patch receipt. ProtocolVersion remains 14;
// older bridges expose no member and Python retains checkpoint-first fallback.
constexpr DISPID kPrepareTextPatches = 39;
constexpr DISPID kExecutePreparedTextPatches = 40;
// Capability-gated, read-only literal offsets. Older bridges must not treat
// these offsets as native cursor coordinates or fall back to mutation.
constexpr DISPID kPrepareLiteralTextPatches = 41;
constexpr LONG kActivationIdle = 0;
constexpr LONG kActivationPending = 1;
constexpr LONG kActivationSucceeded = 2;
constexpr LONG kActivationFailed = 3;
volatile LONG gActivationSequence = 0;
// How many writes have entered this Invoke, in this bridge process.
//
// The cheap revision token reports it, and that is the only thing it is for: a
// write can change a document without changing its page count, its control
// chain or a single character of its text -- turning a run bold does exactly
// that -- and no observation cheap enough to belong in that token would notice.
//
// Read the first sentence literally. This counts writes that arrive as a member
// of this dispatch interface, which is not the same as every write the product
// performs. The Python side also drives Hangul's own automation directly, and
// `HAction.Execute` / `HAction.Run` from there (hwp_live_table_format.cpp has no
// counterpart -- see hwp_live_table_format.py:52, hwp_page_setup.py:123,
// hwp_live_caption_edit.py:21) never reaches this function, so this counter
// cannot see those at all. Widening the counter to cover them is a separate
// piece of work; until it is done, a caller that needs to notice a formatting
// change made through those paths needs a content signature, not this token.
//
// It is bumped on both sides of a write, never once. A token read that races a
// write in flight must not be able to observe a value the finished write leaves
// unchanged, or whatever was cached against that value would outlive the
// document it described.
//
// Process-wide rather than per-document on purpose: writing to one document
// then invalidates a sibling document's cached derivation too, which costs one
// recomputation and cannot be wrong. A per-document counter would be sharper
// and would have to be right about document identity to stay safe. Per process,
// equally: a second worker driving the same Hangul moves this counter, but a
// worker that never calls in does not, and no counter here can say otherwise.
volatile LONG gWriteEpoch = 0;

bool MemberWritesDocument(const DISPID memberId) noexcept;

// Bumps the write epoch around a member that can change the document.
//
// The DISPATCH_METHOD condition is not decoration. A caller that merely reaches
// for `batch.ExecuteActions` makes the scripting host probe the same DISPID as
// a property first, and every write member refuses that probe -- so counting it
// would report writes that were only ever looked up. No write member can run
// without DISPATCH_METHOD, so the condition cannot hide one either.
class WriteEpochBracket {
public:
    WriteEpochBracket(const DISPID memberId, const WORD flags) noexcept
        : writes_((flags & DISPATCH_METHOD) != 0 &&
              MemberWritesDocument(memberId)) {
        if (writes_) {
            static_cast<void>(InterlockedIncrement(&gWriteEpoch));
        }
    }

    ~WriteEpochBracket() noexcept {
        if (writes_) {
            static_cast<void>(InterlockedIncrement(&gWriteEpoch));
        }
    }

    WriteEpochBracket(const WriteEpochBracket&) = delete;
    WriteEpochBracket& operator=(const WriteEpochBracket&) = delete;

private:
    bool writes_ = false;
};

std::uint64_t CurrentWriteEpoch() noexcept {
    return static_cast<std::uint64_t>(
        static_cast<ULONG>(InterlockedCompareExchange(&gWriteEpoch, 0, 0)));
}

DISPID MemberId(const wchar_t* const name) noexcept {
    if (_wcsicmp(name, L"ProtocolVersion") == 0) {
        return kProtocolVersion;
    }
    if (_wcsicmp(name, L"Ping") == 0) {
        return kPing;
    }
    if (_wcsicmp(name, L"Execute") == 0) {
        return kExecute;
    }
    if (_wcsicmp(name, L"Snapshot") == 0) {
        return kSnapshot;
    }
    if (_wcsicmp(name, L"InspectPage") == 0) {
        return kInspectPage;
    }
    if (_wcsicmp(name, L"ExecuteActions") == 0) {
        return kExecuteActions;
    }
    if (_wcsicmp(name, L"InspectPageV3") == 0) {
        return kInspectPageV3;
    }
    if (_wcsicmp(name, L"InspectPageSummary") == 0) {
        return kInspectPageSummary;
    }
    if (_wcsicmp(name, L"ProbeOfficialApi") == 0) {
        return kProbeOfficialApi;
    }
    if (_wcsicmp(name, L"InspectStructure") == 0) {
        return kInspectStructure;
    }
    if (_wcsicmp(name, L"InspectRoutingContext") == 0) {
        return kInspectRoutingContext;
    }
    if (_wcsicmp(name, L"InspectParagraphStyles") == 0) {
        return kInspectParagraphStyles;
    }
    if (_wcsicmp(name, L"SaveReopenVerify") == 0) {
        return kSaveReopenVerify;
    }
    if (_wcsicmp(name, L"InspectPagesV3") == 0) {
        return kInspectPagesV3;
    }
    if (_wcsicmp(name, L"SaveVerify") == 0) {
        return kSaveVerify;
    }
    if (_wcsicmp(name, L"TargetDocumentID") == 0) {
        return kTargetDocumentId;
    }
    if (_wcsicmp(name, L"ActivateDocument") == 0) {
        return kActivateDocument;
    }
    if (_wcsicmp(name, L"ActivationStatus") == 0) {
        return kActivationStatus;
    }
    if (_wcsicmp(name, L"BeginForegroundGuard") == 0) {
        return kBeginForegroundGuard;
    }
    if (_wcsicmp(name, L"EndForegroundGuard") == 0) {
        return kEndForegroundGuard;
    }
    if (_wcsicmp(name, L"ContentSignature") == 0) {
        return kContentSignature;
    }
    if (_wcsicmp(name, L"ContentRevision") == 0) {
        return kContentRevision;
    }
    if (_wcsicmp(name, L"PrepareTextPatches") == 0) {
        return kPrepareTextPatches;
    }
    if (_wcsicmp(name, L"PrepareLiteralTextPatches") == 0) {
        return kPrepareLiteralTextPatches;
    }
    if (_wcsicmp(name, L"ExecutePreparedTextPatches") == 0) {
        return kExecutePreparedTextPatches;
    }
    if (_wcsicmp(name, L"ExecuteHistory") == 0) {
        return kExecuteHistory;
    }
    if (_wcsicmp(name, L"ExecuteActionsChecked") == 0) {
        return kExecuteActionsChecked;
    }
    if (_wcsicmp(name, L"ExecuteProtocolBundle") == 0) {
        return kExecuteProtocolBundle;
    }
    if (_wcsicmp(name, L"GraphProtocolVersion") == 0) {
        return kGraphProtocolVersion;
    }
    if (_wcsicmp(name, L"GraphCapabilities") == 0) {
        return kGraphCapabilities;
    }
    if (_wcsicmp(name, L"GraphOpen") == 0) {
        return kGraphOpen;
    }
    if (_wcsicmp(name, L"GraphNext") == 0) {
        return kGraphNext;
    }
    if (_wcsicmp(name, L"GraphCancel") == 0) {
        return kGraphCancel;
    }
    if (_wcsicmp(name, L"GraphClose") == 0) {
        return kGraphClose;
    }
    if (_wcsicmp(name, L"PatchBegin") == 0) {
        return kPatchBegin;
    }
    if (_wcsicmp(name, L"PatchChunk") == 0) {
        return kPatchChunk;
    }
    if (_wcsicmp(name, L"PatchCommit") == 0) {
        return kPatchCommit;
    }
    if (_wcsicmp(name, L"PatchAbort") == 0) {
        return kPatchAbort;
    }
    if (_wcsicmp(name, L"PatchValidate") == 0) {
        return kPatchValidate;
    }
    if (_wcsicmp(name, L"PatchApply") == 0) {
        return kPatchApply;
    }
    if (_wcsicmp(name, L"BlobRead") == 0) {
        return kBlobRead;
    }
    return DISPID_UNKNOWN;
}

// Every member of this interface that can leave the document different from how
// it found it. Writes that never come through this interface -- the direct
// HAction paths described at gWriteEpoch -- are outside what this can classify.
//
// ExecuteProtocolBundle is on the list although a bundle can be read-only. The
// two mistakes are not symmetric: counting a read as a write costs one
// recomputation, missing a write serves a stale derivation of a document that
// has moved on.
bool MemberWritesDocument(const DISPID memberId) noexcept {
    return memberId == kExecute ||
        memberId == kExecuteActions ||
        memberId == kExecuteActionsChecked ||
        memberId == kExecutePreparedTextPatches ||
        memberId == kExecuteHistory ||
        memberId == kExecuteProtocolBundle ||
        memberId == kPatchCommit ||
        memberId == kPatchApply;
}

HRESULT ActivateDocumentNow(
    IDispatch* const hwp,
    const LONG documentId) noexcept {
    if (hwp == nullptr || documentId <= 0) {
        return E_INVALIDARG;
    }
    CComVariant rawDocuments;
    HRESULT status = hancom::dispatch::PropertyGet(
        hwp,
        L"XHwpDocuments",
        &rawDocuments);
    CComPtr<IDispatch> documents;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsDispatch(rawDocuments, documents);
    }
    CComVariant rawDocument;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::Method(
            documents,
            L"FindItem",
            {CComVariant(documentId)},
            &rawDocument);
    }
    CComPtr<IDispatch> document;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsDispatch(rawDocument, document);
    }
    CComVariant ignored;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::Method(
            document,
            L"SetActive_XHwpDocument",
            {},
            &ignored);
    }
    CComVariant rawActive;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::PropertyGet(
            documents,
            L"Active_XHwpDocument",
            &rawActive);
    }
    CComPtr<IDispatch> active;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsDispatch(rawActive, active);
    }
    CComVariant rawActiveId;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::PropertyGet(
            active,
            L"DocumentID",
            &rawActiveId);
    }
    LONG activeId = 0;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsLong(rawActiveId, &activeId);
    }
    if (FAILED(status)) {
        return status;
    }
    if (activeId != documentId) {
        return HRESULT_FROM_WIN32(ERROR_INVALID_STATE);
    }
    return S_OK;
}

HRESULT ProbeOfficialApiUnguarded(
    IDispatch* const hwp,
    BSTR const payload,
    VARIANT* const result) {
    const std::wstring response = hancom::official_api::Probe(
        hwp,
        std::wstring(payload, SysStringLen(payload)));
    VariantInit(result);
    result->vt = VT_BSTR;
    result->bstrVal = SysAllocStringLen(
        response.data(),
        static_cast<UINT>(response.size()));
    return result->bstrVal == nullptr && !response.empty() ? E_OUTOFMEMORY : S_OK;
}

HRESULT ProbeOfficialApiGuarded(
    IDispatch* const hwp,
    BSTR const payload,
    VARIANT* const result) noexcept {
    __try {
        return ProbeOfficialApiUnguarded(hwp, payload, result);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        wchar_t response[64]{};
        static_cast<void>(swprintf_s(
            response,
            L"HCV1\tERROR\tSEH\t%08lX",
            GetExceptionCode()));
        VariantInit(result);
        result->vt = VT_BSTR;
        result->bstrVal = SysAllocString(response);
        return result->bstrVal == nullptr ? E_OUTOFMEMORY : S_OK;
    }
}

std::wstring HistoryErrorResponse(
    const wchar_t* const code,
    const std::wstring& message,
    const bool mutationStarted = false) {
    return std::wstring(L"HCH1\tERROR\t") + code + L'\t' + message +
        (mutationStarted ? L"\t1" : L"\t0");
}

HRESULT ResolveActiveDocument(
    IDispatch* const hwp,
    CComPtr<IDispatch>& document) {
    CComVariant rawDocuments;
    HRESULT status = hancom::dispatch::PropertyGet(
        hwp,
        L"XHwpDocuments",
        &rawDocuments);
    CComPtr<IDispatch> documents;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsDispatch(rawDocuments, documents);
    }
    CComVariant rawDocument;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::PropertyGet(
            documents,
            L"Active_XHwpDocument",
            &rawDocument);
    }
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsDispatch(rawDocument, document);
    }
    return status;
}

std::wstring ExecuteHistoryUnguarded(
    IDispatch* const hwp,
    const std::wstring& direction,
    const std::wstring& expectedSignature) {
    const wchar_t* method = nullptr;
    if (direction == L"undo") {
        method = L"Undo";
    } else if (direction == L"redo") {
        method = L"Redo";
    } else {
        return HistoryErrorResponse(
            L"BAD_DIRECTION",
            L"history direction must be undo or redo");
    }
    if (!hancom::official_api::DocumentContentSignatureIsComplete(
            expectedSignature)) {
        return HistoryErrorResponse(
            L"BAD_SIGNATURE",
            L"expected content signature is incomplete or malformed");
    }

    CComPtr<IDispatch> document;
    const HRESULT documentStatus = ResolveActiveDocument(hwp, document);
    if (FAILED(documentStatus)) {
        return HistoryErrorResponse(
            L"ACTIVE_DOCUMENT",
            std::to_wstring(static_cast<LONG>(documentStatus)));
    }

    const auto started = std::chrono::steady_clock::now();
    const std::wstring before =
        hancom::official_api::FormatDocumentContentSignature(
            hancom::official_api::CaptureDocumentContentSignature(hwp));
    if (!hancom::official_api::DocumentContentSignatureIsComplete(before) ||
        before != expectedSignature) {
        return HistoryErrorResponse(
            L"STALE_CONTENT",
            L"live document content changed after history authorization");
    }

    // No HWP call sits between the complete-signature comparison above and
    // this destructive invocation.
    CComVariant raw;
    HRESULT status = hancom::dispatch::Method(
        document,
        method,
        {CComVariant(1L)},
        &raw);
    bool applied = false;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsBool(raw, &applied);
    }
    if (FAILED(status)) {
        return HistoryErrorResponse(
            L"HISTORY_INVOKE",
            std::to_wstring(static_cast<LONG>(status)),
            true);
    }
    const std::wstring after =
        hancom::official_api::FormatDocumentContentSignature(
            hancom::official_api::CaptureDocumentContentSignature(hwp));
    if (!hancom::official_api::DocumentContentSignatureIsComplete(after)) {
        return HistoryErrorResponse(
            L"POST_SIGNATURE",
            L"post-history content signature is incomplete or malformed",
            true);
    }
    const long long elapsed =
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - started).count();
    std::wostringstream response;
    response << L"HCH1\tOK\t" << (applied ? 1 : 0)
             << L'\t' << elapsed
             << L'\t' << before
             << L'\t' << after;
    return response.str();
}

}

BatchAutomation::BatchAutomation(
    IDispatch* const hwp,
    const LONG targetDocumentId,
    const HWND windowHandle) noexcept
    : hwp_(hwp),
      targetDocumentId_(targetDocumentId),
      windowHandle_(windowHandle) {}

BatchAutomation::~BatchAutomation() {
    hancom::graph::protocol::NoteRouteOwnerDestruction(
        {targetDocumentId_, reinterpret_cast<std::uintptr_t>(windowHandle_)});
    if (activationFailureEvent_ != nullptr) {
        CloseHandle(activationFailureEvent_);
    }
    if (activationSuccessEvent_ != nullptr) {
        CloseHandle(activationSuccessEvent_);
    }
}

struct BatchAutomation::ActivationWork {
    BatchAutomation* owner = nullptr;
    IStream* stream = nullptr;
    LONG documentId = 0;
    // STA thread that started the activation. ActivateDocumentNow runs on the
    // worker but marshals back to this thread, so this is where the activation
    // it triggers actually happens and where the guard has to hook.
    DWORD ownerThreadId = 0;
};

HRESULT BatchAutomation::BeginActivation(
    const LONG documentId,
    VARIANT* const result) noexcept {
    if (documentId <= 0 || result == nullptr || hwp_ == nullptr) {
        return E_INVALIDARG;
    }
    const LONG previousState = InterlockedCompareExchange(
        &activationState_,
        kActivationPending,
        kActivationIdle);
    if (previousState == kActivationPending) {
        if (InterlockedCompareExchange(
                &activationDocumentId_,
                0,
                0) != documentId) {
            return HRESULT_FROM_WIN32(ERROR_BUSY);
        }
        VariantInit(result);
        return ReturnString(activationToken_, result);
    }
    if (previousState != kActivationIdle &&
        InterlockedCompareExchange(
            &activationState_,
            kActivationPending,
            previousState) != previousState) {
        return HRESULT_FROM_WIN32(ERROR_BUSY);
    }
    InterlockedExchange(&activationDocumentId_, documentId);
    InterlockedExchange(&activationHresult_, E_PENDING);
    if (activationFailureEvent_ != nullptr) {
        CloseHandle(activationFailureEvent_);
        activationFailureEvent_ = nullptr;
    }
    if (activationSuccessEvent_ != nullptr) {
        CloseHandle(activationSuccessEvent_);
        activationSuccessEvent_ = nullptr;
    }
    const LONG sequence = InterlockedIncrement(&gActivationSequence);
    wchar_t token[128]{};
    if (swprintf_s(
            token,
            L"Local\\HancomLiveActivation.%lu.%ld",
            static_cast<unsigned long>(GetCurrentProcessId()),
            sequence) < 0) {
        FinishActivation(E_FAIL);
        return E_FAIL;
    }
    activationToken_.assign(token);
    const std::wstring successName = activationToken_ + L".Success";
    const std::wstring failureName = activationToken_ + L".Failure";
    activationSuccessEvent_ = CreateEventW(
        nullptr,
        TRUE,
        FALSE,
        successName.c_str());
    activationFailureEvent_ = CreateEventW(
        nullptr,
        TRUE,
        FALSE,
        failureName.c_str());
    if (activationSuccessEvent_ == nullptr ||
        activationFailureEvent_ == nullptr) {
        const HRESULT eventStatus = HRESULT_FROM_WIN32(GetLastError());
        FinishActivation(eventStatus);
        return eventStatus;
    }

    IStream* stream = nullptr;
    HRESULT status = CoMarshalInterThreadInterfaceInStream(
        IID_IDispatch,
        hwp_,
        &stream);
    if (FAILED(status)) {
        FinishActivation(status);
        return status;
    }
    ActivationWork* const work = new (std::nothrow) ActivationWork{
        this,
        stream,
        documentId,
        GetCurrentThreadId(),
    };
    if (work == nullptr) {
        stream->Release();
        FinishActivation(E_OUTOFMEMORY);
        return E_OUTOFMEMORY;
    }
    static_cast<void>(AddRef());
    HANDLE const thread = CreateThread(
        nullptr,
        0,
        &BatchAutomation::RunActivation,
        work,
        0,
        nullptr);
    if (thread == nullptr) {
        const HRESULT threadStatus = HRESULT_FROM_WIN32(GetLastError());
        static_cast<void>(Release());
        stream->Release();
        delete work;
        FinishActivation(threadStatus);
        return threadStatus;
    }
    CloseHandle(thread);
    return ReturnString(activationToken_, result);
}

HRESULT BatchAutomation::PollActivation(
    const LONG documentId,
    VARIANT* const result) noexcept {
    if (documentId <= 0 || result == nullptr) {
        return E_INVALIDARG;
    }
    if (InterlockedCompareExchange(
            &activationDocumentId_,
            0,
            0) != documentId) {
        return HRESULT_FROM_WIN32(ERROR_INVALID_STATE);
    }
    const LONG state = InterlockedCompareExchange(
        &activationState_,
        0,
        0);
    LONG publicState = 0;
    if (state == kActivationSucceeded) {
        publicState = 1;
    } else if (state == kActivationFailed) {
        publicState = InterlockedCompareExchange(
            &activationHresult_,
            0,
            0);
        if (publicState == 0 || publicState == 1) {
            publicState = E_FAIL;
        }
    } else if (state != kActivationPending) {
        return HRESULT_FROM_WIN32(ERROR_INVALID_STATE);
    }
    VariantInit(result);
    result->vt = VT_I4;
    result->lVal = publicState;
    return S_OK;
}

DWORD WINAPI BatchAutomation::RunActivation(void* const context) noexcept {
    ActivationWork* const work = static_cast<ActivationWork*>(context);
    if (work == nullptr || work->owner == nullptr || work->stream == nullptr) {
        return ERROR_INVALID_PARAMETER;
    }
    // Armed here rather than in Invoke: BeginActivation returns as soon as the
    // thread is created, so the Invoke guard is already gone by the time the
    // tab switch runs. Nothing can trigger an activation between those two
    // points, because the switch is this thread's own next COM call.
    hancom::foreground::ForegroundGuard activationGuard;
    activationGuard.Arm(work->ownerThreadId);
    const HRESULT initialized = CoInitializeEx(
        nullptr,
        COINIT_MULTITHREADED);
    CComPtr<IDispatch> hwp;
    IDispatch* rawHwp = nullptr;
    HRESULT status = initialized;
    if (SUCCEEDED(status)) {
        status = CoGetInterfaceAndReleaseStream(
            work->stream,
            IID_IDispatch,
            reinterpret_cast<void**>(&rawHwp));
        work->stream = nullptr;
        if (SUCCEEDED(status)) {
            hwp.Attach(rawHwp);
        }
    }
    if (SUCCEEDED(status)) {
        status = ActivateDocumentNow(hwp, work->documentId);
    }
    work->owner->FinishActivation(status);
    // Released after FinishActivation so the caller is never held up by the
    // watch below. SetActive_XHwpDocument is marshalled to the STA, so the
    // window it raises can appear after this thread's call returns; a single
    // check right here would run too early to see it. This is a dedicated
    // worker about to exit and it is not the Hangul UI thread, so waiting here
    // blocks nobody. The refusal stays installed for the whole watch.
    static_cast<void>(activationGuard.ReleaseWithin(
        hancom::foreground::kActivationTailMilliseconds,
        hancom::foreground::kActivationPollMilliseconds));
    if (SUCCEEDED(initialized)) {
        CoUninitialize();
    } else if (work->stream != nullptr) {
        work->stream->Release();
    }
    static_cast<void>(work->owner->Release());
    delete work;
    return FAILED(status)
        ? static_cast<DWORD>(status)
        : ERROR_SUCCESS;
}

void BatchAutomation::FinishActivation(const HRESULT status) noexcept {
    InterlockedExchange(&activationHresult_, status);
    InterlockedExchange(
        &activationState_,
        SUCCEEDED(status) ? kActivationSucceeded : kActivationFailed);
    HANDLE const event = SUCCEEDED(status)
        ? activationSuccessEvent_
        : activationFailureEvent_;
    if (event != nullptr) {
        SetEvent(event);
    }
}

HRESULT STDMETHODCALLTYPE BatchAutomation::QueryInterface(
    REFIID interfaceId,
    void** const object) {
    if (object == nullptr) {
        return E_POINTER;
    }
    if (interfaceId == IID_IUnknown || interfaceId == IID_IDispatch) {
        *object = static_cast<IDispatch*>(this);
        static_cast<void>(AddRef());
        return S_OK;
    }
    *object = nullptr;
    return E_NOINTERFACE;
}

ULONG STDMETHODCALLTYPE BatchAutomation::AddRef() {
    return static_cast<ULONG>(InterlockedIncrement(&references_));
}

ULONG STDMETHODCALLTYPE BatchAutomation::Release() {
    const LONG remaining = InterlockedDecrement(&references_);
    if (remaining == 0) {
        delete this;
    }
    return static_cast<ULONG>(remaining);
}

HRESULT STDMETHODCALLTYPE BatchAutomation::GetTypeInfoCount(UINT* const count) {
    if (count == nullptr) {
        return E_POINTER;
    }
    *count = 0;
    return S_OK;
}

HRESULT STDMETHODCALLTYPE BatchAutomation::GetTypeInfo(
    UINT,
    LCID,
    ITypeInfo**) {
    return E_NOTIMPL;
}

HRESULT STDMETHODCALLTYPE BatchAutomation::GetIDsOfNames(
    REFIID interfaceId,
    LPOLESTR* const names,
    const UINT count,
    LCID,
    DISPID* const memberIds) {
    if (interfaceId != IID_NULL) {
        return DISP_E_UNKNOWNINTERFACE;
    }
    if (names == nullptr || memberIds == nullptr) {
        return E_POINTER;
    }
    HRESULT status = S_OK;
    for (UINT index = 0; index < count; ++index) {
        memberIds[index] = names[index] == nullptr ? DISPID_UNKNOWN : MemberId(names[index]);
        if (memberIds[index] == DISPID_UNKNOWN) {
            status = DISP_E_UNKNOWNNAME;
        }
    }
    return status;
}

HRESULT BatchAutomation::ReturnString(
    const std::wstring& value,
    VARIANT* const result) noexcept {
    if (result == nullptr) {
        return E_POINTER;
    }
    VariantInit(result);
    result->vt = VT_BSTR;
    result->bstrVal = SysAllocStringLen(value.data(), static_cast<UINT>(value.size()));
    return result->bstrVal == nullptr && !value.empty() ? E_OUTOFMEMORY : S_OK;
}

HRESULT BatchAutomation::VerifyTarget() {
    if (targetDocumentId_ <= 0) {
        return S_OK;
    }
    CComVariant rawDocuments;
    HRESULT status = hancom::dispatch::PropertyGet(
        hwp_,
        L"XHwpDocuments",
        &rawDocuments);
    CComPtr<IDispatch> documents;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsDispatch(rawDocuments, documents);
    }
    CComVariant rawActive;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::PropertyGet(
            documents,
            L"Active_XHwpDocument",
            &rawActive);
    }
    CComPtr<IDispatch> active;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsDispatch(rawActive, active);
    }
    CComVariant rawActiveId;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::PropertyGet(
            active,
            L"DocumentID",
            &rawActiveId);
    }
    LONG activeId = 0;
    if (SUCCEEDED(status)) {
        status = hancom::dispatch::AsLong(rawActiveId, &activeId);
    }
    if (FAILED(status)) {
        return status;
    }
    return activeId == targetDocumentId_
        ? S_OK
        : HRESULT_FROM_WIN32(ERROR_INVALID_STATE);
}

HRESULT STDMETHODCALLTYPE BatchAutomation::Invoke(
    const DISPID memberId,
    REFIID interfaceId,
    LCID,
    const WORD flags,
    DISPPARAMS* const parameters,
    VARIANT* const result,
    EXCEPINFO*,
    UINT* const argumentError) {
    if (interfaceId != IID_NULL) {
        return DISP_E_UNKNOWNINTERFACE;
    }
    if (argumentError != nullptr) {
        *argumentError = 0;
    }
    const UINT argumentCount = parameters == nullptr ? 0 : parameters->cArgs;
    // Declared before every write branch, including the graph patch members
    // routed away below, so no write can leave Invoke without having moved the
    // epoch on both sides of itself. Invoke has dozens of early returns, so a
    // destructor is the only shape that covers all of them -- the same reason
    // the foreground guard further down is built this way.
    const WriteEpochBracket writeEpoch(memberId, flags);
    if (memberId == kProtocolVersion) {
        if ((flags & DISPATCH_PROPERTYGET) == 0 || argumentCount != 0 || result == nullptr) {
            return DISP_E_BADPARAMCOUNT;
        }
        VariantInit(result);
        result->vt = VT_I4;
        result->lVal = 14;
        return S_OK;
    }
    if (memberId == kTargetDocumentId) {
        if ((flags & DISPATCH_PROPERTYGET) == 0 || argumentCount != 0 ||
            result == nullptr) {
            return DISP_E_BADPARAMCOUNT;
        }
        VariantInit(result);
        result->vt = VT_I4;
        result->lVal = targetDocumentId_;
        return S_OK;
    }
    if ((memberId >= kGraphProtocolVersion && memberId <= kPatchApply) ||
        memberId == kBlobRead) {
        return hancom::graph::protocol::InvokeGraphMember(
            memberId,
            flags,
            parameters,
            result,
            {targetDocumentId_, reinterpret_cast<std::uintptr_t>(windowHandle_)},
            hwp_);
    }
    if (memberId == kPing) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        return ReturnString(L"HCB14\tPONG\t14", result);
    }
    if (memberId == kBeginForegroundGuard) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        return ReturnString(
            hancom::foreground::BeginSharedGuard()
                ? L"HCB13\tGUARD\tARMED"
                : L"HCB13\tGUARD\tIDLE",
            result);
    }
    if (memberId == kEndForegroundGuard) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        const bool restored = hancom::foreground::EndSharedGuard();
        wchar_t response[64]{};
        if (swprintf_s(
                response,
                L"HCB13\tGUARD\t%s\t%ld",
                restored ? L"RESTORED" : L"NOOP",
                hancom::foreground::BlockedActivationCount()) < 0) {
            return ReturnString(L"HCB13\tGUARD\tNOOP", result);
        }
        return ReturnString(response, result);
    }
    // Everything below can reach into Hangul and make it raise its own window.
    // The guard refuses that activation outright, and hands the foreground back
    // if it slipped through anyway. Declared after the branches above so the
    // hot, trivial members (ProtocolVersion, TargetDocumentID, Ping) keep
    // costing nothing. Invoke has dozens of early returns, so the destructor is
    // the only shape that covers all of them.
    hancom::foreground::ForegroundGuard foregroundGuard;
    foregroundGuard.Arm();
    if (memberId == kContentSignature) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        if (targetDocumentId_ > 0) {
            const HRESULT status = VerifyTarget();
            if (FAILED(status)) {
                return status;
            }
        }
        const hancom::official_api::DocumentContentSignature signature =
            hancom::official_api::CaptureDocumentContentSignature(hwp_);
        if (!signature.captured) {
            return ReturnString(L"", result);
        }
        return ReturnString(
            hancom::official_api::FormatDocumentContentSignature(signature),
            result);
    }
    if (memberId == kContentRevision) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        if (targetDocumentId_ > 0) {
            const HRESULT status = VerifyTarget();
            if (FAILED(status)) {
                return status;
            }
        }
        hancom::official_api::DocumentContentRevision revision =
            hancom::official_api::CaptureDocumentContentRevision(hwp_);
        // The capture reads the document; only the bridge can say which process
        // read it and how many writes it had made by then.
        revision.sessionTag =
            static_cast<std::uint64_t>(GetCurrentProcessId());
        revision.writeEpoch = CurrentWriteEpoch();
        // An unusable capture answers with an empty string, exactly as
        // ContentSignature does, and the caller reads that as "unknown".
        return ReturnString(
            hancom::official_api::FormatDocumentContentRevision(revision),
            result);
    }
    if (memberId == kPrepareTextPatches || memberId == kPrepareLiteralTextPatches) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr ||
            argumentCount != 1 || result == nullptr) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant payload(parameters->rgvarg[0]);
        const HRESULT converted = payload.ChangeType(VT_BSTR);
        if (FAILED(converted) || payload.bstrVal == nullptr) {
            return DISP_E_TYPEMISMATCH;
        }
        hancom::actions::Request request;
        hancom::actions::Error parseError;
        if (!hancom::actions::ParseRequest(
                std::wstring(payload.bstrVal, SysStringLen(payload.bstrVal)),
                &request,
                &parseError)) {
            std::wostringstream output;
            // A request that never parsed never reached the document, so the
            // two trailing fields -- retrySafe, partialMutation -- are the only
            // pair a refusal at this point can carry.
            output << L"HTP2\tERROR\t0\t" << parseError.code << L'\t'
                   << hancom::encoding::EncodeUtf8Base64(parseError.location) << L'\t'
                   << hancom::encoding::EncodeUtf8Base64(parseError.message)
                   << L"\t1\t0";
            return ReturnString(output.str(), result);
        }
        const hancom::actions::ExecutionResult prepared =
            hancom::actions::PreflightTextPatches(hwp_, request);
        std::wostringstream output;
        if (!prepared.succeeded) {
            // Preflight computes both of these and they are the whole answer to
            // "may the caller simply try again": a preflight that touched the
            // document is not repeatable. Dropping them here left the caller
            // assuming the safe pair for every refusal alike.
            output << L"HTP2\tERROR\t" << prepared.failedRequestIndex << L'\t'
                   << prepared.error.code << L'\t'
                   << hancom::encoding::EncodeUtf8Base64(prepared.error.location) << L'\t'
                   << hancom::encoding::EncodeUtf8Base64(prepared.error.message)
                   << L'\t' << (prepared.retrySafe ? 1 : 0)
                   << L'\t' << (prepared.partialMutation ? 1 : 0);
            return ReturnString(output.str(), result);
        }
        hancom::official_api::DocumentContentRevision revision =
            hancom::official_api::CaptureDocumentContentRevision(hwp_);
        revision.sessionTag = static_cast<std::uint64_t>(GetCurrentProcessId());
        revision.writeEpoch = CurrentWriteEpoch();
        const std::wstring formatted =
            hancom::official_api::FormatDocumentContentRevision(revision);
        if (!hancom::official_api::DocumentContentRevisionIsComplete(formatted)) {
            output << L"HTP2\tERROR\t0\tCONTENT_REVISION\t"
                   << hancom::encoding::EncodeUtf8Base64(L"text.patch.preflight")
                   << L'\t'
                   << hancom::encoding::EncodeUtf8Base64(
                          L"content revision is unavailable after preflight")
                   << L'\t' << (prepared.retrySafe ? 1 : 0)
                   << L'\t' << (prepared.partialMutation ? 1 : 0);
            return ReturnString(output.str(), result);
        }
        output << L"HTP2\tOK\t"
               << hancom::encoding::EncodeUtf8Base64(formatted) << L'\t'
               << prepared.preflightTargetCount << L'\t'
               << prepared.elapsedMicroseconds;
        for (const hancom::actions::PreparedTextPatchTarget& target :
             prepared.preparedTextPatchTargets) {
            output << L'\t' << target.startList
                   << L'\t' << target.startParagraph
                   << L'\t' << target.startCharacter
                   << L'\t' << target.endList
                   << L'\t' << target.endParagraph
                   << L'\t' << target.endCharacter
                   << L'\t' << hancom::encoding::EncodeUtf8Base64(target.text)
                   << L'\t' << hancom::encoding::EncodeUtf8Base64(target.faceName)
                   << L'\t' << target.height
                   << L'\t' << (target.bold ? 1 : 0)
                   << L'\t' << target.textColor
                   << L'\t' << target.alignment << L',' << target.lineSpacing;
        }
        return ReturnString(output.str(), result);
    }
    if (memberId == kActivateDocument) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr ||
            argumentCount != 1 || result == nullptr) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant documentId(parameters->rgvarg[0]);
        const HRESULT converted = documentId.ChangeType(VT_I4);
        if (FAILED(converted) || documentId.lVal <= 0) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        if (targetDocumentId_ > 0 &&
            targetDocumentId_ != documentId.lVal) {
            return HRESULT_FROM_WIN32(ERROR_INVALID_STATE);
        }
        return BeginActivation(documentId.lVal, result);
    }
    if (memberId == kActivationStatus) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr ||
            argumentCount != 1 || result == nullptr) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant documentId(parameters->rgvarg[0]);
        const HRESULT converted = documentId.ChangeType(VT_I4);
        if (FAILED(converted) || documentId.lVal <= 0) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return PollActivation(documentId.lVal, result);
    }
    if (targetDocumentId_ > 0) {
        const HRESULT status = VerifyTarget();
        if (FAILED(status)) {
            return status;
        }
    }
    if (memberId == kExecuteHistory) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr ||
            argumentCount != 2 || result == nullptr) {
            return DISP_E_BADPARAMCOUNT;
        }
        // IDispatch arguments are stored in reverse call order.
        CComVariant direction(parameters->rgvarg[1]);
        CComVariant expected(parameters->rgvarg[0]);
        const HRESULT directionStatus = direction.ChangeType(VT_BSTR);
        const HRESULT expectedStatus = expected.ChangeType(VT_BSTR);
        if (FAILED(directionStatus) || FAILED(expectedStatus) ||
            direction.bstrVal == nullptr || expected.bstrVal == nullptr) {
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(
            ExecuteHistoryUnguarded(
                hwp_,
                std::wstring(
                    direction.bstrVal,
                    SysStringLen(direction.bstrVal)),
                std::wstring(
                    expected.bstrVal,
                    SysStringLen(expected.bstrVal))),
            result);
    }
    if (memberId == kSaveVerify) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        return ReturnString(hancom::lifecycle::SaveVerify(hwp_), result);
    }
    if (memberId == kSaveReopenVerify) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        return ReturnString(hancom::lifecycle::SaveReopenVerify(hwp_), result);
    }
    if (memberId == kSnapshot) {
        if ((flags & DISPATCH_METHOD) == 0 || argumentCount != 0) {
            return DISP_E_BADPARAMCOUNT;
        }
        return ReturnString(hancom::inspection::Snapshot(hwp_), result);
    }
    if (memberId == kInspectPage) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant page(parameters->rgvarg[0]);
        const HRESULT converted = page.ChangeType(VT_I4);
        if (FAILED(converted)) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(hancom::inspection::InspectPage(hwp_, page.lVal), result);
    }
    if (memberId == kInspectPageV3) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant page(parameters->rgvarg[0]);
        const HRESULT converted = page.ChangeType(VT_I4);
        if (FAILED(converted)) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(hancom::inspection::InspectPageV3(hwp_, page.lVal), result);
    }
    if (memberId == kInspectPageSummary) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant page(parameters->rgvarg[0]);
        const HRESULT converted = page.ChangeType(VT_I4);
        if (FAILED(converted)) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(hancom::inspection::InspectPageSummary(hwp_, page.lVal), result);
    }
    if (memberId == kInspectPagesV3) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant pages(parameters->rgvarg[0]);
        const HRESULT converted = pages.ChangeType(VT_BSTR);
        if (FAILED(converted) || pages.bstrVal == nullptr) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(
            hancom::inspection::InspectPagesV3(
                hwp_,
                std::wstring(pages.bstrVal, SysStringLen(pages.bstrVal))),
            result);
    }
    if (memberId == kInspectRoutingContext) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant pageHint(parameters->rgvarg[0]);
        const HRESULT converted = pageHint.ChangeType(VT_I4);
        if (FAILED(converted)) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(
            hancom::inspection::InspectRoutingContext(hwp_, pageHint.lVal),
            result);
    }
    if (memberId == kInspectStructure) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant page(parameters->rgvarg[0]);
        const HRESULT converted = page.ChangeType(VT_I4);
        if (FAILED(converted)) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(hancom::inspection::InspectStructure(hwp_, page.lVal), result);
    }
    if (memberId == kInspectParagraphStyles) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant scan(parameters->rgvarg[0]);
        const HRESULT converted = scan.ChangeType(VT_BSTR);
        if (FAILED(converted) || scan.bstrVal == nullptr) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(
            hancom::inspection::InspectParagraphStyles(
                hwp_,
                std::wstring(scan.bstrVal, SysStringLen(scan.bstrVal))),
            result);
    }
    if (memberId == kProbeOfficialApi) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant payload(parameters->rgvarg[0]);
        const HRESULT converted = payload.ChangeType(VT_BSTR);
        if (FAILED(converted) || payload.bstrVal == nullptr) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ProbeOfficialApiGuarded(hwp_, payload.bstrVal, result);
    }
    if (memberId == kExecuteActions ||
        memberId == kExecuteActionsChecked ||
        memberId == kExecutePreparedTextPatches) {
        const bool checked = memberId == kExecuteActionsChecked;
        const bool prepared = memberId == kExecutePreparedTextPatches;
        const bool hasPrecondition = checked || prepared;
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr ||
            argumentCount != (hasPrecondition ? 2U : 1U)) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant payload(parameters->rgvarg[hasPrecondition ? 1 : 0]);
        const HRESULT converted = payload.ChangeType(VT_BSTR);
        if (FAILED(converted) || payload.bstrVal == nullptr) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        hancom::actions::Request request;
        hancom::actions::Error parseError;
        std::wstring response;
        const std::wstring requestPayload(payload.bstrVal, SysStringLen(payload.bstrVal));
        if (!hancom::actions::ParseRequest(requestPayload, &request, &parseError)) {
            response = hancom::actions::ErrorResponse(parseError);
        } else {
            CComVariant expected;
            if (hasPrecondition) {
                expected = parameters->rgvarg[0];
                const HRESULT expectedStatus = expected.ChangeType(VT_BSTR);
                if (FAILED(expectedStatus) || expected.bstrVal == nullptr) {
                    return DISP_E_TYPEMISMATCH;
                }
            }
            if (checked) {
                request.expectedContentSignature.assign(
                    expected.bstrVal,
                    SysStringLen(expected.bstrVal));
                request.requiresContentAuthorization = true;
            }
            hancom::actions::ExecutionResult execution;
            if (prepared) {
                hancom::official_api::DocumentContentRevision revision =
                    hancom::official_api::CaptureDocumentContentRevision(hwp_);
                revision.sessionTag =
                    static_cast<std::uint64_t>(GetCurrentProcessId());
                revision.writeEpoch = CurrentWriteEpoch() - 1;
                const std::wstring actual =
                    hancom::official_api::FormatDocumentContentRevision(revision);
                const std::wstring required(
                    expected.bstrVal,
                    SysStringLen(expected.bstrVal));
                if (actual != required) {
                    execution.error.code = L"STALE_PREPARED_RECEIPT";
                    execution.error.location = L"text.patch.preflight";
                    execution.error.message =
                        L"document revision changed after text.patch preflight";
                    execution.failedStep = L"text.patch.preflight";
                    execution.retrySafe = true;
                } else {
                    execution = hancom::actions::Execute(hwp_, request);
                }
            } else {
                execution = hancom::actions::Execute(hwp_, request);
            }
            response = execution.succeeded
                ? hancom::actions::SuccessResponse(execution)
                : hancom::actions::FailureResponse(execution);
        }
        return ReturnString(response, result);
    }
    if (memberId == kExecuteProtocolBundle) {
        if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr ||
            argumentCount != 1) {
            return DISP_E_BADPARAMCOUNT;
        }
        CComVariant payload(parameters->rgvarg[0]);
        const HRESULT converted = payload.ChangeType(VT_BSTR);
        if (FAILED(converted) || payload.bstrVal == nullptr) {
            if (argumentError != nullptr) {
                *argumentError = 0;
            }
            return DISP_E_TYPEMISMATCH;
        }
        return ReturnString(
            hancom::protocol_bundle::Execute(
                hwp_,
                targetDocumentId_,
                windowHandle_,
                std::wstring(payload.bstrVal, SysStringLen(payload.bstrVal))),
            result);
    }
    if (memberId != kExecute) {
        return DISP_E_MEMBERNOTFOUND;
    }
    if ((flags & DISPATCH_METHOD) == 0 || parameters == nullptr || argumentCount != 1) {
        return DISP_E_BADPARAMCOUNT;
    }
    CComVariant payload(parameters->rgvarg[0]);
    const HRESULT converted = payload.ChangeType(VT_BSTR);
    if (FAILED(converted) || payload.bstrVal == nullptr) {
        if (argumentError != nullptr) {
            *argumentError = 0;
        }
        return DISP_E_TYPEMISMATCH;
    }

    hancom::batch::Request request;
    hancom::batch::Error parseError;
    std::wstring response;
    const std::wstring requestPayload(payload.bstrVal, SysStringLen(payload.bstrVal));
    if (!hancom::batch::ParseRequest(requestPayload, &request, &parseError)) {
        response = hancom::batch::ErrorResponse(parseError);
    } else {
        const hancom::batch::ExecutionResult execution = hancom::batch::Execute(hwp_, request);
        response = execution.succeeded
            ? hancom::batch::SuccessResponse(
                  execution.textUpdates,
                  execution.imageUpdates,
                  execution.elapsedMicroseconds)
            : hancom::batch::ErrorResponse(execution.error);
    }
    return ReturnString(response, result);
}
