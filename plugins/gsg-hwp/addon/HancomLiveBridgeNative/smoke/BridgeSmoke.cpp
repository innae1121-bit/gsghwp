#include <Windows.h>
#include <ObjIdl.h>
#include <Ocidl.h>
#include <Ole2.h>
#include <atlcomcli.h>

#include "../BridgeStatus.h"
#include "../DocumentGraphProtocol.h"
#include "../OfficialApiVirtualMethod.h"
#include "../ParagraphText.h"
#include "../TableInspection.h"
#include "FakeParameterArrayDispatch.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <cwchar>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <map>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

bool DocumentGraphSchemaSmoke();
bool DocumentGraphPropertyRegistrySmoke(const wchar_t* scenario);
bool DocumentGraphCodecStoreSmoke();
bool DocumentGraphCaptureSmoke();
bool DocumentGraphCaptureMismatchDiagnosticsSmoke();
bool DocumentGraphCaptureIntegrationSmoke();
bool DocumentGraphCaptureNegativeSmoke();
bool DocumentGraphControlsSmoke();
bool DocumentGraphEffectivePropertiesSmoke();
bool DocumentGraphImagesSmoke();
bool DocumentGraphIdentitySmoke();
bool DocumentGraphLayoutSmoke();
bool DocumentGraphProtocolSmoke();
bool DocumentGraphPatchValidateProtocolSmoke();
bool EmitDocumentGraphProtocolGolden(const wchar_t* const outputRoot);
bool EmitDocumentGraphProtocolLarge(std::uint64_t fieldBytes);
bool DocumentGraphReadCapabilitySmoke();
bool DocumentGraphContinuationSmoke();
bool DocumentGraphQuerySmoke();
bool DocumentGraphStoriesSmoke();
bool DocumentGraphTablesSmoke();
bool DocumentGraphTextSmoke();
bool OfficialApiCapabilitySmoke();
bool TextPatchReadbackSmoke();
bool TextPatchProtocolSmoke();
bool TableReaderPerformanceSmoke();
bool ReferenceClosurePerformanceSmoke();
int RunNativeStructureFixtures(
    const wchar_t* fixtureRoot,
    const wchar_t* receiptRoot);

namespace {

constexpr char kOnInitialLoad[] = "{B91A2981-A001-44a9-933F-5BF70A747967}";
constexpr char kOnLoad[] = "{3E4DC866-051C-4989-820E-DADC1E6264B9}";
constexpr char kBootstrapAction[] = "{CFB0F99F-3589-4A85-9D8B-2D6BCE5B35D1}";
constexpr IID kDispatchEventIid = {
    0xF04A09A0,
    0xF319,
    0x40D5,
    {0x88, 0xCA, 0xCA, 0x43, 0x9C, 0xC6, 0x82, 0x0C},
};

std::wstring FileSha256(const std::wstring& path) {
    HANDLE file = CreateFileW(
        path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE |
        FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) return {};
    HCRYPTPROV provider = 0;
    HCRYPTHASH hash = 0;
    if (!CryptAcquireContextW(
            &provider, nullptr, nullptr, PROV_RSA_AES,
            CRYPT_VERIFYCONTEXT | CRYPT_SILENT) ||
        !CryptCreateHash(provider, CALG_SHA_256, 0, 0, &hash)) {
        if (provider != 0) CryptReleaseContext(provider, 0);
        CloseHandle(file);
        return {};
    }
    std::array<std::uint8_t, 1U << 16> buffer{};
    bool ok = true;
    for (;;) {
        DWORD count = 0;
        if (!ReadFile(file, buffer.data(), static_cast<DWORD>(buffer.size()),
                      &count, nullptr)) {
            ok = false;
            break;
        }
        if (count == 0) break;
        if (!CryptHashData(hash, buffer.data(), count, 0)) {
            ok = false;
            break;
        }
    }
    std::array<std::uint8_t, 32> digest{};
    DWORD digestBytes = static_cast<DWORD>(digest.size());
    ok = ok && CryptGetHashParam(
        hash, HP_HASHVAL, digest.data(), &digestBytes, 0) &&
        digestBytes == digest.size();
    CryptDestroyHash(hash);
    CryptReleaseContext(provider, 0);
    CloseHandle(file);
    if (!ok) return {};
    static constexpr wchar_t digits[] = L"0123456789abcdef";
    std::wstring encoded;
    encoded.reserve(digest.size() * 2);
    for (const std::uint8_t value : digest) {
        encoded.push_back(digits[value >> 4]);
        encoded.push_back(digits[value & 0x0f]);
    }
    return encoded;
}

std::string Utf8(const std::wstring_view value) {
    if (value.empty()) return {};
    const int byteCount = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (byteCount <= 0) return {};
    std::string bytes(static_cast<std::size_t>(byteCount), '\0');
    if (WideCharToMultiByte(
            CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
            static_cast<int>(value.size()), bytes.data(), byteCount,
            nullptr, nullptr) != byteCount) return {};
    return bytes;
}

std::wstring Sha256Bytes(const std::string_view bytes) {
    HCRYPTPROV provider = 0;
    HCRYPTHASH hash = 0;
    if (!CryptAcquireContextW(
            &provider, nullptr, nullptr, PROV_RSA_AES,
            CRYPT_VERIFYCONTEXT | CRYPT_SILENT) ||
        !CryptCreateHash(provider, CALG_SHA_256, 0, 0, &hash)) {
        if (provider != 0) CryptReleaseContext(provider, 0);
        return {};
    }
    const bool hashed = bytes.size() <= UINT32_MAX && CryptHashData(
        hash, reinterpret_cast<const BYTE*>(bytes.data()),
        static_cast<DWORD>(bytes.size()), 0) != FALSE;
    std::array<std::uint8_t, 32> digest{};
    DWORD digestBytes = static_cast<DWORD>(digest.size());
    const bool ok = hashed && CryptGetHashParam(
        hash, HP_HASHVAL, digest.data(), &digestBytes, 0) != FALSE &&
        digestBytes == digest.size();
    CryptDestroyHash(hash);
    CryptReleaseContext(provider, 0);
    if (!ok) return {};
    static constexpr wchar_t digits[] = L"0123456789abcdef";
    std::wstring encoded;
    encoded.reserve(64);
    for (const std::uint8_t value : digest) {
        encoded.push_back(digits[value >> 4]);
        encoded.push_back(digits[value & 0x0f]);
    }
    return encoded;
}

std::wstring CanonicalArgvSha256(
    const int argumentCount, wchar_t** const arguments) {
    std::string canonical;
    for (int index = 0; index < argumentCount; ++index) {
        const std::string argument = Utf8(arguments[index]);
        const std::uint64_t size = argument.size();
        for (unsigned int shift = 0; shift != 64; shift += 8)
            canonical.push_back(static_cast<char>((size >> shift) & 0xff));
        canonical += argument;
    }
    return Sha256Bytes(canonical);
}

std::uint64_t ProcessStartFileTime() noexcept {
    FILETIME created{}, exited{}, kernel{}, user{};
    if (!GetProcessTimes(
            GetCurrentProcess(), &created, &exited, &kernel, &user)) return 0;
    ULARGE_INTEGER value{};
    value.LowPart = created.dwLowDateTime;
    value.HighPart = created.dwHighDateTime;
    return value.QuadPart;
}

bool IsLowerHex64(const wchar_t* const value) noexcept {
    if (value == nullptr || std::wcslen(value) != 64) return false;
    return std::all_of(value, value + 64, [](const wchar_t current) {
        return (current >= L'0' && current <= L'9') ||
            (current >= L'a' && current <= L'f');
    });
}

std::wstring EncodeUtf8Base64(const std::wstring& value) {
    const int byteCount = WideCharToMultiByte(
        CP_UTF8,
        WC_ERR_INVALID_CHARS,
        value.data(),
        static_cast<int>(value.size()),
        nullptr,
        0,
        nullptr,
        nullptr);
    if (byteCount <= 0) {
        return L"";
    }
    std::vector<unsigned char> bytes(static_cast<size_t>(byteCount));
    if (WideCharToMultiByte(
            CP_UTF8,
            WC_ERR_INVALID_CHARS,
            value.data(),
            static_cast<int>(value.size()),
            reinterpret_cast<char*>(bytes.data()),
            byteCount,
            nullptr,
            nullptr) != byteCount) {
        return L"";
    }
    constexpr wchar_t alphabet[] =
        L"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::wstring encoded;
    encoded.reserve(((bytes.size() + 2) / 3) * 4);
    for (size_t index = 0; index < bytes.size(); index += 3) {
        const size_t remaining = bytes.size() - index;
        const std::uint32_t triple =
            static_cast<std::uint32_t>(bytes[index]) << 16 |
            (remaining > 1
                ? static_cast<std::uint32_t>(bytes[index + 1]) << 8
                : 0U) |
            (remaining > 2
                ? static_cast<std::uint32_t>(bytes[index + 2])
                : 0U);
        encoded.push_back(alphabet[(triple >> 18) & 0x3f]);
        encoded.push_back(alphabet[(triple >> 12) & 0x3f]);
        encoded.push_back(remaining > 1 ? alphabet[(triple >> 6) & 0x3f] : L'=');
        encoded.push_back(remaining > 2 ? alphabet[triple & 0x3f] : L'=');
    }
    return encoded;
}

bool WriteCheckpointFixture(
    const std::filesystem::path& path,
    const std::string& encodedBlock,
    const std::string& contentSignature) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    constexpr char legacyMagic[] = "GSG_HWP_ENCODED_BLOCK_V1\n";
    constexpr char signedMagic[] = "GSG_HWP_ENCODED_BLOCK_V2\n";
    if (contentSignature.empty()) {
        output.write(
            legacyMagic,
            static_cast<std::streamsize>(sizeof(legacyMagic) - 1));
    } else {
        output.write(
            signedMagic,
            static_cast<std::streamsize>(sizeof(signedMagic) - 1));
        output.write(
            contentSignature.data(),
            static_cast<std::streamsize>(contentSignature.size()));
        output.put('\n');
    }
    output.write(
        encodedBlock.data(),
        static_cast<std::streamsize>(encodedBlock.size()));
    output.close();
    return output.good();
}

// The inverse of EncodeUtf8Base64, and the reason it exists: a response carries
// its message base64-encoded, so searching the raw response text for a phrase
// the bridge reported can never match. A check written that way does not fail
// loudly -- it silently reads a working feature as broken, which is exactly what
// happened to the rollback check below.
std::wstring DecodeUtf8Base64(const std::wstring& value) {
    const auto sextet = [](const wchar_t character) -> int {
        if (character >= L'A' && character <= L'Z') {
            return character - L'A';
        }
        if (character >= L'a' && character <= L'z') {
            return character - L'a' + 26;
        }
        if (character >= L'0' && character <= L'9') {
            return character - L'0' + 52;
        }
        if (character == L'+') {
            return 62;
        }
        if (character == L'/') {
            return 63;
        }
        return -1;
    };
    std::vector<unsigned char> bytes;
    std::uint32_t accumulator = 0;
    int bits = 0;
    for (const wchar_t character : value) {
        if (character == L'=') {
            break;
        }
        const int decoded = sextet(character);
        if (decoded < 0) {
            return L"";
        }
        accumulator = (accumulator << 6) | static_cast<std::uint32_t>(decoded);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            bytes.push_back(
                static_cast<unsigned char>((accumulator >> bits) & 0xFFU));
        }
    }
    if (bytes.empty()) {
        return L"";
    }
    const int characterCount = MultiByteToWideChar(
        CP_UTF8,
        MB_ERR_INVALID_CHARS,
        reinterpret_cast<const char*>(bytes.data()),
        static_cast<int>(bytes.size()),
        nullptr,
        0);
    if (characterCount <= 0) {
        return L"";
    }
    std::wstring decoded(static_cast<size_t>(characterCount), L'\0');
    if (MultiByteToWideChar(
            CP_UTF8,
            MB_ERR_INVALID_CHARS,
            reinterpret_cast<const char*>(bytes.data()),
            static_cast<int>(bytes.size()),
            decoded.data(),
            characterCount) != characterCount) {
        return L"";
    }
    return decoded;
}

// One tab-separated field of a response, decoded. Out of range reads as empty.
std::wstring DecodedResponseField(const std::wstring& response, const size_t index) {
    size_t start = 0;
    for (size_t field = 0; field <= index; ++field) {
        const size_t end = response.find(L'\t', start);
        if (field == index) {
            return DecodeUtf8Base64(
                end == std::wstring::npos
                    ? response.substr(start)
                    : response.substr(start, end - start));
        }
        if (end == std::wstring::npos) {
            return L"";
        }
        start = end + 1;
    }
    return L"";
}

// Call results, decoded to one "method=value" per line.
//
// They ride inside one base64 field, and every entry base64-encodes its method
// name and its string value again. Searching the outer field for a word can
// therefore never match, which is exactly how a working probe read as broken --
// the same mistake that read a working rollback message as missing two rounds
// ago. Decoding is the only way to assert on what the bridge actually said.
std::wstring DecodedCallResults(const std::wstring& response) {
    const std::wstring block = DecodedResponseField(response, 8);
    std::wstring text;
    size_t start = 0;
    while (start <= block.size()) {
        const size_t end = block.find(L'\n', start);
        const std::wstring line = end == std::wstring::npos
            ? block.substr(start)
            : block.substr(start, end - start);
        if (!line.empty()) {
            std::vector<std::wstring> parts;
            size_t field = 0;
            for (;;) {
                const size_t stop = line.find(L'\t', field);
                parts.push_back(
                    stop == std::wstring::npos
                        ? line.substr(field)
                        : line.substr(field, stop - field));
                if (stop == std::wstring::npos) {
                    break;
                }
                field = stop + 1;
            }
            if (parts.size() >= 2) {
                text += DecodeUtf8Base64(parts[1]);
                text += L'=';
                if (parts.size() >= 3) {
                    // Only a string value is encoded again; a number is plain.
                    text += parts[0] == L"S" ? DecodeUtf8Base64(parts[2]) : parts[2];
                }
                text += L'\n';
            }
        }
        if (end == std::wstring::npos) {
            break;
        }
        start = end + 1;
    }
    return text;
}

std::vector<std::filesystem::path> CheckpointRollbackCopies(
    const std::filesystem::path& checkpoint) {
    std::vector<std::filesystem::path> copies;
    const std::wstring prefix = checkpoint.filename().wstring() + L".";
    std::error_code error;
    for (std::filesystem::directory_iterator entry(
             checkpoint.parent_path(),
             error);
         !error && entry != std::filesystem::directory_iterator();
         entry.increment(error)) {
        const std::wstring name = entry->path().filename().wstring();
        constexpr wchar_t suffix[] = L".rollback";
        if (name.rfind(prefix, 0) == 0 &&
            name.size() >= std::size(suffix) - 1 &&
            name.compare(
                name.size() - (std::size(suffix) - 1),
                std::size(suffix) - 1,
                suffix) == 0) {
            copies.push_back(entry->path());
        }
    }
    return copies;
}

// A restore that fails after both attempts keeps a uniquely named copy of the
// document it found. Every case that ends that way clears only this checkpoint's
// copies, so a later case never mistakes them for its own evidence.
void RemoveCheckpointRollbackCopy(const std::filesystem::path& checkpoint) {
    std::error_code ignored;
    for (const std::filesystem::path& rollback :
         CheckpointRollbackCopies(checkpoint)) {
        static_cast<void>(std::filesystem::remove(rollback, ignored));
        static_cast<void>(std::filesystem::remove(
            std::filesystem::path(rollback.wstring() + L".gsgmeta"),
            ignored));
    }
}

bool RemoveAbandonedCheckpointSmokeFiles() {
    constexpr std::wstring_view prefixes[]{
        L"HancomLiveBridgeCheckpointDocumentSmoke-",
        L"HancomLiveBridgeUserDocumentSmoke-",
        L"HancomLiveBridgeBlockProbeSmoke-",
        L"HancomLiveBridgeCheckpointSmoke-",
        L"HancomLiveBridgeLegacyCheckpointSmoke-",
    };
    const auto hasOwnedPrefix = [&](const std::wstring& name) {
        return std::any_of(
            std::begin(prefixes),
            std::end(prefixes),
            [&](const std::wstring_view prefix) {
                return name.size() >= prefix.size() &&
                    name.compare(0, prefix.size(), prefix) == 0;
            });
    };
    std::error_code error;
    const std::filesystem::path root = std::filesystem::temp_directory_path(error);
    if (error) {
        return false;
    }
    for (std::filesystem::directory_iterator entries(root, error), end;
         !error && entries != end;
         entries.increment(error)) {
        const std::wstring name = entries->path().filename().wstring();
        const bool owned = hasOwnedPrefix(name);
        if (!owned) {
            continue;
        }
        std::filesystem::permissions(
            entries->path(),
            std::filesystem::perms::owner_all,
            std::filesystem::perm_options::add,
            error);
        if (error || !std::filesystem::remove(entries->path(), error) || error) {
            return false;
        }
    }
    if (error) {
        return false;
    }
    for (const std::filesystem::directory_entry& entry :
         std::filesystem::directory_iterator(root, error)) {
        const std::wstring name = entry.path().filename().wstring();
        if (hasOwnedPrefix(name)) {
            return false;
        }
    }
    return !error;
}

// A checkpoint written in the document-file layout: it carries none of our
// magics, which is exactly how the bridge tells the two layouts apart.
bool WriteDocumentCheckpointFixture(const std::filesystem::path& path) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    constexpr char body[] = "HWP DOCUMENT FILE CHECKPOINT FIXTURE";
    output.write(body, static_cast<std::streamsize>(sizeof(body) - 1));
    output.close();
    return output.good();
}

std::string ReadFileBytes(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

// The content signature this fake engine produces for a restored document.
//
// It has to be here because the restore refuses to delete anything inside the
// checkpoint's own area unless a signature exists to catch a wrong deletion --
// so a checkpoint without one cannot exercise that path at all. Every success
// case below is therefore a real signature comparison and not a page count.
//
// The value is measured, not invented: run any of these cases with the wrong
// one and the refusal prints "expected <this> but read <that>".
// Note the doubled "SIG": the sidecar line is the key "SIG " followed by the
// signature string, and a signature string starts with "SIG " of its own. The
// writer in ActionLifecycle.cpp does the same, and a fixture that wrote only one
// of them silently produced a signature nothing could ever match.
constexpr char kCheckpointDocumentSignature[] =
    "SIG 4 1 10845636922292024283 10045998281374587588 19 0 2 "
    "10610860378690137251 146";
constexpr LONG kCheckpointDocumentExpectedPages = 4;
constexpr char kCheckpointEmptyCaptureWithTextSignature[] =
    "SIG 4 1 10845636922292024283 1469598103934665603 0 1 2 "
    "10610860378690137251 146";
constexpr char kCheckpointPictureOnlySignature[] =
    "SIG 4 1 10845636922292024283 1469598103934665603 0 1 1 "
    "10610860378690137251 146";

// The sidecar a document-file checkpoint carries its format and signature in.
bool WriteDocumentCheckpointMeta(
    const std::filesystem::path& path,
    const std::string& signature,
    const std::string& originSignature = "",
    const LONG originPageCount = -1) {
    std::ofstream output(
        std::filesystem::path(path.wstring() + L".gsgmeta"),
        std::ios::binary | std::ios::trunc);
    std::string text = "GSG_HWP_DOCUMENT_FILE_V1\nFMT HWP\n";
    if (!signature.empty()) {
        text += "SIG " + signature + "\n";
    }
    if (!originSignature.empty() && originPageCount > 0) {
        text += "HISTORY P1_SINGLE_TEXT_UNDO\n";
        text += "ORIGIN " + originSignature + "\n";
        text += "ORIGIN_PAGES " + std::to_string(originPageCount) + "\n";
    }
    output.write(text.data(), static_cast<std::streamsize>(text.size()));
    output.close();
    return output.good();
}

hancom::inspection::CellTopologyCell IndexedTopologyCell(
    const wchar_t* const address,
    const long rowSpan = 1,
    const long columnSpan = 1) {
    hancom::inspection::CellTopologyCell cell;
    cell.address = address;
    cell.rowSpan = rowSpan;
    cell.columnSpan = columnSpan;
    return cell;
}

bool CellTopologyOwnerIndexSmoke() {
    hancom::inspection::CellTopology topology;
    std::wstring error;
    if (!topology.Build(
            {
                IndexedTopologyCell(L"A1", 1, 2),
                IndexedTopologyCell(L"A2"),
                IndexedTopologyCell(L"B2"),
            },
            &error)) {
        return false;
    }
    const auto addressAt = [](const hancom::inspection::CellTopology& value,
                              const long row,
                              const long column) {
        const hancom::inspection::CellTopologyCell* const owner =
            value.OwnerAt(row, column);
        return owner == nullptr ? std::wstring{} : owner->address;
    };
    if (addressAt(topology, 1, 1) != L"A1" ||
        addressAt(topology, 1, 2) != L"A1" ||
        addressAt(topology, 2, 1) != L"A2" ||
        addressAt(topology, 2, 2) != L"B2" ||
        topology.OwnerAt(0, 1) != nullptr ||
        topology.OwnerAt(3, 1) != nullptr) {
        return false;
    }

    const hancom::inspection::CellTopology copied = topology;
    topology.Clear();
    if (topology.OwnerAt(1, 1) != nullptr ||
        addressAt(copied, 1, 2) != L"A1") {
        return false;
    }
    return topology.Build(
               {
                   IndexedTopologyCell(L"A1"),
                   IndexedTopologyCell(L"B1"),
               },
               &error) &&
        addressAt(topology, 1, 1) == L"A1" &&
        addressAt(topology, 1, 2) == L"B1" &&
        topology.OwnerAt(2, 1) == nullptr;
}

// Runs the real sampler on real topology output. The wanted positions and the
// coordinates CellTopology::Build writes have to count from the same base; when
// they did not, this returned nothing for every table and cell appearance was
// silently absent from every inspection response.
bool TableCellFormatSamplingSmoke() {
    const auto addressesOf = [](const hancom::inspection::CellTopology& topology) {
        std::vector<std::wstring> addresses;
        for (const hancom::inspection::CellTopologyCell* const cell :
             hancom::inspection::SampleCells(topology.Cells())) {
            addresses.push_back(cell->address);
        }
        return addresses;
    };

    hancom::inspection::CellTopology grid;
    std::wstring error;
    if (!grid.Build(
            {
                IndexedTopologyCell(L"A1"), IndexedTopologyCell(L"B1"),
                IndexedTopologyCell(L"C1"), IndexedTopologyCell(L"D1"),
                IndexedTopologyCell(L"A2"), IndexedTopologyCell(L"B2"),
                IndexedTopologyCell(L"C2"), IndexedTopologyCell(L"D2"),
                IndexedTopologyCell(L"A3"), IndexedTopologyCell(L"B3"),
                IndexedTopologyCell(L"C3"), IndexedTopologyCell(L"D3"),
            },
            &error)) {
        return false;
    }
    // The sampler crosses first/middle/last columns with first/early/middle/
    // last rows. In a 3x4 table early and middle are both row 2, yielding
    // nine distinct physical owners in canonical wanted-position order.
    if (addressesOf(grid) != std::vector<std::wstring>{
            L"A1", L"B1", L"D1", L"A2", L"B2", L"D2",
            L"A3", L"B3", L"D3"}) {
        return false;
    }

    // A merged first row answers through its owner, so the first two wanted
    // positions collapse into one record instead of dropping out.
    hancom::inspection::CellTopology merged;
    if (!merged.Build(
            {
                IndexedTopologyCell(L"A1", 1, 4),
                IndexedTopologyCell(L"A2"), IndexedTopologyCell(L"B2"),
                IndexedTopologyCell(L"C2"), IndexedTopologyCell(L"D2"),
                IndexedTopologyCell(L"A3"), IndexedTopologyCell(L"B3"),
                IndexedTopologyCell(L"C3"), IndexedTopologyCell(L"D3"),
            },
            &error)) {
        return false;
    }
    if (addressesOf(merged) != std::vector<std::wstring>{
            L"A1", L"A2", L"B2", L"D2", L"A3", L"B3", L"D3"}) {
        return false;
    }

    // One row, one column: every wanted position is the same cell.
    hancom::inspection::CellTopology single;
    if (!single.Build({IndexedTopologyCell(L"A1")}, &error)) {
        return false;
    }
    if (addressesOf(single) != std::vector<std::wstring>{L"A1"}) {
        return false;
    }

    // Nothing inspected, nothing sampled -- and no crash reaching for row 0.
    return hancom::inspection::SampleCells({}).empty();
}

bool OfficialApiVirtualPropertyGetSmoke() {
    auto* const target = new FakeVirtualPropertyGetDispatch();
    CComPtr<IUnknown> interfaceObject;
    size_t slot = 0;
    const HRESULT status =
        hancom::official_api::ResolveVirtualPropertyGet(
            target,
            L"HAction",
            VT_DISPATCH,
            interfaceObject,
            &slot);
    static_cast<void>(target->Release());
    if (FAILED(status)) {
        std::wcerr << L"ResolveVirtualPropertyGet failed: 0x"
                   << std::hex << static_cast<unsigned long>(status)
                   << std::dec << L'\n';
    }
    return status == S_OK && interfaceObject != nullptr && slot == 9U;
}

bool ParagraphTextNormalizationSmoke() {
    return hancom::text::SameParagraphText(
               L"first\nsecond",
               L"first\r\nsecond") &&
        hancom::text::SameParagraphText(
               L"first\nsecond",
               L"first\rsecond") &&
        !hancom::text::SameParagraphText(
            L"first\nsecond",
            L"first\nchanged");
}

struct IHncUserActionModule {
    virtual LPCSTR EnumAction(int iterator) = 0;
    virtual BOOL GetActionImage(
        LPCSTR action,
        UINT state,
        HBITMAP* bitmap,
        int* imageIndex) = 0;
    virtual BOOL UpdateUI(LPCSTR action, LPDISPATCH object, UINT* state) = 0;
    virtual int DoAction(LPCSTR action, LPDISPATCH object) = 0;
};

// Since 0.5.173 the enumeration is the three lifecycle actions followed by the
// 32 ribbon slot AIDs. Checking the slots by shape rather than by 32 literals
// keeps this smoke from becoming a second copy of the pool that can drift.
constexpr char kSlotActionPrefix[] = "{2C445309-901C-49B2-BED1-D2D9CFFB";
constexpr int kSlotActionPrefixLength = 33;
constexpr int kSlotActionLength = 38;
constexpr int kLifecycleActionCount = 3;

bool SlotActionMatches(const char* const action, const int index) noexcept {
    if (action == nullptr ||
        std::strlen(action) != static_cast<std::size_t>(kSlotActionLength) ||
        std::strncmp(action, kSlotActionPrefix, kSlotActionPrefixLength) != 0) {
        return false;
    }
    char expected[6] = {};
    if (sprintf_s(expected, "%04X}", index) < 0) {
        return false;
    }
    return std::strcmp(action + kSlotActionPrefixLength, expected) == 0;
}

bool UserActionEnumerationMatches(IHncUserActionModule* const module) noexcept {
    if (module == nullptr) {
        return false;
    }
    const char* const initial = module->EnumAction(0);
    const char* const load = module->EnumAction(1);
    const char* const bootstrap = module->EnumAction(2);
    if (initial == nullptr || load == nullptr || bootstrap == nullptr ||
        std::strcmp(initial, kOnInitialLoad) != 0 ||
        std::strcmp(load, kOnLoad) != 0 ||
        std::strcmp(bootstrap, kBootstrapAction) != 0) {
        return false;
    }
    for (int index = 0; index < bridge_status::kSlotCount; ++index) {
        if (!SlotActionMatches(
                module->EnumAction(kLifecycleActionCount + index), index)) {
            return false;
        }
    }
    return module->EnumAction(
               kLifecycleActionCount + bridge_status::kSlotCount) == nullptr;
}

class FakeListParaPosDispatch final : public IDispatch {
public:
    void SetPosition(
        const LONG list,
        const LONG paragraph,
        const LONG character) noexcept {
        list_ = list;
        paragraph_ = paragraph;
        character_ = character;
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(
        REFIID interfaceId,
        void** const object) override {
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

    ULONG STDMETHODCALLTYPE AddRef() override {
        return static_cast<ULONG>(InterlockedIncrement(&references_));
    }

    ULONG STDMETHODCALLTYPE Release() override {
        const LONG remaining = InterlockedDecrement(&references_);
        if (remaining == 0) {
            delete this;
        }
        return static_cast<ULONG>(remaining);
    }

    HRESULT STDMETHODCALLTYPE GetTypeInfoCount(UINT* const count) override {
        if (count == nullptr) {
            return E_POINTER;
        }
        *count = 0;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetTypeInfo(UINT, LCID, ITypeInfo**) override {
        return E_NOTIMPL;
    }

    HRESULT STDMETHODCALLTYPE GetIDsOfNames(
        REFIID,
        LPOLESTR* const names,
        const UINT count,
        LCID,
        DISPID* const members) override {
        if (names == nullptr || members == nullptr || count != 1 ||
            std::wstring(names[0]) != L"Item") {
            return DISP_E_UNKNOWNNAME;
        }
        members[0] = 1;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE Invoke(
        const DISPID member,
        REFIID,
        LCID,
        const WORD flags,
        DISPPARAMS* const parameters,
        VARIANT* const result,
        EXCEPINFO*,
        UINT*) override {
        if (member != 1 || (flags & DISPATCH_METHOD) == 0 || result == nullptr ||
            parameters == nullptr || parameters->cArgs != 1 ||
            parameters->rgvarg[0].vt != VT_BSTR) {
            return DISP_E_MEMBERNOTFOUND;
        }
        const std::wstring name(parameters->rgvarg[0].bstrVal);
        VariantInit(result);
        result->vt = VT_I4;
        result->lVal = name == L"List"
            ? list_
            : name == L"Para"
            ? paragraph_
            : name == L"Pos"
            ? character_
            : 0;
        return name == L"List" || name == L"Para" || name == L"Pos"
            ? S_OK
            : DISP_E_MEMBERNOTFOUND;
    }

private:
    ~FakeListParaPosDispatch() = default;

    volatile LONG references_ = 1;
    LONG list_ = 0;
    LONG paragraph_ = 0;
    LONG character_ = 0;
};

class FakeDispatch final
    : public IDispatch,
      public IConnectionPointContainer,
      public IConnectionPoint {
public:
    explicit FakeDispatch(
        const LONG windowHandle = 4242,
        const LONG documentId = 17)
        : windowHandle_(windowHandle),
          documentId_(documentId) {}

    LONG ReferenceCount() noexcept {
        return InterlockedCompareExchange(&references_, 0, 0);
    }

    enum Member : DISPID {
        XHwpDocuments = 1,
        ActiveDocument = 2,
        DocumentId = 3,
        FullName = 4,
        HAction = 5,
        CellShape = 6,
        ParentCtrl = 7,
        CurSelectedCtrl = 8,
        SelectCtrl = 9,
        GetCtrlInstId = 10,
        Run = 11,
        GetAnchorPos = 12,
        Item = 13,
        SetPos = 14,
        HParameterSet = 15,
        HStyle = 16,
        HParaShape = 17,
        HSet = 18,
        GetDefault = 19,
        Execute = 20,
        Apply = 21,
        AlignType = 22,
        LineSpacing = 23,
        LeftMargin = 24,
        RightMargin = 25,
        Indentation = 26,
        PrevSpacing = 27,
        NextSpacing = 28,
        SetMessageBoxMode = 29,
        SelectText = 30,
        GetTextFile = 31,
        SetTextFile = 32,
        InitScan = 33,
        GetText = 34,
        ReleaseScan = 35,
        HInsertText = 36,
        Text = 37,
        ReturnBoolean = 38,
        ReturnInteger = 39,
        ReturnText = 40,
        ReturnVoid = 41,
        CreateAction = 42,
        CreateSet = 43,
        PageCount = 44,
        IsModified = 45,
        SetItem = 46,
        ItemExist = 47,
        XHwpMessageBox = 48,
        Application = 49,
        SetCurMetatagName = 50,
        Password = 51,
        SetID = 52,
        GetSelectedPosBySet = 53,
        HeadCtrl = 54,
        CtrlID = 55,
        Next = 56,
        GetPos = 57,
        Save = 58,
        Clear = 59,
        Open = 60,
        DeleteCtrl = 61,
        HArrayFixture = 62,
        SelectionMode = 63,
        XHwpWindows = 64,
        ActiveWindow = 65,
        WindowHandle = 66,
        FindItem = 67,
        SetActiveDocument = 68,
        SaveAs = 69,
        HCharShape = 70,
        XHwpDocumentInfo = 71,
        CurrentPage = 72,
        Properties = 73,
        KeyIndicator = 74,
        FaceNameHangul = 75,
        CharacterHeight = 76,
        Bold = 77,
        TextColor = 78,
        RatioHangul = 79,
        SpacingHangul = 80,
        LineSpacingType = 81,
        GenericParameter = 82,
        LastCtrl = 83,
        Prev = 84,
        UserDesc = 85,
        GetPageText = 86,
        HwpLineType = 87,
        HwpLineWidth = 88,
        MutateTableTopology = 89,
        HInsertFile = 90,
        PageBreakBefore = 91,
        GetHeadingString = 92,
        HFindReplace = 93,
        FindString = 94,
        FindDirection = 95,
        FindMatchCase = 96,
        FindIgnoreMessage = 97,
        MovePos = 98,
    };

    bool CompletedLifecycleSequence() const noexcept {
        return saveCalledWithTrue_ && clearCalledWithDiscard_ && openCalledWithArguments_ &&
            lifecycleCalls_.size() == 3 && lifecycleCalls_[0] == L"Save" &&
            lifecycleCalls_[1] == L"Clear" && lifecycleCalls_[2] == L"Open" &&
            activeDocumentFullNameReads_ == 2;
    }

    bool StoppedLifecycleAfterSave() const noexcept {
        return saveCalledWithTrue_ && !clearCalledWithDiscard_ && !openCalledWithArguments_ &&
            lifecycleCalls_.size() == 1 && lifecycleCalls_[0] == L"Save";
    }

    bool StoppedLifecycleBeforeDestructiveReopen() const noexcept {
        return StoppedLifecycleAfterSave() &&
            !lifecycleRecovered_ && !saveAsCalledWithArguments_ &&
            documentOpen_ && !modified_ &&
            fullName_ == lifecyclePath_;
    }

    bool RecoveredLifecycleSession() const noexcept {
        return lifecycleRecovered_ && documentOpen_ && !modified_ &&
            fullName_ == lifecyclePath_;
    }

    bool UsedExpectedLifecycleRecoveryFormat() const noexcept {
        return saveAsCalledWithArguments_;
    }

    bool RecoveredLifecycleFingerprintMismatch() const noexcept {
        return lifecycleMismatchAfterOpen_ && !lifecycleMismatchActive_ &&
            clearCallCount_ == 1 &&
            RecoveredLifecycleSession();
    }

    void ResetLifecycleExpectations() {
        lifecyclePath_ = L"C:\\\uD55C\uAE00\\x.hwp";
        lifecycleReopenedPath_ = L"C:\\\uD55C\uAE00\\X.HWP";
        lifecycleFormat_ = L"HWP";
        saveAsCalledWithArguments_ = false;
        lifecycleRecoveryBlockCaptured_ = true;
        lifecycleMismatchAfterOpen_ = false;
        lifecycleMismatchActive_ = false;
        clearCallCount_ = 0;
    }

    void PrepareLifecycleSuccess() {
        ResetLifecycleExpectations();
        lifecycleCalls_.clear();
        saveCalledWithTrue_ = false;
        clearCalledWithDiscard_ = false;
        openCalledWithArguments_ = false;
        lifecycleRecovered_ = false;
        saveReturnsTrue_ = true;
        saveClearsModified_ = true;
        openReturnsTrue_ = true;
        modified_ = true;
        documentOpen_ = true;
        fullName_ = lifecyclePath_;
        requireActiveDocumentFullName_ = true;
        activeDocumentFullNameReady_ = false;
        activeDocumentFullNameReads_ = 0;
        unstableSerializationMetadataFixture_ = false;
        unstableCaretMetadataFixture_ = true;
        diagnosticSectionMismatchFixture_ = false;
        diagnosticSectionBeforeHwpml_.clear();
        diagnosticSectionAfterHwpml_.clear();
        serializationReads_ = 0;
    }

    void PrepareLifecycleHwpxSuccess() {
        PrepareLifecycleSuccess();
        lifecyclePath_ = L"C:\\\uD55C\uAE00\\x.HwPx";
        lifecycleReopenedPath_ = L"C:\\\uD55C\uAE00\\X.HWPX";
        lifecycleFormat_ = L"HWPX";
        fullName_ = lifecyclePath_;
    }

    void PrepareLifecycleHwpxOpenFailure() {
        PrepareLifecycleHwpxSuccess();
        openReturnsTrue_ = false;
    }

    void PrepareLifecycleRecoveryCaptureFailure() {
        PrepareLifecycleSuccess();
        lifecycleRecoveryBlockCaptured_ = false;
    }

    void PrepareLifecycleOpenSuccessFingerprintMismatch() {
        PrepareLifecycleSuccess();
        lifecycleMismatchAfterOpen_ = true;
    }

    void PrepareLifecycleHwpxOpenSuccessFingerprintMismatch() {
        PrepareLifecycleHwpxSuccess();
        lifecycleMismatchAfterOpen_ = true;
    }

    void PrepareSaveVerifySerializationFixture() {
        PrepareLifecycleSuccess();
        unstableSerializationMetadataFixture_ = true;
        unstableCaretMetadataFixture_ = false;
        fullName_ = kSaveEvidenceFile;
        const HANDLE file = CreateFileW(
            kSaveEvidenceFile,
            GENERIC_WRITE,
            FILE_SHARE_READ,
            nullptr,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            nullptr);
        if (file != INVALID_HANDLE_VALUE) {
            constexpr char content[] = "HWP";
            DWORD written = 0;
            static_cast<void>(WriteFile(
                file,
                content,
                static_cast<DWORD>(sizeof(content) - 1),
                &written,
                nullptr));
            CloseHandle(file);
        }
    }

    // A document too large for the engine to serialize. GetTextFile answers
    // S_OK with an empty string, having already spent the whole attempt, so
    // the count of attempts is the cost this fixture measures.
    void PrepareSaveVerifyOversizeRefusalFixture() {
        PrepareSaveVerifySerializationFixture();
        unstableSerializationMetadataFixture_ = false;
        oversizeSerializationRefusalFixture_ = true;
        hwpmlSerializationAttempts_ = 0;
    }

    // Refusing to serialize is this one fixture's business. Every later
    // fixture in the run needs the engine answering normally again, so the
    // refusal is switched off explicitly rather than left for the next
    // Prepare call to remember.
    void ClearSaveVerifyOversizeRefusalFixture() noexcept {
        oversizeSerializationRefusalFixture_ = false;
    }

    size_t HwpmlSerializationAttempts() const noexcept {
        return hwpmlSerializationAttempts_;
    }

    void PrepareSaveVerifySectionDiagnosticFixture() {
        PrepareSaveVerifySerializationFixture();
        unstableSerializationMetadataFixture_ = false;
        diagnosticSectionMismatchFixture_ = true;
        constexpr wchar_t prefix[] = L"<HWPML><BODY>";
        diagnosticSectionBeforeHwpml_ = prefix;
        diagnosticSectionBeforeHwpml_.append(8 * 1024 * 1024, L'A');
        diagnosticSectionBeforeHwpml_ += L"</BODY></HWPML>";
        diagnosticSectionAfterHwpml_ = diagnosticSectionBeforeHwpml_;
        diagnosticSectionAfterHwpml_[
            std::size(prefix) - 1 + 4 * 1024 * 1024 + 123] = L'B';
        serializationReads_ = 0;
    }

    void PrepareLifecycleSaveFailure() {
        ResetLifecycleExpectations();
        lifecycleCalls_.clear();
        saveCalledWithTrue_ = false;
        clearCalledWithDiscard_ = false;
        openCalledWithArguments_ = false;
        lifecycleRecovered_ = false;
        saveReturnsTrue_ = true;
        saveClearsModified_ = false;
        openReturnsTrue_ = true;
        modified_ = true;
        documentOpen_ = true;
        fullName_ = lifecyclePath_;
        requireActiveDocumentFullName_ = true;
        activeDocumentFullNameReady_ = false;
        activeDocumentFullNameReads_ = 0;
    }

    void PrepareLifecycleFalseSaveReturn() {
        ResetLifecycleExpectations();
        lifecycleCalls_.clear();
        saveCalledWithTrue_ = false;
        clearCalledWithDiscard_ = false;
        openCalledWithArguments_ = false;
        lifecycleRecovered_ = false;
        saveReturnsTrue_ = false;
        saveClearsModified_ = true;
        openReturnsTrue_ = true;
        modified_ = true;
        documentOpen_ = true;
        fullName_ = lifecyclePath_;
        requireActiveDocumentFullName_ = true;
        activeDocumentFullNameReady_ = false;
        activeDocumentFullNameReads_ = 0;
    }

    void PrepareLifecycleCleanNoOpSave() {
        PrepareLifecycleFalseSaveReturn();
        modified_ = false;
    }

    void PrepareLifecycleOpenFailure() {
        PrepareLifecycleSuccess();
        openReturnsTrue_ = false;
    }

    void RestoreLifecycleFixture() {
        ResetLifecycleExpectations();
        saveReturnsTrue_ = true;
        saveClearsModified_ = true;
        openReturnsTrue_ = true;
        lifecycleRecovered_ = false;
        modified_ = false;
        documentOpen_ = true;
        fullName_ = L"C:\\x.hwp";
        requireActiveDocumentFullName_ = false;
        activeDocumentFullNameReady_ = false;
        activeDocumentFullNameReads_ = 0;
        unstableSerializationMetadataFixture_ = false;
        unstableCaretMetadataFixture_ = false;
        diagnosticSectionMismatchFixture_ = false;
        diagnosticSectionBeforeHwpml_.clear();
        diagnosticSectionAfterHwpml_.clear();
        serializationReads_ = 0;
    }

    bool UsedAnchorSelectionForCopy() const noexcept {
        return copyUsedAnchorSelection_;
    }

    bool PasteUsedSourceAnchorFormat() const noexcept {
        return pasteUsedSourceAnchorFormat_;
    }

    bool UsedNativeTableBlockCopy() const noexcept {
        return nativeTableBlockCopied_;
    }

    bool UsedNativeTableBlockPaste() const noexcept {
        return nativeTableBlockPasted_;
    }

    bool UsedClipboardTableTransfer() const noexcept {
        return clipboardTableTransferUsed_;
    }

    bool RanBreakPage() const noexcept {
        return ranBreakPage_;
    }

    void PrepareSelectCtrlFrontFailure() noexcept {
        failSelectCtrlFront_ = true;
    }

    void RestoreSelectCtrlFrontFixture() noexcept {
        failSelectCtrlFront_ = false;
        ranBreakPage_ = false;
    }

    void PrepareAtomicAppendFailure(const bool failRollback) noexcept {
        atomicAppendFixture_ = true;
        failSelectCtrlFrontWithHresult_ = true;
        failAtomicTailDelete_ = failRollback;
        tailSelection_ = false;
        pageCount_ = 1;
        currentList_ = 0;
        currentParagraph_ = 0;
        currentCharacter_ = 0;
        atomicTail_.clear();
        atomicTailDeletes_ = 0;
        atomicTailMaximumOccurrences_ = 0;
        insertedText_.clear();
        ranBreakPage_ = false;
    }

    void RestoreAtomicAppendFixture() noexcept {
        atomicAppendFixture_ = false;
        failSelectCtrlFrontWithHresult_ = false;
        failAtomicTailDelete_ = false;
        tailSelection_ = false;
        pageCount_ = 1;
        atomicTail_.clear();
        insertedText_.clear();
        ranBreakPage_ = false;
    }

    bool AtomicTailRollbackRestored() const noexcept {
        return atomicTail_.empty() && pageCount_ == 1 && atomicTailDeletes_ == 2 &&
            atomicTailMaximumOccurrences_ == 1;
    }

    bool AtomicTailRollbackRestoredOnce() const noexcept {
        return atomicTail_.empty() && pageCount_ == 1 && atomicTailDeletes_ == 1 &&
            atomicTailMaximumOccurrences_ == 1;
    }

    bool AtomicTailRollbackFailureRetainedTail() const noexcept {
        return !atomicTail_.empty() && pageCount_ == 2 && atomicTailDeletes_ == 0;
    }

    void PrepareCheckpointRestoreFixture(const bool wrongTargetPageCount) {
        checkpointRestoreFixture_ = true;
        checkpointWrongTargetPageCount_ = wrongTargetPageCount;
        checkpointSetTextFileFails_ = false;
        checkpointDocumentBlock_ = L"CHECKPOINT_ROLLBACK";
        checkpointSetAttempts_ = 0;
        checkpointBlockBodyPresent_ = true;
        checkpointBlockBodySelected_ = false;
        pageCount_ = 2;
        currentList_ = 0;
        currentParagraph_ = 0;
        currentCharacter_ = 0;
        runActions_.clear();
    }

    void PrepareCheckpointRestoreFailureFixture() {
        PrepareCheckpointRestoreFixture(true);
        checkpointSetTextFileFails_ = true;
    }

    bool CheckpointRestoreSucceeded() const noexcept {
        const std::vector<std::wstring> expected{
            L"MoveDocBegin",
            L"MoveDocEnd",
            L"Delete",
            L"MoveDocBegin",
            L"MoveDocEnd",
            L"MoveDocBegin",
            L"MoveDocBegin",
        };
        return checkpointRestoreFixture_ &&
            checkpointSetAttempts_ == 1 &&
            checkpointDocumentBlock_ == L"CHECKPOINT_TARGET" &&
            pageCount_ == 4 &&
            runActions_ == expected;
    }

    bool CheckpointRollbackSucceeded() const noexcept {
        const std::vector<std::wstring> expected{
            L"MoveDocBegin",
            L"MoveDocEnd",
            L"Delete",
            L"MoveDocBegin",
            L"MoveDocEnd",
            L"MoveDocBegin",
            L"MoveDocBegin",
            L"MoveDocBegin",
            L"MoveDocEnd",
            L"Delete",
            L"MoveDocBegin",
            L"MoveDocEnd",
            L"MoveDocBegin",
            L"MoveDocBegin",
        };
        return checkpointRestoreFixture_ &&
            checkpointSetAttempts_ == 2 &&
            checkpointDocumentBlock_ == L"CHECKPOINT_ROLLBACK" &&
            pageCount_ == 2 &&
            runActions_ == expected;
    }

    bool CheckpointRestoreWasNotAttempted() const noexcept {
        return checkpointRestoreFixture_ &&
            checkpointSetAttempts_ == 0 &&
            checkpointDocumentBlock_ == L"CHECKPOINT_ROLLBACK" &&
            checkpointBlockBodyPresent_ &&
            pageCount_ == 2 &&
            runActions_.empty();
    }

    void RestoreCheckpointRestoreFixture() {
        checkpointRestoreFixture_ = false;
        checkpointWrongTargetPageCount_ = false;
        checkpointSetTextFileFails_ = false;
        checkpointDocumentBlock_.clear();
        checkpointSetAttempts_ = 0;
        checkpointBlockBodyPresent_ = false;
        checkpointBlockBodySelected_ = false;
        pageCount_ = 1;
        runActions_.clear();
    }

    // The other checkpoint layout: the file is handed to the engine's InsertFile
    // instead of an encoded block being poured in. Two things go wrong on that
    // path that the block layout never sees, and this models both separately so
    // a test can tell which one a restore actually survived.
    //
    // `residueAtFront` is which side it lands on. At the front, the paragraph
    // left by emptying retains one `secd` and the inserted document begins with a
    // second `secd` at paragraph 1. Deleting only the paragraph leaves the first
    // section control behind and therefore leaves the extra page behind too.
    // The other side is still modelled because the tail trim still handles it.
    //
    // `sectionsNeedKeeping` is the document the flattening insert cannot
    // reproduce at all: its sections do not share one page layout, so pouring
    // them into one reflows it into an extra page that no trim can take back.
    // Only the section-faithful insert restores it.
    void PrepareCheckpointDocumentFileFixture(
        const bool sectionsNeedKeeping,
        const bool residueAtFront) {
        checkpointDocumentFileFixture_ = true;
        checkpointInsertNeverMatches_ = false;
        checkpointRollbackInsertFails_ = false;
        checkpointResidueAtFront_ = residueAtFront;
        checkpointSectionsNeedKeeping_ = sectionsNeedKeeping;
        checkpointInsertKeepSections_.clear();
        checkpointResidueParagraph_ = false;
        checkpointLeadingSectionResidue_ = false;
        checkpointLeadingSectionDeletes_ = 0;
        checkpointFlattenedPenalty_ = false;
        checkpointWholeSelected_ = false;
        checkpointBodySelected_ = false;
        checkpointTailSelected_ = false;
        checkpointHeadSelected_ = false;
        checkpointContentParagraphs_ = kCheckpointContentParagraphs;
        checkpointPendingKeepSection_ = -1;
        checkpointPendingFileName_.clear();
        checkpointOpenCount_ = 0;
        checkpointClearCallCount_ = 0;
        checkpointClearFails_ = false;
        checkpointTargetPageMismatch_ = false;
        checkpointRollbackOpenFails_ = false;
        checkpointLastKeepSection_ = -1;
        checkpointHeadProbeActive_ = false;
        checkpointHeadControlIndex_ = 0;
        checkpointHeadPageBreakBefore_ = false;
        checkpointHeadSectionIsolated_ = true;
        checkpointTailAnswer_ = L"\r\n";
        checkpointHeadAnswer_ = L"\r\n";
        checkpointAbsorbsResidue_ = false;
        checkpointInsertCaretBeforeEnd_ = false;
        checkpointSingleParagraph_ = false;
        checkpointSelectionMisreports_ = false;
        checkpointHeadRangeReportsAfterInsert_ = 0;
        checkpointEngineUndoRestores_ = false;
        checkpointEngineUndoWrongStep_ = false;
        checkpointSignatureCaptureUnavailable_ = false;
        checkpointPageCountBelow_ = false;
        checkpointHeadResidueDeleted_ = false;
        checkpointTextFileEmpty_ = false;
        checkpointPictureOnly_ = false;
        checkpointPresenceScan_ = false;
        checkpointTextLossInsertAttempts_ = 0;
        checkpointDeleteLeavesTextAttempts_ = 0;
        checkpointCrossListEndAttempts_ = 0;
        checkpointBodyDeleteAttempts_ = 0;
        checkpointDocumentBlock_ = L"CHECKPOINT_ROLLBACK";
        pageCount_ = 2;
        currentList_ = 0;
        currentParagraph_ = 0;
        currentCharacter_ = 0;
        runActions_.clear();
    }

    void SetDocumentPathFixture(const std::wstring& path) {
        fullName_ = path;
        checkpointUserDocumentPath_ = path;
    }

    void SetCheckpointDirectTargetMismatch() noexcept {
        checkpointTargetPageMismatch_ = true;
    }

    void SetCheckpointClearFails() noexcept {
        checkpointClearFails_ = true;
    }

    void SetCheckpointDirectRollbackFailure() noexcept {
        checkpointTargetPageMismatch_ = true;
        checkpointRollbackOpenFails_ = true;
    }

    bool CheckpointDirectRestoreExact() const noexcept {
        return checkpointDocumentFileFixture_ && checkpointOpenCount_ == 1 &&
            checkpointDocumentBlock_ == L"CHECKPOINT_TARGET" &&
            pageCount_ == kCheckpointDocumentPages && documentOpen_ &&
            !modified_ && currentList_ == 0 && currentParagraph_ == 0 &&
            currentCharacter_ == 0 && selectionMode_ == 0 &&
            fullName_ == checkpointUserDocumentPath_ &&
            std::find(runActions_.begin(), runActions_.end(), L"MoveDocBegin") !=
                runActions_.end() && checkpointInsertKeepSections_.empty();
    }

    bool CheckpointDirectRollbackExact() const noexcept {
        return checkpointDocumentFileFixture_ && checkpointOpenCount_ == 2 &&
            checkpointDocumentBlock_ == L"CHECKPOINT_ROLLBACK" &&
            pageCount_ == 2 && documentOpen_ && !modified_ &&
            currentList_ == 0 && currentParagraph_ == 0 &&
            currentCharacter_ == 0 && selectionMode_ == 0 &&
            fullName_ == checkpointUserDocumentPath_ &&
            checkpointInsertKeepSections_.empty();
    }

    bool CheckpointNoClearRestoreExact() const noexcept {
        return CheckpointDirectRestoreExact() && checkpointClearCallCount_ == 1;
    }

    bool CheckpointNoClearRollbackExact() const noexcept {
        return CheckpointDirectRollbackExact() && checkpointClearCallCount_ == 1;
    }

    bool CheckpointDirectRollbackFailed() const noexcept {
        return checkpointDocumentFileFixture_ && checkpointOpenCount_ == 2 &&
            checkpointDocumentBlock_ == L"CHECKPOINT_TARGET" &&
            pageCount_ == kCheckpointDocumentPages - 1 && !documentOpen_ &&
            checkpointInsertKeepSections_.empty();
    }

    std::wstring CheckpointDirectState() const {
        return L"opens=" + std::to_wstring(checkpointOpenCount_) +
            L";block=" + checkpointDocumentBlock_ +
            L";pages=" + std::to_wstring(pageCount_) +
            L";open=" + std::to_wstring(documentOpen_) +
            L";modified=" + std::to_wstring(modified_) +
            L";position=" + std::to_wstring(currentList_) + L":" +
                std::to_wstring(currentParagraph_) + L":" +
                std::to_wstring(currentCharacter_) +
            L";selection=" + std::to_wstring(selectionMode_) +
            L";path=" + std::to_wstring(
                fullName_ == checkpointUserDocumentPath_) +
            L";inserts=" + std::to_wstring(checkpointInsertKeepSections_.size());
    }

    void SetCheckpointEngineUndoRestores() noexcept {
        checkpointEngineUndoRestores_ = true;
    }

    void SetCheckpointEngineUndoWrongStep() noexcept {
        checkpointEngineUndoWrongStep_ = true;
    }

    bool CheckpointEngineUndoRestoredExactly() const noexcept {
        return checkpointDocumentFileFixture_ &&
            checkpointDocumentBlock_ == L"CHECKPOINT_TARGET" &&
            pageCount_ == kCheckpointDocumentPages &&
            checkpointContentParagraphs_ == kCheckpointContentParagraphs &&
            !checkpointResidueParagraph_ &&
            !checkpointLeadingSectionResidue_ &&
            checkpointInsertKeepSections_.empty();
    }

    bool CheckpointEngineWrongStepWasReversed() const noexcept {
        return CheckpointEngineUndoRestoredExactly() &&
            std::find(runActions_.begin(), runActions_.end(), L"Undo") !=
                runActions_.end() &&
            std::find(runActions_.begin(), runActions_.end(), L"Redo") !=
                runActions_.end();
    }

    // An engine no insert can satisfy: whatever the restore does, the document
    // it produces is not the checkpoint. This is the path that ends with the
    // document in a state nobody can name, and it is the reason the copy the
    // call started from has to survive it.
    // What GetTextFile answers between the insert caret and document end.
    //
    // "\r\n" is the leftover paragraph and is safe to remove. The other two are
    // the refusals, and until this existed neither of them had ever run: a fake
    // that only ever answers "one empty paragraph" cannot exercise the branch
    // that decides not to delete.
    //
    //   body text -> the trim must leave it and let the verification refuse
    //   ""        -> a read that failed, not an empty stretch. Also must be left
    //                alone; IsOnlyParagraphBreaks says true for it, so this is
    //                the one that used to delete an unseen selection.
    void SetCheckpointTailAnswer(const wchar_t* const answer) {
        checkpointTailAnswer_ = answer;
    }

    // What GetTextFile("HWP","") hands back. An empty answer is how this call
    // reports a failed allocation, which is the thing the block probe measures.
    void SetCheckpointDocumentBlock(const wchar_t* const block) {
        checkpointDocumentBlock_ = block;
    }

    // Nothing in the document moved. The probe is read-only, so this must hold
    // across it.
    bool CheckpointDocumentFileUntouched() const noexcept {
        return checkpointDocumentFileFixture_ && pageCount_ == 2 &&
            checkpointContentParagraphs_ == kCheckpointContentParagraphs &&
            checkpointInsertKeepSections_.empty() && runActions_.empty();
    }

    // The same for the first paragraph. "\f" is the one that matters: a manual
    // page break reads as a form feed, and deleting it takes a page with it --
    // which would make the page count come out right for a document that just
    // lost a break.
    void SetCheckpointHeadAnswer(const wchar_t* const answer) {
        checkpointHeadAnswer_ = answer;
    }

    void SetCheckpointHeadPageBreakBefore() noexcept {
        checkpointHeadPageBreakBefore_ = true;
    }

    void SetCheckpointHeadSectionUnproven() noexcept {
        checkpointHeadSectionIsolated_ = false;
    }

    // The engine absorbs the leftover paragraph instead of keeping it, so the
    // insert lands on exactly the checkpoint's page count. This is the shape of
    // the 28 page document that has always restored correctly, and the only one
    // that exercises "the page count already matches, do not go looking".
    void SetCheckpointInsertAbsorbsResidue() noexcept {
        checkpointAbsorbsResidue_ = true;
    }

    // Model the observed case where MoveNextPos=1 does not identify the end of
    // the inserted checkpoint: selecting from the caret to document end returns
    // checkpoint body text. That text must survive while the independent head
    // probe is still allowed to trim a paragraph-break-only residue.
    void SetCheckpointInsertCaretBeforeEnd() noexcept {
        checkpointInsertCaretBeforeEnd_ = true;
        checkpointTailAnswer_ = L"checkpoint body text";
    }

    // One paragraph in the whole document, so there is no second paragraph to
    // bound the first with. Nothing may be deleted from a range that cannot be
    // established.
    void SetCheckpointSingleParagraph() noexcept {
        checkpointSingleParagraph_ = true;
    }

    // The engine reports a selection other than the one that was asked for.
    // SelectTextRange is supposed to catch that; until this existed the fake
    // echoed the request back and the check could never fail.
    void SetCheckpointSelectionMisreports() noexcept {
        checkpointSelectionMisreports_ = true;
    }

    void SetCheckpointSignatureCaptureUnavailable() noexcept {
        checkpointSignatureCaptureUnavailable_ = true;
    }

    void SetCheckpointPageCountBelow() noexcept {
        checkpointPageCountBelow_ = true;
    }

    void SetCheckpointTextCaptureEmpty() noexcept {
        checkpointTextFileEmpty_ = true;
    }

    void SetCheckpointPictureOnly() noexcept {
        checkpointTextFileEmpty_ = true;
        checkpointPictureOnly_ = true;
    }

    void SetCheckpointTextLossInsertAttempts() noexcept {
        checkpointTextLossInsertAttempts_ = 2;
    }

    void SetCheckpointDeleteLeavesTextOnce() noexcept {
        checkpointDeleteLeavesTextAttempts_ = 1;
    }

    void SetCheckpointCrossListEndOnce() noexcept {
        checkpointCrossListEndAttempts_ = 1;
    }

    bool CheckpointTextLossWasRejectedAndRolledBack() const noexcept {
        const std::vector<LONG> expected{0L, 1L, 0L};
        return checkpointContentParagraphs_ == kCheckpointContentParagraphs &&
            checkpointInsertKeepSections_ == expected;
    }

    bool CheckpointPictureOnlyRestoredStructurally() const noexcept {
        return checkpointPictureOnly_ && CheckpointDocumentFileRestoredExactly();
    }

    bool CheckpointResidualTextWasRejectedAndRolledBack() const noexcept {
        const std::vector<LONG> expected{0L};
        return checkpointBodyDeleteAttempts_ == 2 &&
            checkpointContentParagraphs_ == kCheckpointContentParagraphs &&
            checkpointInsertKeepSections_ == expected;
    }

    bool CheckpointCrossListWasRejectedBeforeTargetDelete() const noexcept {
        const std::vector<LONG> expected{0L};
        return checkpointBodyDeleteAttempts_ == 1 &&
            checkpointContentParagraphs_ == kCheckpointContentParagraphs &&
            checkpointInsertKeepSections_ == expected;
    }

    bool CheckpointHeadResidueWasNeverDeleted() const noexcept {
        return !checkpointHeadResidueDeleted_ &&
            checkpointLeadingSectionDeletes_ == 0;
    }

    bool CheckpointLeadingSectionResidueRemovedOnce() const noexcept {
        return checkpointLeadingSectionDeletes_ == 1;
    }

    bool CheckpointLeadingSectionResidueRemovedTwice() const noexcept {
        return checkpointLeadingSectionDeletes_ == 2;
    }

    // A page the restore ends up with that no leftover paragraph explains and no
    // trimming can remove. It is what puts the document above the checkpoint's
    // page count without there being anything the trim is allowed to take.
    void SetCheckpointUnexplainedExtraPage() noexcept {
        checkpointInsertNeverMatches_ = true;
    }

    // Nothing was removed. Used by both refusal cases: the leftover paragraph
    // the fixture created is still there afterwards.
    bool PendingCheckpointFileIsRollback() const noexcept {
        constexpr size_t kRollbackSuffixLength = 9;
        return checkpointPendingFileName_.size() >= kRollbackSuffixLength &&
            checkpointPendingFileName_.compare(
                checkpointPendingFileName_.size() - kRollbackSuffixLength,
                kRollbackSuffixLength,
                L".rollback") == 0;
    }

    bool CheckpointDocumentFileRolledBackExactly() const noexcept {
        return checkpointDocumentFileFixture_ &&
            pageCount_ == 2 &&
            checkpointContentParagraphs_ == kCheckpointContentParagraphs &&
            !checkpointResidueParagraph_ &&
            PendingCheckpointFileIsRollback();
    }

    void PrepareCheckpointDocumentFileRollbackFixture() {
        PrepareCheckpointDocumentFileFixture(false, true);
        checkpointInsertNeverMatches_ = true;
        checkpointRollbackInsertFails_ = true;
    }

    void RestoreCheckpointDocumentFileFixture() {
        checkpointDocumentFileFixture_ = false;
        checkpointInsertNeverMatches_ = false;
        checkpointRollbackInsertFails_ = false;
        checkpointResidueAtFront_ = true;
        checkpointSectionsNeedKeeping_ = false;
        checkpointInsertKeepSections_.clear();
        checkpointResidueParagraph_ = false;
        checkpointLeadingSectionResidue_ = false;
        checkpointLeadingSectionDeletes_ = 0;
        checkpointFlattenedPenalty_ = false;
        checkpointWholeSelected_ = false;
        checkpointBodySelected_ = false;
        checkpointTailSelected_ = false;
        checkpointHeadSelected_ = false;
        checkpointContentParagraphs_ = 0;
        checkpointPendingKeepSection_ = -1;
        checkpointPendingFileName_.clear();
        checkpointLastKeepSection_ = -1;
        checkpointHeadProbeActive_ = false;
        checkpointHeadControlIndex_ = 0;
        checkpointHeadPageBreakBefore_ = false;
        checkpointHeadSectionIsolated_ = true;
        checkpointTailAnswer_ = L"\r\n";
        checkpointHeadAnswer_ = L"\r\n";
        checkpointAbsorbsResidue_ = false;
        checkpointInsertCaretBeforeEnd_ = false;
        checkpointSingleParagraph_ = false;
        checkpointSelectionMisreports_ = false;
        checkpointHeadRangeReportsAfterInsert_ = 0;
        checkpointSignatureCaptureUnavailable_ = false;
        checkpointPageCountBelow_ = false;
        checkpointHeadResidueDeleted_ = false;
        checkpointTextFileEmpty_ = false;
        checkpointPictureOnly_ = false;
        checkpointPresenceScan_ = false;
        checkpointTextLossInsertAttempts_ = 0;
        checkpointDeleteLeavesTextAttempts_ = 0;
        checkpointCrossListEndAttempts_ = 0;
        checkpointBodyDeleteAttempts_ = 0;
        checkpointDocumentBlock_.clear();
        pageCount_ = 1;
        currentList_ = 0;
        currentParagraph_ = 0;
        currentCharacter_ = 0;
        runActions_.clear();
    }

    // The document that is left is the checkpoint's four pages and nothing else,
    // and one insert was enough: the leftover paragraph was trimmed rather than
    // paid for with a page.
    bool CheckpointDocumentFileRestoredExactly() const noexcept {
        const std::vector<LONG> expected{0L};
        return checkpointDocumentFileFixture_ &&
            pageCount_ == kCheckpointDocumentPages &&
            !checkpointResidueParagraph_ &&
            checkpointInsertKeepSections_ == expected;
    }

    // Same four pages, but this checkpoint needed its sections: the flattening
    // insert ran first, could not reproduce it, and the section-faithful insert
    // was tried after it.
    bool CheckpointDocumentFileRestoredBySection() const noexcept {
        const std::vector<LONG> expected{0L, 1L};
        return checkpointDocumentFileFixture_ &&
            pageCount_ == kCheckpointDocumentPages &&
            !checkpointResidueParagraph_ &&
            checkpointInsertKeepSections_ == expected;
    }

    // What the field measured on one 164 page checkpoint:
    //
    //   KeepSection off -> 165. The isolated leading section costs a page.
    //   KeepSection on  -> 166. The split adds another page.
    //
    // A paragraph-only delete deliberately leaves the first charge in this
    // model. The restore becomes exact only after DeleteCtrl removes the proved
    // leading `secd` and Delete removes its empty paragraph.
    LONG CheckpointDocumentFilePages() const noexcept {
        const bool sectionOfItsOwn =
            checkpointLeadingSectionResidue_ &&
            checkpointLastKeepSection_ == 1L;
        const bool ordinaryResidue =
            checkpointResidueParagraph_ &&
            !checkpointLeadingSectionResidue_;
        return kCheckpointDocumentPages +
            (checkpointLeadingSectionResidue_ ? 1L : 0L) +
            (ordinaryResidue ? 1L : 0L) +
            (sectionOfItsOwn ? 1L : 0L) +
            (checkpointFlattenedPenalty_ ? 1L : 0L) -
            (checkpointPageCountBelow_ ? 3L : 0L);
    }

    // A leftover paragraph in front pushes the whole inserted document down one
    // paragraph. That shift is the defect: it is why the body text moved a page.
    LONG CheckpointContentBaseParagraph() const noexcept {
        return checkpointResidueParagraph_ && checkpointResidueAtFront_ ? 1L : 0L;
    }

    LONG CheckpointContentEndParagraph() const noexcept {
        return checkpointContentParagraphs_ > 0
            ? CheckpointContentBaseParagraph() + checkpointContentParagraphs_ - 1
            : 0L;
    }

    LONG CheckpointLastParagraph() const noexcept {
        return checkpointResidueParagraph_ && !checkpointResidueAtFront_
            ? CheckpointContentEndParagraph() + 1
            : CheckpointContentEndParagraph();
    }

    void PrepareReferenceLayoutFixture(const bool failAfterFirstText) {
        referenceLayoutFixture_ = true;
        failReferenceLayoutAfterFirstText_ = failAfterFirstText;
        dropReferenceEdges_ = false;
        preflightSelectionFixture_ = false;
        preflightTableControlSelection_ = false;
        failPreflightSelectionRestore_ = false;
        preflightSelectionControlCaptured_ = false;
        preflightSelectionReadOccurred_ = false;
        preflightSelectionRestoreAttempted_ = false;
        preflightSelectionRestored_ = false;
        parameterSet_->EnableGenericProperties(true);
        atomicAppendFixture_ = true;
        failSelectCtrlFrontWithHresult_ = false;
        failAtomicTailDelete_ = false;
        tailSelection_ = false;
        tableSelected_ = false;
        referenceTableExists_ = false;
        referenceMerged_ = false;
        pageCount_ = 1;
        currentList_ = 0;
        currentParagraph_ = 108;
        currentCharacter_ = 0;
        selectionMode_ = 0;
        selectedStartList_ = 0;
        selectedStartParagraph_ = 0;
        selectedStartCharacter_ = 0;
        selectedEndList_ = 0;
        selectedEndParagraph_ = 0;
        selectedEndCharacter_ = 0;
        atomicTail_.clear();
        atomicTailDeletes_ = 0;
        atomicTailMaximumOccurrences_ = 0;
        insertedText_.clear();
        referenceTextInsertions_ = 0;
        referenceParagraphBreaks_ = 0;
        referenceEdgeExecutions_ = 0;
        referenceEdgeApplications_ = 0;
        referenceBorderReadbacks_ = 0;
        referenceTopologyInspections_ = 0;
        referenceTopologyChanged_ = false;
        referenceRunTopologyMutated_ = false;
        referenceActionTopologyMutated_ = false;
        referenceCallTopologyMutated_ = false;
        staleTopologyCellPositionAttempted_ = false;
        parameterSet_->ClearGenericValues();
        parameterSet_->ResetGenericValueReads();
        referenceFillApplications_ = 0;
        referenceControlDeletes_ = 0;
        referenceCells_.clear();
        InitializeReferenceCells();
    }

    void RestoreReferenceLayoutFixture() noexcept {
        referenceLayoutFixture_ = false;
        twoReferenceTables_ = false;
        selectedReferenceTableId_ = L"reference-table";
        otherReferenceCells_.clear();
        emptyCellTextFixture_ = false;
        emptyCellSelectAll_ = false;
        preflightSelectionFixture_ = false;
        preflightTableControlSelection_ = false;
        failPreflightSelectionRestore_ = false;
        preflightSelectionControlCaptured_ = false;
        preflightSelectionReadOccurred_ = false;
        preflightSelectionRestoreAttempted_ = false;
        preflightSelectionRestored_ = false;
        failReferenceLayoutAfterFirstText_ = false;
        dropReferenceEdges_ = false;
        parameterSet_->EnableGenericProperties(false);
        atomicAppendFixture_ = false;
        tailSelection_ = false;
        tableSelected_ = false;
        referenceTableExists_ = false;
        referenceMerged_ = false;
        referenceTopologyInspections_ = 0;
        referenceTopologyChanged_ = false;
        referenceRunTopologyMutated_ = false;
        referenceActionTopologyMutated_ = false;
        referenceCallTopologyMutated_ = false;
        staleTopologyCellPositionAttempted_ = false;
        pageCount_ = 1;
        currentList_ = 0;
        currentParagraph_ = 0;
        currentCharacter_ = 0;
        selectionMode_ = 1;
        atomicTail_.clear();
        insertedText_.clear();
        referenceCells_.clear();
        referenceParagraphBreaks_ = 0;
    }

    void PrepareReferenceLayoutDroppedEdgesFixture() {
        PrepareReferenceLayoutFixture(false);
        dropReferenceEdges_ = true;
    }

    bool ReferenceLayoutCreatedExactly() const noexcept {
        const ReferenceCell* const a1 = ReferenceCellByAddress(L"A1");
        const ReferenceCell* const a2 = ReferenceCellByAddress(L"A2");
        const ReferenceCell* const b2 = ReferenceCellByAddress(L"B2");
        const ReferenceCell* const a3 = ReferenceCellByAddress(L"A3");
        const ReferenceCell* const b3 = ReferenceCellByAddress(L"B3");
        return referenceLayoutFixture_ && referenceTableExists_ && referenceMerged_ &&
            pageCount_ == 2 && ReferenceActiveCellCount() == 5 &&
            a1 != nullptr && a1->columnSpan == 2 && a1->text == L"Merged header" &&
            a2 != nullptr && a2->text == L"A2" &&
            b2 != nullptr && b2->text == L"B2" &&
            a3 != nullptr && a3->text == L"A3" &&
            b3 != nullptr && b3->text == L"B3" &&
            ReferenceFormatMatches(a1->format, HeaderReferenceFormat()) &&
            ReferenceFormatMatches(a2->format, BodyReferenceFormat()) &&
            ReferenceFormatMatches(b2->format, BodyReferenceFormat()) &&
            ReferenceFormatMatches(a3->format, BodyReferenceFormat()) &&
            ReferenceFormatMatches(b3->format, BodyReferenceFormat()) &&
            referenceTextInsertions_ == 5 && referenceEdgeApplications_ == 15 &&
            referenceFillApplications_ == 1 && referenceControlDeletes_ == 0;
    }

    bool ReferenceBorderReadbackCoveredRequestedEdges() const noexcept {
        return referenceEdgeExecutions_ == 15 &&
            referenceEdgeApplications_ == 15 &&
            referenceBorderReadbacks_ == 5 &&
            parameterSet_->GenericValueReads() == 45;
    }

    bool ReferenceLayoutDroppedEdgesObserved() const noexcept {
        return referenceEdgeExecutions_ == 15 &&
            referenceEdgeApplications_ == 0 &&
            referenceBorderReadbacks_ == 1 &&
            parameterSet_->GenericValueReads() == 2;
    }

    bool ReferenceLayoutRollbackRestored() const noexcept {
        return referenceLayoutFixture_ && !referenceTableExists_ && pageCount_ == 1 &&
            atomicTail_.empty() && referenceControlDeletes_ == 1 &&
            atomicTailDeletes_ == 1 && referenceTextInsertions_ == 1;
    }

    void PrepareEmptyCellTextFixture() {
        PrepareReferenceLayoutFixture(false);
        emptyCellTextFixture_ = true;
        emptyCellSelectAll_ = false;
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        static_cast<void>(SetReferenceCurrentCell(L"A2"));
        selectionMode_ = 0;
        selectedStartList_ = 0;
        selectedStartParagraph_ = 0;
        selectedStartCharacter_ = 0;
        selectedEndList_ = 0;
        selectedEndParagraph_ = 0;
        selectedEndCharacter_ = 0;
    }

    bool EmptyCellTextFixtureReady() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"A2");
        return emptyCellTextFixture_ && cell != nullptr && cell->text.empty() &&
            currentList_ == 650 && currentParagraph_ == 0 && currentCharacter_ == 0 &&
            selectionMode_ == 0 && selectedStartList_ == 0 &&
            selectedStartParagraph_ == 0 && selectedStartCharacter_ == 0;
    }

    bool EmptyCellTextInsertedExactly() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"A2");
        return cell != nullptr && cell->text == L"filled" &&
            referenceTextInsertions_ == 1;
    }

    void PrepareMultilineCellTextFixture(const bool patchText) {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        const std::wstring address = patchText ? L"B2" : L"A2";
        ReferenceCell* const cell = ReferenceCellByAddress(address);
        if (patchText && cell != nullptr) {
            cell->text = L"old";
        }
        static_cast<void>(SetReferenceCurrentCell(address));
        selectionMode_ = 0;
    }

    bool MultilineCellTextWrittenExactly(
        const std::wstring& address,
        const std::wstring& expected) const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(address);
        return cell != nullptr && cell->text == expected &&
            referenceTextInsertions_ == 2 && referenceParagraphBreaks_ == 1;
    }

    void PrepareAllTargetCellTextPreflightFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        ReferenceCell* const later = ReferenceCellByAddress(L"B2");
        if (later != nullptr) {
            later->text = L"live";
        }
        static_cast<void>(SetReferenceCurrentCell(L"A2"));
        selectionMode_ = 0;
    }

    bool AllTargetCellTextPreflightPreserved() const noexcept {
        const ReferenceCell* const earlier = ReferenceCellByAddress(L"A2");
        const ReferenceCell* const later = ReferenceCellByAddress(L"B2");
        return earlier != nullptr && earlier->text.empty() &&
            later != nullptr && later->text == L"live" &&
            ReferenceFormatMatches(earlier->format, BodyReferenceFormat()) &&
            ReferenceFormatMatches(later->format, BodyReferenceFormat()) &&
            referenceTextInsertions_ == 0 && referenceParagraphBreaks_ == 0;
    }

    void PrepareTopologyReuseFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        ReferenceCell* const first = ReferenceCellByAddress(L"A2");
        ReferenceCell* const second = ReferenceCellByAddress(L"B2");
        if (first != nullptr) {
            first->text = L"old";
        }
        if (second != nullptr) {
            second->text = L"old";
        }
        static_cast<void>(SetReferenceCurrentCell(L"A2"));
        selectionMode_ = 0;
    }

    bool TopologyWasReusedAcrossTextBatch() const noexcept {
        const ReferenceCell* const first = ReferenceCellByAddress(L"A2");
        const ReferenceCell* const second = ReferenceCellByAddress(L"B2");
        return first != nullptr && first->text == L"new-a" &&
            second != nullptr && second->text == L"new-b" &&
            referenceTopologyInspections_ == 2;
    }

    void PreparePublicCellTopologyFixture() {
        PrepareTopologyReuseFixture();
        wrongReferenceControlIdentity_ = false;
        for (ReferenceCell& cell : referenceCells_) {
            cell.text = L"old";
        }
    }

    void PrepareEmptyPublicCellTopologyFixture() {
        PreparePublicCellTopologyFixture();
        for (ReferenceCell& cell : referenceCells_) cell.text.clear();
    }

    void SetWrongReferenceControlIdentity(const bool enabled) noexcept {
        wrongReferenceControlIdentity_ = enabled;
    }

    void PreparePublicTwoTableTopologyFixture() {
        PreparePublicCellTopologyFixture();
        twoReferenceTables_ = true;
        selectedReferenceTableId_ = L"reference-table";
        otherReferenceCells_ = referenceCells_;
        for (ReferenceCell& cell : referenceCells_) {
            cell.text = L"old-a";
        }
        for (ReferenceCell& cell : otherReferenceCells_) {
            cell.listId += 1000;
            cell.text = L"old-b";
        }
    }

    bool PublicTwoTableStateMatches(const bool edited) const noexcept {
        if (!twoReferenceTables_ || selectedReferenceTableId_ != L"reference-table" ||
            referenceTextInsertions_ != (edited ? 3U : 0U)) {
            return false;
        }
        for (size_t index = 0; index < referenceCells_.size(); ++index) {
            ReferenceFormat firstFormat = BodyReferenceFormat();
            ReferenceFormat secondFormat = BodyReferenceFormat();
            if (edited && index < 2) firstFormat.textColor = 8388736;
            if (edited && index == 0) secondFormat.textColor = 8388736;
            if (referenceCells_[index].text != (edited && index < 2 ? L"new" : L"old-a") ||
                otherReferenceCells_[index].text != (edited && index == 0 ? L"new" : L"old-b") ||
                !ReferenceFormatMatches(referenceCells_[index].format, firstFormat) ||
                !ReferenceFormatMatches(otherReferenceCells_[index].format, secondFormat)) {
                return false;
            }
        }
        return true;
    }

    size_t PublicCellTopologyInspections() const noexcept {
        return referenceTopologyInspections_;
    }

    bool PublicCellPatchStateMatches(
        const size_t changedCells,
        const bool purple,
        const bool initiallyEmpty = false) const noexcept {
        if (referenceTextInsertions_ != changedCells) {
            return false;
        }
        for (size_t index = 0; index < referenceCells_.size(); ++index) {
            const ReferenceCell& cell = referenceCells_[index];
            ReferenceFormat expected = BodyReferenceFormat();
            if (index < changedCells && purple) {
                expected.textColor = 8388736;
            }
            if (cell.text != (index < changedCells ? L"new" : initiallyEmpty ? L"" : L"old") ||
                !ReferenceFormatMatches(cell.format, expected)) {
                return false;
            }
        }
        return true;
    }

    void PrepareFormattingTopologyPolicyFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        static_cast<void>(SetReferenceCurrentCell(L"A1"));
        selectionMode_ = 0;
    }

    void PrepareTextFormatReadbackFixture(const bool dropAppliedFormat) {
        PrepareFormattingTopologyPolicyFixture();
        dropAppliedTextFormat_ = dropAppliedFormat;
        pendingTextFormatReadback_ = false;
        textFormatActionsExecuted_ = 0;
        textFormatReadbacks_ = 0;
    }

    void PrepareRequestedFormatPreflightFixture(
        const bool requestedPropertyUnavailable,
        const bool automaticNumberReadback = false,
        const bool staleContent = false) {
        PrepareFormattingTopologyPolicyFixture();
        unavailableUnrelatedFormatProperty_ = true;
        unavailableRequestedFormatProperty_ = requestedPropertyUnavailable;
        automaticNumberReadback_ = automaticNumberReadback;
        ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        if (cell != nullptr) {
            cell->text = staleContent ? L"new" : L"old";
        }
        static_cast<void>(SetReferenceCurrentCell(L"B2"));
        selectionMode_ = 0;
    }

    // A paragraph that HWP numbers automatically. SelectText refuses the
    // range, the block readback carries the drawn number in front of the
    // selected text, and the patch has to come through both.
    void PrepareAutomaticNumberTextPatchFixture(const bool selectTextRefuses) {
        PrepareFormattingTopologyPolicyFixture();
        automaticNumberReadback_ = true;
        automaticNumberSelectTextRefuses_ = selectTextRefuses;
        automaticNumberSelectTextRefusals_ = 0;
        caretSelectionPending_ = false;
        caretSelectionsCompleted_ = 0;
        ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        if (cell != nullptr) {
            cell->text = L"old";
        }
        static_cast<void>(SetReferenceCurrentCell(L"B2"));
        selectionMode_ = 0;
    }

    bool AutomaticNumberTextPatchApplied() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        return cell != nullptr && cell->text == L"new";
    }

    // A cell whose text holds the literal with other body text in front of it,
    // and a ForwardFind that reports `surplus` characters more than it
    // matched. Whatever the replacement does, those characters are not the
    // text that was searched for and must still be there afterwards.
    void PrepareFindSelectionSurplusFixture(
        const size_t surplus,
        const bool automaticNumberReadback,
        const bool displayOnlyPrefix = false) {
        PrepareFormattingTopologyPolicyFixture();
        automaticNumberReadback_ = automaticNumberReadback;
        automaticNumberSelectTextRefuses_ = false;
        automaticNumberSelectTextRefusals_ = 0;
        caretSelectionPending_ = false;
        caretSelectionsCompleted_ = 0;
        findSelectionSurplus_ = surplus;
        findDisplayOnlyPrefix_ = displayOnlyPrefix;
        findString_.clear();
        textScanChunks_.clear();
        failTextScanRelease_ = false;
        findActionAttempts_ = 0;
        textScanHiddenPrefix_ = 0;
        textScanHiddenAt_ = 0;
        textScanReadbacks_ = 0;
        scanStep_ = 0;
        ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        if (cell != nullptr) {
            cell->text = L"the old";
        }
        static_cast<void>(SetReferenceCurrentCell(L"B2"));
        selectionMode_ = 0;
    }

    std::wstring FindSelectionSurplusCellText() const {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        return cell == nullptr ? std::wstring() : cell->text;
    }

    struct TextScanChunk {
        LONG state;
        LONG list;
        LONG paragraph;
        LONG character;
        std::wstring text;
    };

    void PrepareTextScanFixture(
        const std::wstring& text,
        std::vector<TextScanChunk> chunks,
        const bool failRelease = false,
        const LONG hiddenPrefix = 0,
        const LONG hiddenAt = 0) {
        PrepareFindSelectionSurplusFixture(0, false);
        ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        if (cell != nullptr) {
            cell->text = text;
        }
        textScanChunks_ = std::move(chunks);
        failTextScanRelease_ = failRelease;
        textScanHiddenPrefix_ = hiddenPrefix;
        textScanHiddenAt_ = hiddenAt;
    }

    bool TextScanWasPopupFree() const noexcept {
        return findActionAttempts_ == 0 && scanReleased_;
    }

    LONG TextScanSteps() const noexcept { return scanStep_; }
    size_t TextScanReadbacks() const noexcept { return textScanReadbacks_; }
    bool TextScanDidNotSearch() const noexcept { return scanStep_ == 0 && findActionAttempts_ == 0; }

    bool AutomaticNumberSelectTextFallbackUsed() const noexcept {
        return automaticNumberSelectTextRefusals_ > 0 &&
            caretSelectionsCompleted_ >= automaticNumberSelectTextRefusals_;
    }

    bool TextFormatReadbackSucceeded() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"A1");
        return cell != nullptr && cell->format.textColor == 255 &&
            cell->format.alignment == 3 && textFormatActionsExecuted_ == 2 &&
            textFormatReadbacks_ == 2;
    }

    bool TextFormatMismatchWasReadBack() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"A1");
        return cell != nullptr && cell->format.textColor != 65280 &&
            textFormatActionsExecuted_ == 1 && textFormatReadbacks_ == 1;
    }

    void PrepareRangeFormatReadbackFixture(const bool dropAppliedFormat) {
        PrepareFormattingTopologyPolicyFixture();
        rangeFormatReadbackFixture_ = true;
        dropAppliedRangeFormat_ = dropAppliedFormat;
        rangeCellFillValue_ = 0;
        rangeCellFillCoveredBlock_ = false;
        rangeCellBorderCoveredBlock_ = false;
        rangePaddingCoveredBlock_ = false;
        rangeSubsequentActionCoveredBlock_ = false;
    }

    bool RangeFormatSelectionWasPreserved() const noexcept {
        return rangeCellFillValue_ == 255 && rangeCellFillCoveredBlock_ &&
            rangeCellBorderCoveredBlock_ && rangePaddingCoveredBlock_ &&
            rangeSubsequentActionCoveredBlock_ && selectionMode_ == 3;
    }

    bool RangeFormatMismatchWasReadBack() const noexcept {
        return rangeCellFillValue_ != 65280 && rangeCellFillCoveredBlock_ &&
            selectionMode_ == 3;
    }

    bool TopologyWasPreservedAfterCellFormatting() const noexcept {
        const ReferenceCell* const b1 = ReferenceCellByAddress(L"B1");
        return b1 != nullptr && currentList_ == b1->listId &&
            referenceTopologyInspections_ == 1;
    }

    bool TopologyWasPreservedAfterCellPadding() const noexcept {
        const ReferenceCell* const b1 = ReferenceCellByAddress(L"B1");
        return b1 != nullptr && currentList_ == b1->listId &&
            referenceTopologyInspections_ == 1;
    }

    bool TopologyWasInvalidatedAfterCellSizeChange() const noexcept {
        const ReferenceCell* const b1 = ReferenceCellByAddress(L"B1");
        return b1 != nullptr && currentList_ == b1->listId &&
            referenceTopologyInspections_ == 2;
    }

    bool TopologyWasInvalidatedAfterUnknownAction() const noexcept {
        const ReferenceCell* const b1 = ReferenceCellByAddress(L"B1");
        return b1 != nullptr && currentList_ == b1->listId &&
            referenceTopologyInspections_ == 2;
    }

    void PrepareTopologyInvalidationFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        static_cast<void>(SetReferenceCurrentCell(L"A1"));
        selectionMode_ = 0;
    }

    bool TopologyWasInvalidatedAfterMerge() const noexcept {
        return referenceMerged_ &&
            ReferenceCellByAddress(L"B1") == nullptr &&
            referenceTopologyInspections_ == 2 &&
            !staleTopologyCellPositionAttempted_;
    }

    void PrepareRunTopologyInvalidationFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        static_cast<void>(SetReferenceCurrentCell(L"A1"));
        selectionMode_ = 0;
    }

    bool TopologyWasInvalidatedAfterRunDeleteRow() const noexcept {
        const ReferenceCell* const b1 = ReferenceCellByAddress(L"B1");
        return referenceRunTopologyMutated_ &&
            b1 != nullptr && b1->listId == 651 && currentList_ == b1->listId &&
            referenceTopologyInspections_ == 2 &&
            !staleTopologyCellPositionAttempted_;
    }

    void PrepareActionTopologyInvalidationFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        for (ReferenceCell& cell : referenceCells_) {
            if (cell.column == 2) {
                cell.active = false;
            }
        }
        static_cast<void>(SetReferenceCurrentCell(L"A1"));
        selectionMode_ = 0;
    }

    bool TopologyWasInvalidatedAfterActionInsertColumn() const noexcept {
        const ReferenceCell* const b1 = ReferenceCellByAddress(L"B1");
        return referenceActionTopologyMutated_ &&
            b1 != nullptr && currentList_ == b1->listId &&
            referenceTopologyInspections_ == 2 &&
            !staleTopologyCellPositionAttempted_;
    }

    void PrepareCallTopologyInvalidationFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        static_cast<void>(SetReferenceCurrentCell(L"A1"));
        selectionMode_ = 0;
    }

    bool TopologyWasInvalidatedAfterCall() const noexcept {
        return referenceCallTopologyMutated_ &&
            ReferenceCellByAddress(L"B1") == nullptr &&
            referenceTopologyInspections_ == 2 &&
            !staleTopologyCellPositionAttempted_;
    }

    void PrepareOversizedCellPatchFixture() {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        if (cell != nullptr) {
            cell->text = L"old";
        }
        static_cast<void>(SetReferenceCurrentCell(L"A2"));
        selectionMode_ = 0;
    }

    bool OversizedCellPatchPreservedDocument() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"B2");
        return cell != nullptr && cell->text == L"old" &&
            referenceTopologyInspections_ == 0 &&
            referenceTextInsertions_ == 0;
    }

    void PrepareTableTextPreflightSelectionFixture(
        const bool tableControlSelection,
        const bool failRestore) {
        PrepareReferenceLayoutFixture(false);
        referenceTableExists_ = true;
        atomicAppendFixture_ = false;
        preflightSelectionFixture_ = true;
        preflightTableControlSelection_ = tableControlSelection;
        failPreflightSelectionRestore_ = failRestore;
        preflightSelectionControlCaptured_ = false;
        preflightSelectionReadOccurred_ = false;
        preflightSelectionRestoreAttempted_ = false;
        preflightSelectionRestored_ = false;
        ReferenceCell* const cell = ReferenceCellByAddress(L"A2");
        if (cell != nullptr) {
            cell->text = L"old";
        }
        static_cast<void>(SetReferenceCurrentCell(
            tableControlSelection ? L"A2" : L"B2"));
        selectedStartList_ = 650;
        selectedStartParagraph_ = 0;
        selectedStartCharacter_ = 0;
        selectedEndList_ = tableControlSelection ? 650 : 651;
        selectedEndParagraph_ = 0;
        selectedEndCharacter_ = 0;
        referenceSelectionAnchorList_ = selectedStartList_;
        selectionMode_ = tableControlSelection ? 4L : 0x13L;
        tableSelected_ = tableControlSelection;
    }

    bool TableTextPreflightSelectionBatchSucceeded() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"A2");
        return preflightSelectionControlCaptured_ &&
            preflightSelectionReadOccurred_ &&
            preflightSelectionRestoreAttempted_ &&
            preflightSelectionRestored_ &&
            cell != nullptr && cell->text == L"new" &&
            referenceTextInsertions_ == 1 && referenceParagraphBreaks_ == 0;
    }

    bool TableTextPreflightRestoreFailurePreservedDocument() const noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(L"A2");
        return preflightSelectionControlCaptured_ &&
            preflightSelectionReadOccurred_ &&
            preflightSelectionRestoreAttempted_ &&
            !preflightSelectionRestored_ &&
            cell != nullptr && cell->text == L"old" &&
            ReferenceFormatMatches(cell->format, BodyReferenceFormat()) &&
            referenceTextInsertions_ == 0 && referenceParagraphBreaks_ == 0;
    }

    bool UsedAutoYesMessageBoxMode() const noexcept {
        return autoYesMessageBoxModeUsed_;
    }

    const std::wstring& InsertedText() const noexcept {
        return insertedText_;
    }

    size_t InsertTextExecutions() const noexcept {
        return insertTextExecutions_;
    }

    void PrepareFirstWriteReadbackFailure() noexcept {
        firstWriteReadbackFixture_ = true;
        firstWriteReadbackFailed_ = false;
        selectionMode_ = 0;
        currentList_ = 0;
        currentParagraph_ = 108;
        currentCharacter_ = 0;
        insertedText_.clear();
        insertTextExecutions_ = 0;
    }

    bool FirstWriteReadbackFailureWasClassified() const noexcept {
        return firstWriteReadbackFailed_ && insertTextExecutions_ == 1 &&
            insertedText_ == L"new";
    }

    void PrepareSelectedControlTextPatch() noexcept {
        selectionMode_ = 4;
        insertedText_.clear();
        insertTextExecutions_ = 0;
    }

    void RestoreTextPatchSelectionFixture() noexcept {
        selectionMode_ = 1;
        insertedText_.clear();
    }

    void PrepareTextDeletionPatch() noexcept {
        textDeletionFixture_ = true;
        textSelectionActive_ = true;
        selectionMode_ = 1;
        currentList_ = 0;
        currentParagraph_ = 108;
        currentCharacter_ = 0;
        insertedText_.clear();
        insertTextExecutions_ = 0;
        textDeletionActions_ = 0;
    }

    bool DeletedSelectedText() const noexcept {
        return textDeletionFixture_ && !textSelectionActive_ &&
            selectionMode_ == 0 && textDeletionActions_ == 1 &&
            insertTextExecutions_ == 0;
    }

    void RestoreTextDeletionPatch() noexcept {
        textDeletionFixture_ = false;
        textSelectionActive_ = true;
        selectionMode_ = 1;
        textDeletionActions_ = 0;
    }

    void PrepareScopedInspectionFixture() {
        inspectionScopeFixture_ = true;
        inspectionControls_ = {
            {L"gso", L"page-1-shape", L"shape", 1},
            {L"gso", L"page-29-shape", L"shape", 29},
            {L"tbl", L"page-30-table", L"table", 30},
            {L"gso", L"page-31-shape-a", L"shape", 31},
            {L"gso", L"page-31-shape-b", L"shape", 31},
        };
        inspectionControlIndex_ = -1;
        inspectionAnchorRoot_ = false;
        inspectionCurrentPage_ = 30;
        inspectionHeadReads_ = 0;
        inspectionLastReads_ = 0;
        inspectionNextReads_ = 0;
        inspectionPrevReads_ = 0;
        inspectionIdentityReads_ = 0;
        pageCount_ = 31;
        currentList_ = 31;
        currentParagraph_ = 0;
        currentCharacter_ = 0;
        selectionMode_ = 0;
        modified_ = false;
        documentOpen_ = true;
        fullName_ = L"C:\\x.hwp";
    }

    bool UsedBoundedBackwardInspection() const noexcept {
        return inspectionLastReads_ == 1 && inspectionPrevReads_ == 3 &&
            inspectionIdentityReads_ >= 4;
    }

    bool PreservedUnscopedForwardInspection() const noexcept {
        return inspectionHeadReads_ >= 1 && inspectionLastReads_ == 0 &&
            inspectionNextReads_ >= inspectionControls_.size() &&
            inspectionPrevReads_ == 0 &&
            inspectionIdentityReads_ >= inspectionControls_.size();
    }

    void RestoreScopedInspectionFixture() noexcept {
        inspectionScopeFixture_ = false;
        inspectionControls_.clear();
        inspectionControlIndex_ = -1;
        inspectionAnchorRoot_ = false;
        pageCount_ = 1;
        currentList_ = 0;
        currentParagraph_ = 108;
        currentCharacter_ = 0;
        selectionMode_ = 1;
    }

    bool MessageBoxModeRestored() const noexcept {
        return messageBoxMode_ == kInitialMessageBoxMode;
    }

    void PrepareTransientMessageBoxModeRestoreFailure() noexcept {
        messageBoxMode_ = kInitialMessageBoxMode;
        failNextMessageBoxModeRestore_ = true;
        messageBoxModeRestoreAttempts_ = 0;
    }

    bool RetriedMessageBoxModeRestore() const noexcept {
        return !failNextMessageBoxModeRestore_ &&
            messageBoxModeRestoreAttempts_ == 2 &&
            MessageBoxModeRestored();
    }

    bool UsedStreamingTextScan() const noexcept {
        return scanStarted_ && scanReleased_;
    }

    bool PreservedAttachedCaptionNumber() const noexcept {
        const auto attached = std::find(
            runActions_.begin(), runActions_.end(), L"ShapeObjAttachCaption");
        const auto moved = std::find(
            attached, runActions_.end(), L"MoveParaEnd");
        const auto selected = std::find(moved, runActions_.end(), L"SelectAll");
        const auto closed = std::find(selected, runActions_.end(), L"CloseEx");
        return attached != runActions_.end() &&
            moved != runActions_.end() &&
            selected != runActions_.end() &&
            closed != runActions_.end() &&
            std::find(attached, closed, L"Delete") == closed;
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(
        REFIID interfaceId,
        void** const object) override {
        if (object == nullptr) {
            return E_POINTER;
        }
        if (interfaceId == IID_IUnknown || interfaceId == IID_IDispatch) {
            *object = static_cast<IDispatch*>(this);
            static_cast<void>(AddRef());
            return S_OK;
        }
        if (interfaceId == IID_IConnectionPointContainer) {
            *object = static_cast<IConnectionPointContainer*>(this);
            static_cast<void>(AddRef());
            return S_OK;
        }
        if (interfaceId == IID_IConnectionPoint) {
            *object = static_cast<IConnectionPoint*>(this);
            static_cast<void>(AddRef());
            return S_OK;
        }
        *object = nullptr;
        return E_NOINTERFACE;
    }

    ULONG STDMETHODCALLTYPE AddRef() override {
        return static_cast<ULONG>(InterlockedIncrement(&references_));
    }

    ULONG STDMETHODCALLTYPE Release() override {
        const LONG remaining = InterlockedDecrement(&references_);
        if (remaining == 0) {
            delete this;
        }
        return static_cast<ULONG>(remaining);
    }

    HRESULT STDMETHODCALLTYPE GetTypeInfoCount(UINT* const count) override {
        if (count == nullptr) {
            return E_POINTER;
        }
        *count = 0;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetTypeInfo(
        UINT,
        LCID,
        ITypeInfo**) override {
        return E_NOTIMPL;
    }

    HRESULT STDMETHODCALLTYPE GetIDsOfNames(
        REFIID,
        LPOLESTR* const names,
        const UINT count,
        LCID,
        DISPID* const members) override {
        if (names == nullptr || members == nullptr || count != 1) {
            return E_INVALIDARG;
        }
        const std::wstring name(names[0]);
        if (name == L"XHwpDocuments") {
            members[0] = XHwpDocuments;
        } else if (name == L"Active_XHwpDocument") {
            members[0] = ActiveDocument;
        } else if (name == L"DocumentID") {
            members[0] = DocumentId;
        } else if (name == L"FullName") {
            if (requireActiveDocumentFullName_ && !activeDocumentFullNameReady_) {
                return DISP_E_UNKNOWNNAME;
            }
            members[0] = FullName;
        } else if (name == L"HAction") {
            members[0] = HAction;
        } else if (name == L"CellShape") {
            members[0] = CellShape;
        } else if (name == L"ParentCtrl") {
            members[0] = ParentCtrl;
        } else if (name == L"CurSelectedCtrl") {
            members[0] = CurSelectedCtrl;
        } else if (name == L"SelectCtrl") {
            members[0] = SelectCtrl;
        } else if (name == L"GetCtrlInstID") {
            members[0] = GetCtrlInstId;
        } else if (name == L"Run") {
            members[0] = Run;
        } else if (name == L"GetAnchorPos") {
            members[0] = GetAnchorPos;
        } else if (name == L"Item") {
            members[0] = Item;
        } else if (name == L"SetPos") {
            members[0] = SetPos;
        } else if (name == L"HParameterSet") {
            members[0] = HParameterSet;
        } else if (name == L"HStyle") {
            members[0] = HStyle;
        } else if (name == L"HParaShape") {
            members[0] = HParaShape;
        } else if (referenceLayoutFixture_ && name == L"HCharShape") {
            members[0] = HCharShape;
        } else if (name == L"HFindReplace") {
            members[0] = HFindReplace;
        } else if (name == L"FindString") {
            members[0] = FindString;
        } else if (name == L"Direction") {
            members[0] = FindDirection;
        } else if (name == L"MatchCase") {
            members[0] = FindMatchCase;
        } else if (name == L"IgnoreMessage") {
            members[0] = FindIgnoreMessage;
        } else if (name == L"HSet") {
            members[0] = HSet;
        } else if (name == L"GetDefault") {
            members[0] = GetDefault;
        } else if (name == L"Execute") {
            members[0] = Execute;
        } else if (name == L"Apply") {
            members[0] = Apply;
        } else if (name == L"AlignType") {
            members[0] = AlignType;
        } else if (name == L"LineSpacing") {
            members[0] = LineSpacing;
        } else if (name == L"LeftMargin") {
            members[0] = LeftMargin;
        } else if (name == L"RightMargin") {
            members[0] = RightMargin;
        } else if (name == L"Indentation") {
            members[0] = Indentation;
        } else if (name == L"PrevSpacing") {
            members[0] = PrevSpacing;
        } else if (name == L"NextSpacing") {
            members[0] = NextSpacing;
        } else if (name == L"PageBreakBefore") {
            members[0] = PageBreakBefore;
        } else if (name == L"SetMessageBoxMode") {
            members[0] = SetMessageBoxMode;
        } else if (name == L"SelectText") {
            members[0] = SelectText;
        } else if (name == L"GetHeadingString") {
            members[0] = GetHeadingString;
        } else if (name == L"GetTextFile") {
            members[0] = GetTextFile;
        } else if (name == L"SetTextFile") {
            members[0] = SetTextFile;
        } else if (name == L"InitScan") {
            members[0] = InitScan;
        } else if (name == L"MovePos") {
            members[0] = MovePos;
        } else if (name == L"GetText") {
            members[0] = GetText;
        } else if (name == L"ReleaseScan") {
            members[0] = ReleaseScan;
        } else if (name == L"HInsertText") {
            members[0] = HInsertText;
        } else if (name == L"HInsertFile") {
            members[0] = HInsertFile;
        } else if (name == L"HArrayFixture") {
            members[0] = HArrayFixture;
        } else if (name == L"SelectionMode") {
            members[0] = SelectionMode;
        } else if (name == L"XHwpWindows") {
            members[0] = XHwpWindows;
        } else if (name == L"Active_XHwpWindow") {
            members[0] = ActiveWindow;
        } else if (name == L"WindowHandle") {
            members[0] = WindowHandle;
        } else if (name == L"FindItem") {
            members[0] = FindItem;
        } else if (name == L"SetActive_XHwpDocument") {
            members[0] = SetActiveDocument;
        } else if (name == L"Text") {
            members[0] = Text;
        } else if (name == L"ReturnBoolean") {
            members[0] = ReturnBoolean;
        } else if (name == L"ReturnInteger") {
            members[0] = ReturnInteger;
        } else if (name == L"ReturnText") {
            members[0] = ReturnText;
        } else if (name == L"ReturnVoid") {
            members[0] = ReturnVoid;
        } else if (name == L"CreateAction") {
            members[0] = CreateAction;
        } else if (name == L"CreateSet") {
            members[0] = CreateSet;
        } else if (name == L"PageCount") {
            members[0] = PageCount;
        } else if (name == L"IsModified") {
            members[0] = IsModified;
        } else if (name == L"SetItem") {
            members[0] = SetItem;
        } else if (name == L"ItemExist") {
            members[0] = ItemExist;
        } else if (name == L"XHwpMessageBox") {
            members[0] = XHwpMessageBox;
        } else if (name == L"Application") {
            members[0] = Application;
        } else if (name == L"SetCurMetatagName") {
            members[0] = SetCurMetatagName;
        } else if (name == L"Password") {
            members[0] = Password;
        } else if (name == L"SetID") {
            members[0] = SetID;
        } else if (name == L"GetSelectedPosBySet") {
            members[0] = GetSelectedPosBySet;
        } else if (name == L"HeadCtrl") {
            members[0] = HeadCtrl;
        } else if (inspectionScopeFixture_ && name == L"LastCtrl") {
            members[0] = LastCtrl;
        } else if (name == L"CtrlID") {
            members[0] = CtrlID;
        } else if (name == L"Next") {
            members[0] = Next;
        } else if (inspectionScopeFixture_ && name == L"Prev") {
            members[0] = Prev;
        } else if (inspectionScopeFixture_ && name == L"UserDesc") {
            members[0] = UserDesc;
        } else if (inspectionScopeFixture_ && name == L"GetPageText") {
            members[0] = GetPageText;
        } else if (name == L"GetPos") {
            members[0] = GetPos;
        } else if (name == L"Save") {
            members[0] = Save;
        } else if (name == L"SaveAs") {
            members[0] = SaveAs;
        } else if ((referenceLayoutFixture_ || inspectionScopeFixture_) &&
                   name == L"XHwpDocumentInfo") {
            members[0] = XHwpDocumentInfo;
        } else if ((referenceLayoutFixture_ || inspectionScopeFixture_) &&
                   name == L"CurrentPage") {
            members[0] = CurrentPage;
        } else if (referenceLayoutFixture_ && name == L"Properties") {
            members[0] = Properties;
        } else if (referenceLayoutFixture_ && name == L"KeyIndicator") {
            members[0] = KeyIndicator;
        } else if (referenceLayoutFixture_ && name == L"FaceNameHangul" &&
                   !unavailableUnrelatedFormatProperty_) {
            members[0] = FaceNameHangul;
        } else if (referenceLayoutFixture_ && name == L"Height") {
            members[0] = CharacterHeight;
        } else if (referenceLayoutFixture_ && name == L"Bold") {
            members[0] = Bold;
        } else if (referenceLayoutFixture_ && name == L"TextColor" &&
                   !unavailableRequestedFormatProperty_) {
            members[0] = TextColor;
        } else if (referenceLayoutFixture_ && name == L"RatioHangul") {
            members[0] = RatioHangul;
        } else if (referenceLayoutFixture_ && name == L"SpacingHangul") {
            members[0] = SpacingHangul;
        } else if (referenceLayoutFixture_ && name == L"LineSpacingType") {
            members[0] = LineSpacingType;
        } else if (referenceLayoutFixture_ && name == L"HwpLineType") {
            members[0] = HwpLineType;
        } else if (referenceLayoutFixture_ && name == L"HwpLineWidth") {
            members[0] = HwpLineWidth;
        } else if (referenceLayoutFixture_ && name == L"MutateTableTopology") {
            members[0] = MutateTableTopology;
        } else if (name == L"Clear") {
            members[0] = Clear;
        } else if (name == L"Open") {
            members[0] = Open;
        } else if (name == L"DeleteCtrl") {
            members[0] = DeleteCtrl;
        } else if (referenceLayoutFixture_) {
            members[0] = GenericParameter;
        } else {
            return DISP_E_UNKNOWNNAME;
        }
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE Invoke(
        const DISPID member,
        REFIID,
        LCID,
        const WORD flags,
        DISPPARAMS* const parameters,
        VARIANT* result,
        EXCEPINFO*,
        UINT*) override {
        if ((flags & (DISPATCH_PROPERTYPUT | DISPATCH_PROPERTYPUTREF)) != 0) {
            if (parameters == nullptr || parameters->cArgs != 1) {
                return DISP_E_TYPEMISMATCH;
            }
            if (member == Text) {
                if (parameters->rgvarg[0].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                insertedText_.assign(
                    parameters->rgvarg[0].bstrVal,
                    SysStringLen(parameters->rgvarg[0].bstrVal));
                return S_OK;
            }
            if (referenceLayoutFixture_) {
                const VARIANT& value = parameters->rgvarg[0];
                if (member == GenericParameter || member == Properties) {
                    return S_OK;
                }
                if (member == FindString) {
                    if (value.vt != VT_BSTR) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    findString_.assign(
                        value.bstrVal,
                        SysStringLen(value.bstrVal));
                    return S_OK;
                }
                if (member == FindDirection || member == FindMatchCase ||
                    member == FindIgnoreMessage) {
                    return S_OK;
                }
                if (member == FaceNameHangul) {
                    if (value.vt != VT_BSTR) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    pendingReferenceFormat_.faceName.assign(
                        value.bstrVal,
                        SysStringLen(value.bstrVal));
                    return S_OK;
                }
                if (member == Bold) {
                    if (value.vt != VT_BOOL) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    pendingReferenceFormat_.bold = value.boolVal != VARIANT_FALSE;
                    return S_OK;
                }
                if (value.vt == VT_I4) {
                    if (member == CharacterHeight) {
                        pendingReferenceFormat_.height = value.lVal;
                        return S_OK;
                    }
                    if (member == TextColor) {
                        pendingReferenceFormat_.textColor = value.lVal;
                        return S_OK;
                    }
                    if (member == RatioHangul) {
                        pendingReferenceFormat_.widthRatio = value.lVal;
                        return S_OK;
                    }
                    if (member == SpacingHangul) {
                        pendingReferenceFormat_.letterSpacing = value.lVal;
                        return S_OK;
                    }
                    if (member == AlignType) {
                        pendingReferenceFormat_.alignment = value.lVal;
                        return S_OK;
                    }
                    if (member == LineSpacingType) {
                        pendingReferenceFormat_.lineSpacingType = value.lVal;
                        return S_OK;
                    }
                    if (member == LineSpacing) {
                        pendingReferenceFormat_.lineSpacing = value.lVal;
                        return S_OK;
                    }
                    if (member == LeftMargin) {
                        pendingReferenceFormat_.leftMargin = value.lVal;
                        return S_OK;
                    }
                    if (member == RightMargin) {
                        pendingReferenceFormat_.rightMargin = value.lVal;
                        return S_OK;
                    }
                    if (member == Indentation) {
                        pendingReferenceFormat_.indentation = value.lVal;
                        return S_OK;
                    }
                    if (member == PrevSpacing) {
                        pendingReferenceFormat_.previousSpacing = value.lVal;
                        return S_OK;
                    }
                    if (member == NextSpacing) {
                        pendingReferenceFormat_.nextSpacing = value.lVal;
                        return S_OK;
                    }
                }
            }
            if (parameters->rgvarg[0].vt != VT_I4) {
                return DISP_E_TYPEMISMATCH;
            }
            const LONG value = parameters->rgvarg[0].lVal;
            if (member == Apply) {
                pendingProfile_.styleId = value;
            } else if (member == AlignType) {
                pendingProfile_.alignment = value;
            } else if (member == LineSpacing) {
                pendingProfile_.lineSpacing = value;
            } else if (member == LeftMargin) {
                pendingProfile_.leftMargin = value;
            } else if (member == RightMargin) {
                pendingProfile_.rightMargin = value;
            } else if (member == Indentation) {
                pendingProfile_.indentation = value;
            } else if (member == PrevSpacing) {
                pendingProfile_.previousSpacing = value;
            } else if (member == NextSpacing) {
                pendingProfile_.nextSpacing = value;
            } else {
                return DISP_E_MEMBERNOTFOUND;
            }
            return S_OK;
        }
        CComVariant ignoredResult;
        if (result == nullptr && (flags & DISPATCH_METHOD) != 0 &&
            (member == Run || member == SetItem || member == ReturnVoid ||
             member == ReleaseScan || member == GetPos || member == KeyIndicator)) {
            result = &ignoredResult;
        }
        if (result == nullptr) {
            return E_POINTER;
        }
        VariantInit(result);
        if ((flags & DISPATCH_PROPERTYGET) != 0) {
            if (preflightSelectionFixture_ &&
                !preflightSelectionReadOccurred_ &&
                ((preflightTableControlSelection_ &&
                  member == CurSelectedCtrl &&
                  (selectionMode_ & 0x0F) == 4) ||
                 (!preflightTableControlSelection_ &&
                  member == ParentCtrl &&
                  (selectionMode_ & 0x0F) == 3))) {
                preflightSelectionControlCaptured_ = true;
            }
            if (member == HSet) {
                result->vt = VT_DISPATCH;
                result->pdispVal = parameterSet_;
                static_cast<void>(parameterSet_->AddRef());
                return S_OK;
            }
            if (member == GenericParameter && referenceLayoutFixture_) {
                result->vt = VT_DISPATCH;
                result->pdispVal = parameterSet_;
                static_cast<void>(parameterSet_->AddRef());
                return S_OK;
            }
            if (member == XHwpDocuments || member == ActiveDocument ||
                member == XHwpWindows || member == ActiveWindow ||
                member == HeadCtrl || member == LastCtrl ||
                member == HAction || member == CurSelectedCtrl ||
                member == HParameterSet ||
                member == XHwpMessageBox || member == Application ||
                 member == XHwpDocumentInfo || member == Properties) {
                if (member == HeadCtrl && checkpointHeadProbeActive_) {
                    checkpointHeadControlIndex_ = 0;
                }
                if (inspectionScopeFixture_ &&
                    (member == HeadCtrl || member == LastCtrl)) {
                    if (inspectionControls_.empty()) {
                        result->vt = VT_EMPTY;
                        return S_OK;
                    }
                    inspectionControlIndex_ = member == HeadCtrl
                        ? 0
                        : static_cast<LONG>(inspectionControls_.size() - 1);
                    if (member == HeadCtrl) {
                        ++inspectionHeadReads_;
                    } else {
                        ++inspectionLastReads_;
                    }
                }
                if (member == HeadCtrl &&
                    (!documentOpen_ ||
                     (referenceLayoutFixture_ && !referenceTableExists_))) {
                    result->vt = VT_EMPTY;
                    return S_OK;
                }
                if (member == CurSelectedCtrl && referenceLayoutFixture_ &&
                    !referenceTableExists_) {
                    result->vt = VT_EMPTY;
                    return S_OK;
                }
                if (member == ActiveDocument) {
                    activeDocumentFullNameReady_ = true;
                }
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == HStyle || member == HParaShape || member == HInsertText ||
                member == HCharShape || member == HInsertFile ||
                member == HFindReplace) {
                activeParameter_ = member;
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == HArrayFixture) {
                result->vt = VT_DISPATCH;
                result->pdispVal = parameterSet_;
                static_cast<void>(parameterSet_->AddRef());
                return S_OK;
            }
            if (member == DocumentId) {
                result->vt = VT_I4;
                result->lVal = documentId_;
                return S_OK;
            }
            if (member == WindowHandle) {
                result->vt = VT_I4;
                result->lVal = windowHandle_;
                return S_OK;
            }
            if (member == FullName) {
                if (requireActiveDocumentFullName_) {
                    activeDocumentFullNameReady_ = false;
                    ++activeDocumentFullNameReads_;
                }
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocStringLen(
                    fullName_.data(),
                    static_cast<UINT>(fullName_.size()));
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (referenceLayoutFixture_ &&
                (member == CellShape || member == ParentCtrl)) {
                if (member == ParentCtrl &&
                    (!referenceTableExists_ || ReferenceCurrentCell() == nullptr)) {
                    result->vt = VT_EMPTY;
                    return S_OK;
                }
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == CellShape || member == ParentCtrl) {
                result->vt = VT_I4;
                result->lVal = member == CellShape ? 1 : 0;
                return S_OK;
            }
            if (member == PageCount) {
                result->vt = VT_I4;
                result->lVal = documentOpen_ ? pageCount_ : 0L;
                return S_OK;
            }
            if (member == CurrentPage) {
                result->vt = VT_I4;
                result->lVal = inspectionScopeFixture_
                    ? inspectionCurrentPage_
                    : referenceLayoutFixture_ && referenceTableExists_ ? 1L : 0L;
                return S_OK;
            }
            if (member == IsModified) {
                result->vt = VT_BOOL;
                result->boolVal = modified_ ? VARIANT_TRUE : VARIANT_FALSE;
                return S_OK;
            }
            if (member == SelectionMode) {
                result->vt = VT_I4;
                result->lVal = selectionMode_;
                return S_OK;
            }
            if (member == CtrlID) {
                if (checkpointHeadProbeActive_) {
                    result->vt = VT_BSTR;
                    result->bstrVal = SysAllocString(L"secd");
                    return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
                }
                if (inspectionScopeFixture_ && InspectionControlReady()) {
                    ++inspectionIdentityReads_;
                    const std::wstring& type = inspectionControls_[
                        static_cast<size_t>(inspectionControlIndex_)].type;
                    result->vt = VT_BSTR;
                    result->bstrVal = SysAllocStringLen(
                        type.data(),
                        static_cast<UINT>(type.size()));
                    return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
                }
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocString(L"tbl");
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (checkpointHeadProbeActive_ && member == Next) {
                ++checkpointHeadControlIndex_;
                const bool hasNextSection =
                    checkpointLeadingSectionResidue_ &&
                    checkpointHeadSectionIsolated_ &&
                    checkpointHeadControlIndex_ == 1;
                if (!hasNextSection) {
                    checkpointHeadProbeActive_ = false;
                    result->vt = VT_EMPTY;
                    return S_OK;
                }
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (inspectionScopeFixture_ &&
                (member == Next || member == Prev)) {
                if (member == Next) {
                    ++inspectionNextReads_;
                    ++inspectionControlIndex_;
                } else {
                    ++inspectionPrevReads_;
                    --inspectionControlIndex_;
                }
                if (!InspectionControlReady()) {
                    result->vt = VT_EMPTY;
                    return S_OK;
                }
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == Next || member == Prev) {
                result->vt = VT_EMPTY;
                return S_OK;
            }
            if (inspectionScopeFixture_ && member == UserDesc &&
                InspectionControlReady()) {
                const std::wstring& description = inspectionControls_[
                    static_cast<size_t>(inspectionControlIndex_)].description;
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocStringLen(
                    description.data(),
                    static_cast<UINT>(description.size()));
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (member == SetID) {
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocString(L"TableCreation");
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (referenceLayoutFixture_ && member == FaceNameHangul) {
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocStringLen(
                    pendingReferenceFormat_.faceName.data(),
                    static_cast<UINT>(pendingReferenceFormat_.faceName.size()));
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (referenceLayoutFixture_ && member == Bold) {
                result->vt = VT_BOOL;
                result->boolVal = pendingReferenceFormat_.bold
                    ? VARIANT_TRUE
                    : VARIANT_FALSE;
                return S_OK;
            }
            if (referenceLayoutFixture_ &&
                (member == CharacterHeight || member == TextColor ||
                 member == RatioHangul || member == SpacingHangul ||
                 member == AlignType || member == LineSpacingType ||
                 member == LineSpacing || member == LeftMargin ||
                 member == RightMargin || member == Indentation ||
                 member == PrevSpacing || member == NextSpacing)) {
                result->vt = VT_I4;
                result->lVal = ReferenceFormatValue(pendingReferenceFormat_, member);
                return S_OK;
            }
            if (member == PageBreakBefore) {
                result->vt = VT_BOOL;
                result->boolVal =
                    checkpointDocumentFileFixture_ &&
                        checkpointResidueParagraph_ &&
                        checkpointHeadPageBreakBefore_
                    ? VARIANT_TRUE
                    : VARIANT_FALSE;
                return S_OK;
            }
            if (member >= Apply && member <= NextSpacing) {
                result->vt = VT_I4;
                result->lVal = ProfileValue(currentProfile_, member);
                return S_OK;
            }
        }
        if ((flags & DISPATCH_METHOD) != 0) {
            if (member == MutateTableTopology && referenceLayoutFixture_) {
                if (parameters != nullptr && parameters->cArgs != 0) {
                    return DISP_E_BADPARAMCOUNT;
                }
                const bool mutated = MergeFirstReferenceRow();
                referenceCallTopologyMutated_ = mutated;
                result->vt = VT_BOOL;
                result->boolVal = mutated ? VARIANT_TRUE : VARIANT_FALSE;
                return S_OK;
            }
            if (member == GetPageText && inspectionScopeFixture_) {
                if (parameters == nullptr || parameters->cArgs != 2 ||
                    parameters->rgvarg[0].vt != VT_I4 ||
                    parameters->rgvarg[1].vt != VT_I4) {
                    return DISP_E_TYPEMISMATCH;
                }
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocString(L"scoped page text");
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (member == KeyIndicator && referenceLayoutFixture_) {
                if (parameters == nullptr || parameters->cArgs != 8 ||
                    parameters->rgvarg[0].vt != (VT_BSTR | VT_BYREF) ||
                    parameters->rgvarg[0].pbstrVal == nullptr) {
                    return DISP_E_TYPEMISMATCH;
                }
                const ReferenceCell* const cell = ReferenceCurrentCell();
                const std::wstring indicator = cell == nullptr
                    ? L""
                    : L"\uD45C(" + cell->address + L")";
                *parameters->rgvarg[0].pbstrVal = SysAllocStringLen(
                    indicator.data(),
                    static_cast<UINT>(indicator.size()));
                return *parameters->rgvarg[0].pbstrVal != nullptr
                    ? S_OK
                    : E_OUTOFMEMORY;
            }
            if ((member == HwpLineType || member == HwpLineWidth) &&
                referenceLayoutFixture_) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring value(parameters->rgvarg[0].bstrVal);
                result->vt = VT_I4;
                if (member == HwpLineWidth) {
                    result->lVal = value == L"0.12mm"
                        ? 12L
                        : value == L"0.3mm"
                        ? 30L
                        : 99L;
                    return S_OK;
                }
                const std::vector<std::wstring> lineTypes{
                    L"None",
                    L"Solid",
                    L"Dash",
                    L"Dot",
                    L"DashDot",
                    L"DashDotDot",
                    L"LongDash",
                    L"Circle",
                    L"DoubleSlim",
                    L"SlimThick",
                    L"ThickSlim",
                    L"SlimThickSlim",
                };
                const auto found =
                    std::find(lineTypes.begin(), lineTypes.end(), value);
                result->lVal = found == lineTypes.end()
                    ? -1L
                    : static_cast<LONG>(
                          std::distance(lineTypes.begin(), found));
                return S_OK;
            }
            if (member == GenericParameter && referenceLayoutFixture_) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring value(parameters->rgvarg[0].bstrVal);
                result->vt = VT_I4;
                result->lVal = value == L"Left"
                    ? 1L
                    : value == L"Center"
                    ? 3L
                    : value == L"Right"
                    ? 2L
                    : 0L;
                return S_OK;
            }
            if (member == FindItem) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_I4) {
                    return DISP_E_TYPEMISMATCH;
                }
                pendingDocumentId_ = parameters->rgvarg[0].lVal;
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == SetActiveDocument) {
                if (parameters != nullptr && parameters->cArgs != 0) {
                    return DISP_E_BADPARAMCOUNT;
                }
                if (pendingDocumentId_ <= 0) {
                    return E_INVALIDARG;
                }
                documentId_ = pendingDocumentId_;
                pendingDocumentId_ = 0;
                result->vt = VT_EMPTY;
                return S_OK;
            }
            if (member == GetPos) {
                if (parameters == nullptr || parameters->cArgs != 3 ||
                    parameters->rgvarg[0].vt != (VT_I4 | VT_BYREF) ||
                    parameters->rgvarg[1].vt != (VT_I4 | VT_BYREF) ||
                    parameters->rgvarg[2].vt != (VT_I4 | VT_BYREF) ||
                    parameters->rgvarg[0].plVal == nullptr ||
                    parameters->rgvarg[1].plVal == nullptr ||
                    parameters->rgvarg[2].plVal == nullptr) {
                    return DISP_E_TYPEMISMATCH;
                }
                if (firstWriteReadbackFixture_ && insertTextExecutions_ > 0 &&
                    !firstWriteReadbackFailed_) {
                    firstWriteReadbackFailed_ = true;
                    return E_FAIL;
                }
                const ReferenceCell* const cell = ReferenceCurrentCell();
                const bool staleEmptySelection =
                    emptyCellTextFixture_ && emptyCellSelectAll_ &&
                    cell != nullptr && cell->text.empty();
                *parameters->rgvarg[2].plVal =
                    staleEmptySelection ? 0L : currentList_;
                *parameters->rgvarg[1].plVal =
                    staleEmptySelection ? 0L : currentParagraph_;
                *parameters->rgvarg[0].plVal =
                    staleEmptySelection ? 0L : currentCharacter_;
                return S_OK;
            }
            if (member == Save) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_BOOL) {
                    return DISP_E_TYPEMISMATCH;
                }
                saveCalledWithTrue_ = parameters->rgvarg[0].boolVal == VARIANT_TRUE;
                lifecycleCalls_.push_back(L"Save");
                if (saveClearsModified_) {
                    modified_ = false;
                }
                result->vt = VT_BOOL;
                result->boolVal = saveReturnsTrue_ ? VARIANT_TRUE : VARIANT_FALSE;
                return S_OK;
            }
            if (member == SaveAs) {
                if (parameters == nullptr || parameters->cArgs != 3 ||
                    parameters->rgvarg[0].vt != VT_BSTR ||
                    parameters->rgvarg[1].vt != VT_BSTR ||
                    parameters->rgvarg[2].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring options(parameters->rgvarg[0].bstrVal);
                const std::wstring format(parameters->rgvarg[1].bstrVal);
                const std::wstring path(parameters->rgvarg[2].bstrVal);
                if (checkpointDocumentFileFixture_) {
                    // The engine streams the open document to disk. This is the
                    // copy a restore takes before it deletes anything, so it has
                    // to be a real file: the failure path is judged on whether
                    // it is still there afterwards.
                    std::ofstream copy(
                        std::filesystem::path(path),
                        std::ios::binary | std::ios::trunc);
                    constexpr char body[] = "HWP ROLLBACK COPY";
                    copy.write(body, static_cast<std::streamsize>(sizeof(body) - 1));
                    copy.close();
                    result->vt = VT_BOOL;
                    result->boolVal = copy.good() ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                saveAsCalledWithArguments_ =
                    path == lifecyclePath_ &&
                    format == lifecycleFormat_ &&
                    options.empty();
                const bool saved =
                    lifecycleRecovered_ && saveAsCalledWithArguments_;
                if (saved) {
                    fullName_ = path;
                    documentOpen_ = true;
                    modified_ = false;
                }
                lifecycleCalls_.push_back(L"SaveAs");
                result->vt = VT_BOOL;
                result->boolVal = saved ? VARIANT_TRUE : VARIANT_FALSE;
                return S_OK;
            }
            if (member == Clear) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_I2) {
                    return DISP_E_TYPEMISMATCH;
                }
                ++clearCallCount_;
                if (checkpointDocumentFileFixture_) {
                    ++checkpointClearCallCount_;
                    if (checkpointClearFails_) {
                        return E_FAIL;
                    }
                }
                clearCalledWithDiscard_ = parameters->rgvarg[0].iVal == 1;
                lifecycleCalls_.push_back(L"Clear");
                documentOpen_ = false;
                modified_ = false;
                fullName_.clear();
                lifecycleMismatchActive_ = false;
                result->vt = VT_EMPTY;
                return S_OK;
            }
            if (member == Open) {
                if (parameters == nullptr || parameters->cArgs != 3 ||
                    parameters->rgvarg[0].vt != VT_BSTR ||
                    parameters->rgvarg[1].vt != VT_BSTR ||
                    parameters->rgvarg[2].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring options(parameters->rgvarg[0].bstrVal);
                const std::wstring format(parameters->rgvarg[1].bstrVal);
                const std::wstring path(parameters->rgvarg[2].bstrVal);
                const bool checkpointOpen =
                    checkpointDocumentFileFixture_ &&
                    path == checkpointUserDocumentPath_ &&
                    format == L"HWP" &&
                    options == L"lock:FALSE";
                openCalledWithArguments_ =
                    checkpointOpen || (
                        path == lifecyclePath_ &&
                        format == lifecycleFormat_ &&
                        options == L"lock:FALSE");
                lifecycleCalls_.push_back(L"Open");
                const size_t checkpointOpenOrdinal = checkpointOpen
                    ? ++checkpointOpenCount_
                    : 0;
                const bool opened = openReturnsTrue_ && openCalledWithArguments_ &&
                    !(checkpointRollbackOpenFails_ &&
                      checkpointOpenOrdinal == 2);
                if (opened) {
                    documentOpen_ = true;
                    modified_ = false;
                    fullName_ = checkpointOpen ? path : lifecycleReopenedPath_;
                    if (checkpointOpen) {
                        const bool target = checkpointOpenOrdinal == 1;
                        checkpointDocumentBlock_ = target
                            ? L"CHECKPOINT_TARGET"
                            : L"CHECKPOINT_ROLLBACK";
                        pageCount_ = target
                            ? (checkpointTargetPageMismatch_
                                ? kCheckpointDocumentPages - 1
                                : kCheckpointDocumentPages)
                            : 2;
                    }
                    lifecycleMismatchActive_ =
                        lifecycleMismatchAfterOpen_;
                }
                result->vt = VT_BOOL;
                result->boolVal =
                    opened ? VARIANT_TRUE : VARIANT_FALSE;
                return S_OK;
            }
            if (member == ReturnBoolean) {
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == ReturnInteger) {
                result->vt = VT_I4;
                result->lVal = -42;
                return S_OK;
            }
            if (member == ReturnText) {
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocString(L"C:/Temp/BIN0001.png");
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (member == ReturnVoid) {
                return S_OK;
            }
            if (member == CreateAction) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring action(parameters->rgvarg[0].bstrVal);
                currentAction_ = action;
                if (action == L"AutoSpellRun" || action == L"ChangeRome") {
                    return S_OK;
                }
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == CreateSet) {
                if (parameters != nullptr && parameters->cArgs != 0 &&
                    !(parameters->cArgs == 1 && parameters->rgvarg[0].vt == VT_BSTR)) {
                    return DISP_E_BADPARAMCOUNT;
                }
                // A real engine answers CreateSet("ListParaPos") with a position
                // set whatever else is going on. Handing back the main dispatch
                // instead makes every selection read fail, and a caller that
                // checks the range it selected then cannot tell "the range is
                // not there" from "this harness cannot describe a range".
                if ((referenceLayoutFixture_ || checkpointRestoreFixture_ ||
                     checkpointDocumentFileFixture_) &&
                    parameters != nullptr && parameters->cArgs == 1 &&
                    std::wstring(parameters->rgvarg[0].bstrVal) == L"ListParaPos") {
                    result->vt = VT_DISPATCH;
                    result->pdispVal = new FakeListParaPosDispatch();
                    return S_OK;
                }
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == SetMessageBoxMode) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_I4) {
                    return DISP_E_TYPEMISMATCH;
                }
                const LONG requested = parameters->rgvarg[0].lVal;
                if (requested == kInitialMessageBoxMode &&
                    messageBoxMode_ != kInitialMessageBoxMode) {
                    ++messageBoxModeRestoreAttempts_;
                    if (failNextMessageBoxModeRestore_) {
                        failNextMessageBoxModeRestore_ = false;
                        return E_FAIL;
                    }
                }
                const LONG previous = messageBoxMode_;
                messageBoxMode_ = requested;
                autoYesMessageBoxModeUsed_ = autoYesMessageBoxModeUsed_ ||
                    messageBoxMode_ == 0x00011010;
                result->vt = VT_I4;
                result->lVal = previous;
                return S_OK;
            }
            if (member == SelectCtrl) {
                if (referenceLayoutFixture_) {
                    if (parameters == nullptr || parameters->cArgs != 2 ||
                        parameters->rgvarg[1].vt != VT_BSTR) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    const std::wstring requestedTable(parameters->rgvarg[1].bstrVal);
                    tableSelected_ = referenceTableExists_ &&
                        SelectReferenceTable(requestedTable);
                    if (preflightSelectionFixture_ && tableSelected_) {
                        if (preflightTableControlSelection_ &&
                            preflightSelectionControlCaptured_ &&
                            preflightSelectionReadOccurred_) {
                            preflightSelectionRestoreAttempted_ = true;
                            preflightSelectionRestored_ = true;
                        }
                        selectionMode_ = 4;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = tableSelected_ ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_FALSE;
                return S_OK;
            }
            if (member == DeleteCtrl) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_DISPATCH) {
                    return DISP_E_TYPEMISMATCH;
                }
                if (checkpointDocumentFileFixture_) {
                    const bool deleted =
                        checkpointLeadingSectionResidue_ &&
                        checkpointHeadControlIndex_ == 0;
                    if (deleted) {
                        checkpointLeadingSectionResidue_ = false;
                        ++checkpointLeadingSectionDeletes_;
                        pageCount_ = CheckpointDocumentFilePages();
                    }
                    result->vt = VT_BOOL;
                    result->boolVal =
                        deleted ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                if (referenceLayoutFixture_) {
                    const bool deleted = referenceTableExists_;
                    if (deleted) {
                        referenceTableExists_ = false;
                        tableSelected_ = false;
                        currentList_ = 0;
                        currentParagraph_ = 108;
                        currentCharacter_ = 0;
                        selectionMode_ = 0;
                        ++referenceControlDeletes_;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = deleted ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == GetAnchorPos) {
                if (inspectionScopeFixture_) {
                    if (parameters == nullptr || parameters->cArgs != 1 ||
                        parameters->rgvarg[0].vt != VT_I4) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    inspectionAnchorRoot_ = parameters->rgvarg[0].lVal == 2;
                }
                anchorRead_ = true;
                result->vt = VT_DISPATCH;
                result->pdispVal = this;
                static_cast<void>(AddRef());
                return S_OK;
            }
            if (member == Item) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring item(parameters->rgvarg[0].bstrVal);
                if (checkpointHeadProbeActive_) {
                    result->vt = VT_I4;
                    result->lVal = item == L"List"
                        ? 0L
                        : item == L"Para"
                        ? (checkpointLeadingSectionResidue_ &&
                                   checkpointHeadControlIndex_ == 0
                               ? 0L
                               : CheckpointContentBaseParagraph())
                        : item == L"Pos"
                        ? 0L
                        : 0L;
                    return item == L"List" || item == L"Para" || item == L"Pos"
                        ? S_OK
                        : DISP_E_MEMBERNOTFOUND;
                }
                if (inspectionScopeFixture_ && InspectionControlReady()) {
                    const InspectionControl& control = inspectionControls_[
                        static_cast<size_t>(inspectionControlIndex_)];
                    result->vt = VT_I4;
                    result->lVal = item == L"List"
                        ? control.page
                        : item == L"Para"
                        ? inspectionControlIndex_
                        : item == L"Pos"
                        ? 0L
                        : 0L;
                    return item == L"List" || item == L"Para" || item == L"Pos"
                        ? S_OK
                        : DISP_E_MEMBERNOTFOUND;
                }
                if (referenceLayoutFixture_) {
                    if (item == L"Cell") {
                        result->vt = VT_DISPATCH;
                        result->pdispVal = this;
                        static_cast<void>(AddRef());
                        return S_OK;
                    }
                    const ReferenceCell* const cell = ReferenceCurrentCell();
                    result->vt = VT_I4;
                    result->lVal = item == L"List"
                        ? 0L
                        : item == L"Para"
                        ? 108L
                        : item == L"Pos"
                        ? 0L
                        : item == L"Width" && cell != nullptr
                        ? cell->width
                        : item == L"Height" && cell != nullptr
                        ? cell->height
                        : 0L;
                    return item == L"List" || item == L"Para" || item == L"Pos" ||
                            item == L"Width" || item == L"Height"
                        ? S_OK
                        : DISP_E_MEMBERNOTFOUND;
                }
                result->vt = VT_I4;
                result->lVal = item == L"Para" ? 108L : (item == L"Height" ? 1L : 0L);
                return S_OK;
            }
            if (member == SetItem) {
                if (parameters == nullptr || parameters->cArgs != 2 ||
                    parameters->rgvarg[1].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                if (checkpointDocumentFileFixture_ &&
                    std::wstring(parameters->rgvarg[1].bstrVal) == L"KeepSection") {
                    if (parameters->rgvarg[0].vt != VT_I4) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    checkpointPendingKeepSection_ = parameters->rgvarg[0].lVal;
                }
                if (checkpointDocumentFileFixture_ &&
                    std::wstring(parameters->rgvarg[1].bstrVal) == L"FileName") {
                    if (parameters->rgvarg[0].vt != VT_BSTR) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    checkpointPendingFileName_.assign(
                        parameters->rgvarg[0].bstrVal,
                        SysStringLen(parameters->rgvarg[0].bstrVal));
                }
                if (currentAction_ == L"SetWithoutDefault") {
                    actionInputApplied_ =
                        parameters->rgvarg[0].vt == VT_BSTR &&
                        std::wstring(parameters->rgvarg[1].bstrVal) == L"FileName" &&
                        std::wstring(parameters->rgvarg[0].bstrVal) == L"C:\\probe\\fixture.dat";
                }
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == ItemExist) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                result->vt = VT_BOOL;
                result->boolVal =
                    std::wstring(parameters->rgvarg[0].bstrVal) == L"ReportedMissing"
                    ? VARIANT_FALSE
                    : VARIANT_TRUE;
                return S_OK;
            }
            if (member == SetCurMetatagName) {
                if (parameters == nullptr || parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == SetPos) {
                if (inspectionScopeFixture_ && parameters != nullptr &&
                    parameters->cArgs == 3 &&
                    parameters->rgvarg[0].vt == VT_I4 &&
                    parameters->rgvarg[1].vt == VT_I4 &&
                    parameters->rgvarg[2].vt == VT_I4) {
                    currentList_ = parameters->rgvarg[2].lVal;
                    currentParagraph_ = parameters->rgvarg[1].lVal;
                    currentCharacter_ = parameters->rgvarg[0].lVal;
                    if (currentList_ >= 1 && currentList_ <= pageCount_) {
                        inspectionCurrentPage_ = currentList_ - 1;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                if (referenceLayoutFixture_ && parameters != nullptr &&
                    parameters->cArgs == 3 &&
                    parameters->rgvarg[0].vt == VT_I4 &&
                    parameters->rgvarg[1].vt == VT_I4 &&
                    parameters->rgvarg[2].vt == VT_I4) {
                    const LONG list = parameters->rgvarg[2].lVal;
                    if (twoReferenceTables_ && list >= 648 && list <= 653) {
                        static_cast<void>(SelectReferenceTable(L"reference-table"));
                    } else if (twoReferenceTables_ && list >= 1648 && list <= 1653) {
                        static_cast<void>(SelectReferenceTable(L"reference-table-b"));
                    }
                    if (referenceTopologyChanged_ && list == 649 &&
                        referenceTopologyInspections_ < 2) {
                        staleTopologyCellPositionAttempted_ = true;
                    }
                    const bool positioned = list == 0 ||
                        (referenceTableExists_ &&
                         ReferenceCellByList(list) != nullptr);
                    if (positioned) {
                        currentList_ = list;
                        currentParagraph_ = parameters->rgvarg[1].lVal;
                        currentCharacter_ = parameters->rgvarg[0].lVal;
                        if (caretSelectionPending_) {
                            // Block selection is on, so moving the caret
                            // extends the selection from where it started
                            // instead of dropping it.
                            selectedEndList_ = currentList_;
                            selectedEndParagraph_ = currentParagraph_;
                            selectedEndCharacter_ = currentCharacter_;
                            selectionMode_ = 1;
                            caretSelectionPending_ = false;
                            ++caretSelectionsCompleted_;
                        } else {
                            selectionMode_ = 0;
                        }
                        tailSelection_ = false;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = positioned ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                if (atomicAppendFixture_ && parameters != nullptr && parameters->cArgs == 3 &&
                    parameters->rgvarg[0].vt == VT_I4 &&
                    parameters->rgvarg[1].vt == VT_I4 &&
                    parameters->rgvarg[2].vt == VT_I4) {
                    currentList_ = parameters->rgvarg[2].lVal;
                    currentParagraph_ = parameters->rgvarg[1].lVal;
                    currentCharacter_ = parameters->rgvarg[0].lVal;
                    tailSelection_ = false;
                }
                if (checkpointDocumentFileFixture_ && parameters != nullptr &&
                    parameters->cArgs == 3 &&
                    parameters->rgvarg[0].vt == VT_I4 &&
                    parameters->rgvarg[1].vt == VT_I4 &&
                    parameters->rgvarg[2].vt == VT_I4) {
                    currentList_ = parameters->rgvarg[2].lVal;
                    currentParagraph_ = parameters->rgvarg[1].lVal;
                    currentCharacter_ = parameters->rgvarg[0].lVal;
                    checkpointTailSelected_ = false;
                }
                positionedAfterAnchor_ = anchorRead_;
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == GetHeadingString) {
                // The number HWP draws in front of the paragraph. It occupies
                // no character cell, so it is not part of any range, but the
                // block readback still carries it.
                result->vt = VT_BSTR;
                result->bstrVal = SysAllocString(
                    automaticNumberReadback_ ? L"(2)" : L"");
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (member == SelectText) {
                if (parameters == nullptr || parameters->cArgs != 4) {
                    return DISP_E_BADPARAMCOUNT;
                }
                if (checkpointRestoreFixture_) {
                    selectedStartList_ = currentList_;
                    selectedStartParagraph_ = parameters->rgvarg[3].lVal;
                    selectedStartCharacter_ = parameters->rgvarg[2].lVal;
                    selectedEndList_ = currentList_;
                    selectedEndParagraph_ = parameters->rgvarg[1].lVal;
                    selectedEndCharacter_ = parameters->rgvarg[0].lVal;
                    checkpointBlockBodySelected_ =
                        checkpointBlockBodyPresent_ &&
                        selectedStartParagraph_ == 0 &&
                        selectedStartCharacter_ == 0 &&
                        selectedEndParagraph_ == 108 &&
                        selectedEndCharacter_ == 0;
                    selectionMode_ = 1;
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                if (checkpointDocumentFileFixture_) {
                    const LONG startParagraph = parameters->rgvarg[3].lVal;
                    const LONG startCharacter = parameters->rgvarg[2].lVal;
                    const LONG endParagraph = parameters->rgvarg[1].lVal;
                    const LONG endCharacter = parameters->rgvarg[0].lVal;
                    // A range that runs into a paragraph the document does not
                    // have cannot be selected. That refusal is what stops a
                    // one-paragraph document from being read as a document whose
                    // first paragraph is a leftover.
                    const bool inRange = endParagraph <= CheckpointLastParagraph();
                    if (inRange) {
                        selectedStartList_ = currentList_;
                        selectedStartParagraph_ = startParagraph;
                        selectedStartCharacter_ = startCharacter;
                        selectedEndList_ = currentList_;
                        selectedEndParagraph_ = endParagraph;
                        selectedEndCharacter_ = endCharacter;
                        selectionMode_ = 1;
                        checkpointHeadSelected_ =
                            checkpointResidueParagraph_ &&
                            checkpointResidueAtFront_ &&
                            startParagraph == 0 && startCharacter == 0 &&
                            endParagraph == 1 && endCharacter == 0;
                        checkpointBodySelected_ =
                            checkpointContentParagraphs_ > 0 &&
                            startParagraph == 0 && startCharacter == 0 &&
                            endParagraph == CheckpointContentEndParagraph() &&
                            endCharacter == kCheckpointParagraphLength;
                        checkpointWholeSelected_ =
                            startParagraph == 0 && startCharacter == 0 &&
                            endParagraph == CheckpointLastParagraph() &&
                            endCharacter ==
                                (checkpointResidueParagraph_ &&
                                         !checkpointResidueAtFront_
                                     ? 0
                                     : kCheckpointParagraphLength);
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = inRange ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                if (referenceLayoutFixture_) {
                    if (automaticNumberSelectTextRefuses_) {
                        // Measured in HWP 2024: a paragraph whose number is
                        // drawn automatically answers false here even for a
                        // range inside its body, and leaves no selection.
                        ++automaticNumberSelectTextRefusals_;
                        result->vt = VT_BOOL;
                        result->boolVal = VARIANT_FALSE;
                        return S_OK;
                    }
                    selectedStartList_ = currentList_;
                    selectedStartParagraph_ = parameters->rgvarg[3].lVal;
                    selectedStartCharacter_ = parameters->rgvarg[2].lVal;
                    selectedEndList_ = currentList_;
                    selectedEndParagraph_ = parameters->rgvarg[1].lVal;
                    selectedEndCharacter_ = parameters->rgvarg[0].lVal;
                    selectionMode_ = 1;
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                nativeTableRangeSelected_ = positionedAfterAnchor_;
                result->vt = VT_BOOL;
                result->boolVal = nativeTableRangeSelected_ ? VARIANT_TRUE : VARIANT_FALSE;
                return S_OK;
            }
            if (member == MovePos) {
                if (parameters == nullptr || parameters->cArgs != 3 ||
                    parameters->rgvarg[2].lVal != 201 ||
                    !scanStarted_ || scanReleased_) {
                    return E_INVALIDARG;
                }
                if (!textScanChunks_.empty() && scanStep_ > 0 &&
                    static_cast<size_t>(scanStep_) <= textScanChunks_.size()) {
                    const auto& chunk = textScanChunks_[static_cast<size_t>(scanStep_ - 1)];
                    currentList_ = chunk.list;
                    currentParagraph_ = chunk.paragraph;
                    currentCharacter_ = chunk.character;
                }
                selectionMode_ = 0;
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == InitScan) {
                if (parameters == nullptr || parameters->cArgs != 6) {
                    return DISP_E_BADPARAMCOUNT;
                }
                scanStarted_ = true;
                scanReleased_ = false;
                scanStep_ = 0;
                textLookupScan_ = parameters->rgvarg[4].lVal == 0x77 ||
                    parameters->rgvarg[4].lVal == 0x55;
                checkpointPresenceScan_ =
                    checkpointDocumentFileFixture_ && checkpointTextFileEmpty_;
                if (preflightSelectionFixture_ &&
                    preflightSelectionControlCaptured_) {
                    preflightSelectionReadOccurred_ = true;
                }
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == GetText) {
                if (!scanStarted_ || scanReleased_ || parameters == nullptr ||
                    parameters->cArgs != 1 ||
                    parameters->rgvarg[0].vt != (VT_BSTR | VT_BYREF) ||
                    parameters->rgvarg[0].pbstrVal == nullptr) {
                    return DISP_E_TYPEMISMATCH;
                }
                if (textLookupScan_ && !textScanChunks_.empty()) {
                    const size_t index = static_cast<size_t>(scanStep_++);
                    const bool hasChunk = index < textScanChunks_.size();
                    *parameters->rgvarg[0].pbstrVal = SysAllocString(
                        hasChunk ? textScanChunks_[index].text.c_str() : L"");
                    result->vt = VT_I4;
                    result->lVal = hasChunk ? textScanChunks_[index].state : 1;
                    return S_OK;
                }
                const bool content = scanStep_++ == 0;
                const ReferenceCell* const cell = referenceLayoutFixture_
                    ? ReferenceCurrentCell()
                    : nullptr;
                const std::wstring scanned = checkpointPresenceScan_
                    ? (checkpointContentParagraphs_ > 0 && !checkpointPictureOnly_
                        ? L"body-text"
                        : L"")
                    : referenceLayoutFixture_ && cell != nullptr
                    ? cell->text
                    : L"cell-text";
                *parameters->rgvarg[0].pbstrVal = SysAllocString(
                    content ? scanned.c_str() : L"");
                if (*parameters->rgvarg[0].pbstrVal == nullptr) {
                    return E_OUTOFMEMORY;
                }
                result->vt = VT_I4;
                result->lVal = content ? 2L : 1L;
                return S_OK;
            }
            if (member == ReleaseScan) {
                if (!scanStarted_) {
                    return E_UNEXPECTED;
                }
                scanReleased_ = true;
                if (textLookupScan_ && failTextScanRelease_) {
                    return E_FAIL;
                }
                return S_OK;
            }
            if (member == GetTextFile) {
                if (parameters == nullptr || parameters->cArgs != 2 ||
                    parameters->rgvarg[0].vt != VT_BSTR ||
                    parameters->rgvarg[1].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring options(parameters->rgvarg[0].bstrVal);
                const std::wstring format(parameters->rgvarg[1].bstrVal);
                if (checkpointDocumentFileFixture_ &&
                    checkpointSignatureCaptureUnavailable_ &&
                    format == L"TEXT" && options.empty()) {
                    return E_FAIL;
                }
                if (checkpointDocumentFileFixture_ &&
                    (checkpointTextFileEmpty_ ||
                     checkpointContentParagraphs_ == 0) &&
                    format == L"TEXT" && options.empty()) {
                    result->vt = VT_BSTR;
                    result->bstrVal = SysAllocString(L"");
                    return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
                }
                if (checkpointDocumentFileFixture_ && format == L"UNICODE" &&
                    options == L"saveblock:true") {
                    // What the leftover paragraph reads as, on either side: one
                    // paragraph break and nothing else. Any other selection reads
                    // as body text, which is what stops it from being deleted.
                    const wchar_t* const answer =
                        checkpointContentParagraphs_ == 0 &&
                                checkpointResidueParagraph_
                            ? L"\r\n"
                        : checkpointTailSelected_ ? checkpointTailAnswer_.c_str()
                        : checkpointHeadSelected_ ? checkpointHeadAnswer_.c_str()
                        : (selectionMode_ != 0 ? L"body-text" : L"");
                    result->vt = VT_BSTR;
                    result->bstrVal = SysAllocString(answer);
                    return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
                }
                if (referenceLayoutFixture_ && format == L"UNICODE" &&
                    options == L"saveblock:true") {
                    std::wstring selected;
                    if (tailSelection_) {
                        selected = atomicTail_;
                    } else if (selectionMode_ == 1) {
                        const ReferenceCell* const cell = ReferenceCellByList(
                            selectedStartList_);
                        if (cell != nullptr &&
                            selectedStartList_ == selectedEndList_) {
                            ++textScanReadbacks_;
                            const size_t first = ReferenceTextOffset(
                                cell->text,
                                selectedStartParagraph_,
                                selectedStartCharacter_);
                            const size_t second = ReferenceTextOffset(
                                cell->text,
                                selectedEndParagraph_,
                                selectedEndCharacter_);
                            const size_t start = (std::min)(first, second);
                            const size_t end = (std::max)(first, second);
                            selected = cell->text.substr(start, end - start);
                            if (automaticNumberReadback_ &&
                                !selected.empty()) {
                                // GetHeadingString plus one space, which is
                                // exactly what the block readback carries in
                                // front of an automatically numbered
                                // paragraph -- measured for selections that
                                // start inside the body as well.
                                selected.insert(0, L"(2) ");
                            }
                            if (findDisplayOnlyPrefix_ && !selected.empty()) {
                                // Drawn text that reaches the readback while
                                // the paragraph carries no automatic number,
                                // so GetHeadingString cannot account for it.
                                // Nothing can then line the readback up with
                                // the selected cells.
                                selected.insert(0, L"(Figure 1) ");
                            }
                        }
                    }
                    result->vt = VT_BSTR;
                    result->bstrVal = SysAllocStringLen(
                        selected.data(),
                        static_cast<UINT>(selected.size()));
                    return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
                }
                nativeTableBlockCopied_ = nativeTableRangeSelected_ &&
                    format == L"HWP" && options == L"saveblock:true";
                if (format == L"HWPML2X" && options.empty()) {
                    ++hwpmlSerializationAttempts_;
                    if (oversizeSerializationRefusalFixture_) {
                        result->vt = VT_BSTR;
                        result->bstrVal = SysAllocString(L"");
                        return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
                    }
                }
                result->vt = VT_BSTR;
                const bool unstableHwpml =
                    unstableSerializationMetadataFixture_ &&
                    format == L"HWPML2X" && options.empty();
                const bool unstableCaretHwpml =
                    unstableCaretMetadataFixture_ &&
                    format == L"HWPML2X" && options.empty();
                const wchar_t* const hwpml =
                    diagnosticSectionMismatchFixture_ &&
                    format == L"HWPML2X" && options.empty()
                    ? ((serializationReads_++ % 2 != 0)
                        ? diagnosticSectionAfterHwpml_.c_str()
                        : diagnosticSectionBeforeHwpml_.c_str())
                    : unstableHwpml &&
                    (serializationReads_++ % 2 != 0)
                    ? L"<HWPML><BINDATA Encoding=\"Base64\" Id=\"3\" "
                      L"Size=\"5360\">REENCODED_SAME_IMAGE</BINDATA>"
                      L"<CHARSHAPE TextColor=\"255\"/>"
                      L"<TEXT CharShape=\"1\">body-text</TEXT></HWPML>"
                    : unstableCaretHwpml &&
                      (serializationReads_++ % 2 != 0)
                    ? L"<HWPML><HEAD><DOCSETTING>"
                      L"<CARETPOS List=\"0\" Para=\"0\" Pos=\"0\"/>"
                      L"</DOCSETTING></HEAD>"
                      L"<BODY><PARAMETERSET Count=\"1\" SetId=\"537\">"
                      L"<ITEM ItemId=\"614\" Type=\"Set\">"
                      L"<PARAMETERSET Count=\"0\" SetId=\"614\"/>"
                      L"</ITEM></PARAMETERSET>"
                      L"<CHARSHAPE TextColor=\"255\"/>"
                      L"<TEXT CharShape=\"1\">body-text</TEXT></BODY></HWPML>"
                    : unstableCaretHwpml
                    ? L"<HWPML><HEAD><DOCSETTING>"
                      L"<CARETPOS List=\"26\" Para=\"0\" Pos=\"7\"/>"
                      L"</DOCSETTING></HEAD>"
                      L"<BODY><PARAMETERSET Count=\"1\" SetId=\"537\">"
                      L"<ITEM ItemId=\"16385\" Type=\"BinData\">0</ITEM>"
                      L"</PARAMETERSET>"
                      L"<CHARSHAPE TextColor=\"255\"/>"
                      L"<TEXT CharShape=\"1\">body-text</TEXT></BODY></HWPML>"
                    : L"<HWPML><BINDATA Encoding=\"Base64\" Id=\"3\" "
                      L"Size=\"5373\">UNCHANGED_IMAGE</BINDATA>"
                      L"<CHARSHAPE TextColor=\"255\"/>"
                      L"<TEXT CharShape=\"1\">body-text</TEXT></HWPML>";
                const wchar_t* const value = atomicAppendFixture_ && tailSelection_ &&
                    format == L"UNICODE" && options == L"saveblock:true"
                    ? atomicTail_.c_str()
                    : (checkpointRestoreFixture_ || checkpointDocumentFileFixture_) &&
                      format == L"HWP" && options.empty()
                    ? checkpointDocumentBlock_.c_str()
                    : nativeTableBlockCopied_
                    ? L"SFdQX05BVElWRV9UQUJMRQ=="
                    : (format == L"TEXT" && options.empty()
                    ? (lifecycleMismatchActive_
                        ? L"body-text-mismatch\tcell-text"
                        : L"body-text\tcell-text")
                        : (format == L"HWPML2X" && options.empty()
                            ? hwpml
                        : (format == L"HWP" && options.empty()
                            ? (lifecycleRecoveryBlockCaptured_
                                ? L"SERIALIZED_BODY_TABLE_RED_STYLE"
                                : L"")
                    : (format == L"UNICODE" && options == L"saveblock:true"
                        ? L"old"
                        : L""))));
                result->bstrVal = SysAllocString(value);
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
            if (member == GetSelectedPosBySet) {
                if (parameters == nullptr || parameters->cArgs != 2 ||
                    parameters->rgvarg[0].vt != VT_DISPATCH ||
                    parameters->rgvarg[1].vt != VT_DISPATCH) {
                    return DISP_E_TYPEMISMATCH;
                }
                if (checkpointDocumentFileFixture_ || checkpointRestoreFixture_) {
                    auto* const start = dynamic_cast<FakeListParaPosDispatch*>(
                        parameters->rgvarg[1].pdispVal);
                    auto* const end = dynamic_cast<FakeListParaPosDispatch*>(
                        parameters->rgvarg[0].pdispVal);
                    if (start == nullptr || end == nullptr) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    // Normally reported back exactly as asked for. When the
                    // fixture is told to misreport, the end moves -- which is
                    // the only way the endpoint comparison in SelectTextRange
                    // can be made to fail, and therefore the only way it is
                    // tested at all.
                    start->SetPosition(
                        selectedStartList_,
                        selectedStartParagraph_,
                        selectedStartCharacter_);
                    const bool checkpointHeadRange =
                        checkpointDocumentFileFixture_ &&
                        selectedStartList_ == 0 &&
                        selectedStartParagraph_ == 0 &&
                        selectedStartCharacter_ == 0 &&
                        selectedEndList_ == 0 &&
                        selectedEndParagraph_ == 1 &&
                        selectedEndCharacter_ == 0;
                    if (checkpointHeadRange) {
                        ++checkpointHeadRangeReportsAfterInsert_;
                    }
                    const bool misreportCheckpointHeadRange =
                        checkpointSelectionMisreports_ &&
                        checkpointHeadRange &&
                        checkpointHeadRangeReportsAfterInsert_ == 2;
                    end->SetPosition(
                        selectedEndList_,
                        misreportCheckpointHeadRange
                            ? selectedEndParagraph_ + 1
                            : selectedEndParagraph_,
                        selectedEndCharacter_);
                    result->vt = VT_BOOL;
                    result->boolVal =
                        selectionMode_ != 0 ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                if (referenceLayoutFixture_) {
                    auto* const start = dynamic_cast<FakeListParaPosDispatch*>(
                        parameters->rgvarg[1].pdispVal);
                    auto* const end = dynamic_cast<FakeListParaPosDispatch*>(
                        parameters->rgvarg[0].pdispVal);
                    if (start == nullptr || end == nullptr) {
                        return DISP_E_TYPEMISMATCH;
                    }
                    const bool emptyCellSelection =
                        emptyCellTextFixture_ && selectionMode_ == 0;
                    const bool selected = emptyCellSelection || selectionMode_ != 0;
                    const LONG baseSelectionMode = selectionMode_ & 0x0F;
                    if (baseSelectionMode == 1 ||
                        baseSelectionMode == 3 ||
                        emptyCellSelection) {
                        start->SetPosition(
                            selectedStartList_,
                            selectedStartParagraph_,
                            selectedStartCharacter_);
                        end->SetPosition(
                            selectedEndList_,
                            selectedEndParagraph_,
                            selectedEndCharacter_);
                    } else {
                        start->SetPosition(
                            selected ? currentList_ : 0L,
                            selected ? currentParagraph_ : 0L,
                            selected ? currentCharacter_ : 0L);
                        end->SetPosition(
                            selected ? currentList_ : 0L,
                            selected ? currentParagraph_ : 0L,
                            selected ? currentCharacter_ : 0L);
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = selected ? VARIANT_TRUE : VARIANT_FALSE;
                    return S_OK;
                }
                if (firstWriteReadbackFixture_) {
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_FALSE;
                    return S_OK;
                }
                result->vt = VT_BOOL;
                result->boolVal =
                    textDeletionFixture_ && !textSelectionActive_
                    ? VARIANT_FALSE
                    : VARIANT_TRUE;
                return S_OK;
            }
            if (member == SetTextFile) {
                if (parameters == nullptr || parameters->cArgs != 3 ||
                    parameters->rgvarg[0].vt != VT_BSTR ||
                    parameters->rgvarg[1].vt != VT_BSTR ||
                    parameters->rgvarg[2].vt != VT_BSTR) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring options(parameters->rgvarg[0].bstrVal);
                const std::wstring format(parameters->rgvarg[1].bstrVal);
                const std::wstring block(parameters->rgvarg[2].bstrVal);
                if (checkpointRestoreFixture_ &&
                    format == L"HWP" && options == L"insertfile" &&
                    (block == L"CHECKPOINT_TARGET" ||
                     block == L"CHECKPOINT_ROLLBACK")) {
                    ++checkpointSetAttempts_;
                    if (checkpointSetTextFileFails_) {
                        result->vt = VT_I4;
                        result->lVal = 0;
                        return S_OK;
                    }
                    checkpointDocumentBlock_ = block;
                    checkpointBlockBodyPresent_ = true;
                    checkpointBlockBodySelected_ = false;
                    pageCount_ = block == L"CHECKPOINT_TARGET"
                        ? (checkpointWrongTargetPageCount_ ? 3L : 4L)
                        : 2L;
                    result->vt = VT_I4;
                    result->lVal = 1;
                    return S_OK;
                }
                if (block == L"SERIALIZED_BODY_TABLE_RED_STYLE" &&
                    format == L"HWP" &&
                    options.empty()) {
                    lifecycleRecovered_ = true;
                    lifecycleMismatchActive_ = false;
                    documentOpen_ = true;
                    modified_ = true;
                    result->vt = VT_I4;
                    result->lVal = 1;
                    return S_OK;
                }
                nativeTableBlockPasted_ = nativeTableBlockCopied_ &&
                    block == L"SFdQX05BVElWRV9UQUJMRQ==" &&
                    format == L"HWP" && options == L"insertfile";
                if (nativeTableBlockPasted_) {
                    currentProfile_ = sourceProfile_;
                }
                pasteUsedSourceAnchorFormat_ = nativeTableBlockPasted_ &&
                    currentProfile_ == sourceProfile_;
                result->vt = VT_I4;
                result->lVal = nativeTableBlockPasted_ ? 1L : 0L;
                return S_OK;
            }
            if (member == Run) {
                if (parameters == nullptr || parameters->cArgs > 1 ||
                    (parameters->cArgs == 1 && parameters->rgvarg[0].vt != VT_BSTR)) {
                    return DISP_E_TYPEMISMATCH;
                }
                const std::wstring action = parameters->cArgs == 0
                    ? currentAction_
                    : std::wstring(parameters->rgvarg[0].bstrVal);
                runActions_.push_back(action);
                if (action == L"SelectCtrlFront" && failSelectCtrlFrontWithHresult_) {
                    return E_FAIL;
                }
                if (referenceLayoutFixture_) {
                    result->vt = VT_BOOL;
                    result->boolVal = RunReferenceLayoutAction(action)
                        ? VARIANT_TRUE
                        : VARIANT_FALSE;
                    return S_OK;
                }
                if (checkpointDocumentFileFixture_) {
                    if (action == L"Undo") {
                        result->vt = VT_BOOL;
                        if (checkpointEngineUndoRestores_) {
                            checkpointDocumentBlock_ = L"CHECKPOINT_TARGET";
                            checkpointContentParagraphs_ =
                                kCheckpointContentParagraphs;
                            checkpointResidueParagraph_ = false;
                            checkpointLeadingSectionResidue_ = false;
                            checkpointFlattenedPenalty_ = false;
                            pageCount_ = kCheckpointDocumentPages;
                            currentList_ = 0;
                            currentParagraph_ = 0;
                            currentCharacter_ = 0;
                            selectionMode_ = 0;
                            result->boolVal = VARIANT_TRUE;
                        } else if (checkpointEngineUndoWrongStep_) {
                            checkpointDocumentBlock_ =
                                L"WRONG_HISTORY_STEP";
                            pageCount_ = kCheckpointDocumentPages - 1;
                            result->boolVal = VARIANT_TRUE;
                        } else {
                            result->boolVal = VARIANT_FALSE;
                        }
                        return S_OK;
                    }
                    if (action == L"Redo" &&
                        checkpointEngineUndoWrongStep_) {
                        checkpointDocumentBlock_ = L"CHECKPOINT_TARGET";
                        checkpointContentParagraphs_ =
                            kCheckpointContentParagraphs;
                        checkpointResidueParagraph_ = false;
                        checkpointLeadingSectionResidue_ = false;
                        checkpointFlattenedPenalty_ = false;
                        pageCount_ = kCheckpointDocumentPages;
                        currentList_ = 0;
                        currentParagraph_ = 0;
                        currentCharacter_ = 0;
                        selectionMode_ = 0;
                        result->vt = VT_BOOL;
                        result->boolVal = VARIANT_TRUE;
                        return S_OK;
                    }
                    if (action == L"MoveDocBegin") {
                        currentList_ = 0;
                        currentParagraph_ = 0;
                        currentCharacter_ = 0;
                        checkpointWholeSelected_ = false;
                        checkpointBodySelected_ = false;
                        checkpointTailSelected_ = false;
                        checkpointHeadSelected_ = false;
                        selectionMode_ = 0;
                    } else if (action == L"SelectAll") {
                        checkpointWholeSelected_ = true;
                        checkpointTailSelected_ = false;
                        checkpointHeadSelected_ = false;
                    } else if (action == L"Delete" &&
                               (checkpointTailSelected_ || checkpointHeadSelected_)) {
                        // Only the leftover paragraph is inside this selection,
                        // so only the page it was costing goes with it. Taking
                        // it off the front is what pulls the whole document back
                        // up one paragraph.
                        checkpointHeadResidueDeleted_ =
                            checkpointHeadResidueDeleted_ || checkpointHeadSelected_;
                        checkpointResidueParagraph_ = false;
                        checkpointTailSelected_ = false;
                        checkpointHeadSelected_ = false;
                        selectionMode_ = 0;
                        pageCount_ = CheckpointDocumentFilePages();
                        currentParagraph_ = CheckpointContentEndParagraph();
                        currentCharacter_ = kCheckpointParagraphLength;
                    } else if (action == L"Delete" && checkpointBodySelected_) {
                        checkpointBodySelected_ = false;
                        ++checkpointBodyDeleteAttempts_;
                        selectionMode_ = 0;
                        if (checkpointDeleteLeavesTextAttempts_ > 0) {
                            --checkpointDeleteLeavesTextAttempts_;
                            checkpointContentParagraphs_ = 1;
                            checkpointResidueParagraph_ = false;
                            pageCount_ = kCheckpointDocumentPages;
                            currentParagraph_ = 0;
                            currentCharacter_ = kCheckpointParagraphLength;
                        } else {
                            checkpointContentParagraphs_ = 0;
                            checkpointResidueParagraph_ = true;
                            checkpointLeadingSectionResidue_ = false;
                            checkpointFlattenedPenalty_ = false;
                            pageCount_ = 1;
                            currentParagraph_ = 0;
                            currentCharacter_ = 0;
                        }
                    } else if (action == L"Delete" && checkpointWholeSelected_) {
                        checkpointWholeSelected_ = false;
                        checkpointContentParagraphs_ = 0;
                        checkpointResidueParagraph_ = true;
                        checkpointLeadingSectionResidue_ = false;
                        checkpointFlattenedPenalty_ = false;
                        pageCount_ = 1;
                        currentParagraph_ = 0;
                        currentCharacter_ = 0;
                    } else if (action == L"MoveDocEnd") {
                        checkpointTailSelected_ = false;
                        if (checkpointCrossListEndAttempts_ > 0) {
                            --checkpointCrossListEndAttempts_;
                            currentList_ = 1;
                        } else {
                            currentList_ = 0;
                        }
                        if (checkpointResidueParagraph_ && !checkpointResidueAtFront_) {
                            currentParagraph_ = CheckpointLastParagraph();
                            currentCharacter_ = 0;
                        } else if (checkpointContentParagraphs_ == 0) {
                            currentParagraph_ = 0;
                            currentCharacter_ = 0;
                        } else {
                            currentParagraph_ = CheckpointContentEndParagraph();
                            currentCharacter_ = kCheckpointParagraphLength;
                        }
                    } else if (action == L"MoveSelDocEnd") {
                        checkpointTailSelected_ =
                            checkpointResidueParagraph_ &&
                            ((checkpointInsertCaretBeforeEnd_ &&
                              checkpointResidueAtFront_ &&
                              currentList_ == 0 &&
                              currentParagraph_ ==
                                  CheckpointContentBaseParagraph() &&
                              currentCharacter_ == 0) ||
                             (!checkpointResidueAtFront_ &&
                              currentList_ == 0 &&
                              currentParagraph_ ==
                                  CheckpointContentEndParagraph() &&
                              currentCharacter_ ==
                                  kCheckpointParagraphLength));
                    } else if (action == L"Cancel") {
                        checkpointTailSelected_ = false;
                        checkpointHeadSelected_ = false;
                        selectionMode_ = 0;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                if (checkpointRestoreFixture_) {
                    if (action == L"MoveDocBegin") {
                        currentList_ = 0;
                        currentParagraph_ = 0;
                        currentCharacter_ = 0;
                        selectionMode_ = 0;
                    } else if (action == L"MoveDocEnd") {
                        currentList_ = 0;
                        currentParagraph_ =
                            checkpointBlockBodyPresent_ ? 108L : 0L;
                        currentCharacter_ = 0;
                        selectionMode_ = 0;
                    } else if (
                        action == L"Delete" &&
                        checkpointBlockBodySelected_) {
                        checkpointBlockBodySelected_ = false;
                        checkpointBlockBodyPresent_ = false;
                        pageCount_ = 1;
                        currentParagraph_ = 0;
                        currentCharacter_ = 0;
                        selectionMode_ = 0;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                if (action == L"Delete" && textDeletionFixture_ &&
                    textSelectionActive_) {
                    textSelectionActive_ = false;
                    selectionMode_ = 0;
                    currentList_ = 0;
                    currentParagraph_ = 108;
                    currentCharacter_ = 0;
                    ++textDeletionActions_;
                } else if (action == L"MoveDocEnd" && atomicAppendFixture_) {
                    currentList_ = 0;
                    currentParagraph_ = 108;
                    currentCharacter_ = 0;
                    tailSelection_ = false;
                } else if (action == L"MoveSelDocEnd" && atomicAppendFixture_) {
                    tailSelection_ = currentList_ == 0 && currentParagraph_ == 108 &&
                        currentCharacter_ == 0;
                } else if (action == L"Delete" && atomicAppendFixture_ && tailSelection_) {
                    if (failAtomicTailDelete_) {
                        return E_ACCESSDENIED;
                    }
                    atomicTail_.clear();
                    pageCount_ = 1;
                    tailSelection_ = false;
                    ++atomicTailDeletes_;
                } else if (action == L"Cancel" && atomicAppendFixture_) {
                    tailSelection_ = false;
                } else if (action == L"SelectCtrlFront") {
                    frontSelectedAfterPosition_ = positionedAfterAnchor_;
                } else if (action == L"BreakPage") {
                    ranBreakPage_ = true;
                    if (atomicAppendFixture_) {
                        atomicTail_.push_back(L'\f');
                        pageCount_ = 2;
                        UpdateAtomicTailMaximumOccurrences();
                    }
                } else if (action == L"Copy") {
                    clipboardTableTransferUsed_ = true;
                    copyUsedAnchorSelection_ = anchorRead_ &&
                        positionedAfterAnchor_ && frontSelectedAfterPosition_;
                    copyCompleted_ = true;
                    currentProfile_ = destinationProfile_;
                } else if (action == L"Paste") {
                    clipboardTableTransferUsed_ = true;
                    pasteUsedSourceAnchorFormat_ = currentProfile_ == sourceProfile_;
                }
                result->vt = VT_BOOL;
                result->boolVal =
                    action == L"Copy" || action == L"Paste" ||
                        (action == L"SelectCtrlFront" && failSelectCtrlFront_)
                    ? VARIANT_FALSE
                    : VARIANT_TRUE;
                return S_OK;
            }
            if (member == GetDefault) {
                if (checkpointDocumentFileFixture_ && parameters != nullptr &&
                    parameters->cArgs >= 2 &&
                    parameters->rgvarg[1].vt == VT_BSTR) {
                    currentAction_.assign(
                        parameters->rgvarg[1].bstrVal,
                        SysStringLen(parameters->rgvarg[1].bstrVal));
                    // Every attempt starts from the engine's defaults, so the
                    // KeepSection the next Execute reads is the one this restore
                    // asked for and not the one the previous attempt left.
                    if (currentAction_ == L"InsertFile") {
                        checkpointHeadRangeReportsAfterInsert_ = 0;
                        checkpointPendingKeepSection_ = -1;
                        checkpointPendingFileName_.clear();
                    } else if (currentAction_ == L"ParagraphShape") {
                        checkpointHeadProbeActive_ = true;
                        checkpointHeadControlIndex_ = 0;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                if (currentAction_ == L"SetWithoutDefault") {
                    return E_FAIL;
                }
                if (referenceLayoutFixture_) {
                    if (parameters != nullptr && parameters->cArgs >= 2 &&
                        parameters->rgvarg[1].vt == VT_BSTR) {
                        currentAction_.assign(
                            parameters->rgvarg[1].bstrVal,
                            SysStringLen(parameters->rgvarg[1].bstrVal));
                    }
                    if (pendingTextFormatReadback_ &&
                        (currentAction_ == L"CharShape" ||
                         currentAction_ == L"ParagraphShape")) {
                        pendingTextFormatReadback_ = false;
                        ++textFormatReadbacks_;
                    }
                    parameterSet_->ClearGenericValues();
                    if (rangeFormatReadbackFixture_ &&
                        currentAction_ == L"CellFill") {
                        parameterSet_->SetGenericLong(
                            L"FillColor",
                            rangeCellFillValue_);
                    }
                    const ReferenceCell* const cell = ReferenceFormattingCell();
                    pendingReferenceFormat_ = cell == nullptr
                        ? BodyReferenceFormat()
                        : cell->format;
                    if (currentAction_ == L"CellBorderFill" &&
                        referenceEdgeExecutions_ > 0) {
                        PopulateReferenceBorderReadback(cell);
                        ++referenceBorderReadbacks_;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                pendingProfile_ = currentProfile_;
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == Execute) {
                if (checkpointDocumentFileFixture_) {
                    if (parameters != nullptr && parameters->cArgs >= 2 &&
                        parameters->rgvarg[1].vt == VT_BSTR) {
                        currentAction_.assign(
                            parameters->rgvarg[1].bstrVal,
                            SysStringLen(parameters->rgvarg[1].bstrVal));
                    }
                    if (currentAction_ == L"InsertFile") {
                        // An insert that never said which way to treat the
                        // checkpoint's sections is not an insert this models.
                        if (checkpointPendingKeepSection_ < 0) {
                            return E_FAIL;
                        }
                        const LONG keepSection = checkpointPendingKeepSection_;
                        checkpointInsertKeepSections_.push_back(keepSection);
                        checkpointLastKeepSection_ = keepSection;
                        if (PendingCheckpointFileIsRollback() &&
                            !checkpointRollbackInsertFails_) {
                            checkpointContentParagraphs_ =
                                kCheckpointContentParagraphs;
                            checkpointResidueParagraph_ = false;
                            checkpointLeadingSectionResidue_ = false;
                            checkpointFlattenedPenalty_ = false;
                            pageCount_ = 2;
                            currentList_ = 0;
                            currentParagraph_ = CheckpointContentEndParagraph();
                            currentCharacter_ = kCheckpointParagraphLength;
                            checkpointWholeSelected_ = false;
                            checkpointBodySelected_ = false;
                            checkpointTailSelected_ = false;
                            checkpointHeadSelected_ = false;
                            selectionMode_ = 0;
                            result->vt = VT_BOOL;
                            result->boolVal = VARIANT_TRUE;
                            return S_OK;
                        }
                        if (checkpointTextLossInsertAttempts_ > 0) {
                            --checkpointTextLossInsertAttempts_;
                            checkpointContentParagraphs_ = 0;
                            checkpointResidueParagraph_ = false;
                            checkpointLeadingSectionResidue_ = false;
                            checkpointFlattenedPenalty_ = false;
                            pageCount_ = kCheckpointDocumentPages;
                            currentList_ = 0;
                            currentParagraph_ = 0;
                            currentCharacter_ = 0;
                            checkpointWholeSelected_ = false;
                            checkpointBodySelected_ = false;
                            checkpointTailSelected_ = false;
                            checkpointHeadSelected_ = false;
                            selectionMode_ = 0;
                            result->vt = VT_BOOL;
                            result->boolVal = VARIANT_TRUE;
                            return S_OK;
                        }
                        checkpointContentParagraphs_ = checkpointSingleParagraph_
                            ? 1L
                            : kCheckpointContentParagraphs;
                        // The paragraph the emptied document still had is beside
                        // the inserted content, not replaced by it -- unless this
                        // engine absorbs it, which is the document the trim must
                        // keep its hands off.
                        checkpointResidueParagraph_ = !checkpointAbsorbsResidue_;
                        checkpointLeadingSectionResidue_ =
                            checkpointResidueParagraph_ &&
                            checkpointResidueAtFront_;
                        checkpointFlattenedPenalty_ =
                            checkpointInsertNeverMatches_ ||
                            (checkpointSectionsNeedKeeping_ && keepSection == 0);
                        pageCount_ = CheckpointDocumentFilePages();
                        // MoveNextPos is not a reliable boundary on every live
                        // document, so the fixture can make it land before the
                        // checkpoint's end.
                        currentList_ = 0;
                        currentParagraph_ = checkpointInsertCaretBeforeEnd_
                            ? CheckpointContentBaseParagraph()
                            : CheckpointContentEndParagraph();
                        currentCharacter_ = checkpointInsertCaretBeforeEnd_
                            ? 0L
                            : kCheckpointParagraphLength;
                        checkpointWholeSelected_ = false;
                        checkpointBodySelected_ = false;
                        checkpointTailSelected_ = false;
                        checkpointHeadSelected_ = false;
                        selectionMode_ = 0;
                        result->vt = VT_BOOL;
                        result->boolVal = VARIANT_TRUE;
                        return S_OK;
                    }
                }
                if (referenceLayoutFixture_) {
                    if (parameters != nullptr && parameters->cArgs >= 2 &&
                        parameters->rgvarg[1].vt == VT_BSTR) {
                        currentAction_.assign(
                            parameters->rgvarg[1].bstrVal,
                            SysStringLen(parameters->rgvarg[1].bstrVal));
                    }
                    if (currentAction_ == L"TableCreate") {
                        referenceTableExists_ = true;
                        tableSelected_ = false;
                        pageCount_ = 2;
                        atomicTail_ = L"\f";
                        UpdateAtomicTailMaximumOccurrences();
                        SetReferenceCurrentCell(L"A1");
                    } else if (currentAction_ == L"TableInsertRightColumn") {
                        if (!InsertReferenceRightColumn()) {
                            return E_FAIL;
                        }
                    } else if (currentAction_ == L"CharShape") {
                        ReferenceCell* const cell = ReferenceFormattingCell();
                        if (cell != nullptr && !dropAppliedTextFormat_) {
                            ApplyReferenceCharacterFormat(
                                &cell->format,
                                pendingReferenceFormat_);
                        }
                        pendingTextFormatReadback_ = true;
                        ++textFormatActionsExecuted_;
                    } else if (currentAction_ == L"ParagraphShape") {
                        ReferenceCell* const cell = ReferenceFormattingCell();
                        if (cell != nullptr && !dropAppliedTextFormat_) {
                            ApplyReferenceParagraphFormat(
                                &cell->format,
                                pendingReferenceFormat_);
                        }
                        pendingTextFormatReadback_ = true;
                        ++textFormatActionsExecuted_;
                    } else if (currentAction_ == L"InsertText") {
                        if (failReferenceLayoutAfterFirstText_ &&
                            referenceTextInsertions_ == 1) {
                            return E_FAIL;
                        }
                        if (!InsertReferenceText(insertedText_)) {
                            return E_FAIL;
                        }
                        ++referenceTextInsertions_;
                    } else if (currentAction_ == L"CellBorderFill") {
                        ClearReferenceBorders();
                    } else if (currentAction_ == L"ForwardFind") {
                        ++findActionAttempts_;
                        const ReferenceCell* const cell =
                            ReferenceCellByList(currentList_);
                        result->vt = VT_BOOL;
                        result->boolVal = VARIANT_FALSE;
                        if (cell == nullptr || findString_.empty()) {
                            return S_OK;
                        }
                        const size_t from =
                            static_cast<size_t>((std::max)(0L, currentCharacter_));
                        const size_t hit = from > cell->text.size()
                            ? std::wstring::npos
                            : cell->text.find(findString_, from);
                        if (hit == std::wstring::npos) {
                            return S_OK;
                        }
                        // A find that hands back more than it matched. The
                        // surplus is ordinary body text in front of the
                        // literal, so it occupies cells of its own and a
                        // replacement across the whole selection would carry
                        // it away.
                        const size_t start = hit >= findSelectionSurplus_
                            ? hit - findSelectionSurplus_
                            : 0;
                        selectedStartList_ = currentList_;
                        selectedStartParagraph_ = 0;
                        selectedStartCharacter_ = static_cast<LONG>(start);
                        selectedEndList_ = currentList_;
                        selectedEndParagraph_ = 0;
                        selectedEndCharacter_ =
                            static_cast<LONG>(hit + findString_.size());
                        selectionMode_ = 1;
                        result->boolVal = VARIANT_TRUE;
                        return S_OK;
                    }
                    if (currentAction_ == L"CellZoneBorder") {
                        ++referenceEdgeExecutions_;
                        if (!dropReferenceEdges_) {
                            if (!ApplyReferenceVisibleEdge()) {
                                return E_FAIL;
                            }
                            ++referenceEdgeApplications_;
                        }
                    } else if (currentAction_ == L"CellBorder") {
                        if (rangeFormatReadbackFixture_) {
                            rangeCellBorderCoveredBlock_ = selectionMode_ == 3 &&
                                currentList_ != referenceSelectionAnchorList_;
                        }
                    } else if (currentAction_ == L"TablePropertyDialog") {
                        if (rangeFormatReadbackFixture_) {
                            rangePaddingCoveredBlock_ = selectionMode_ == 3 &&
                                currentList_ != referenceSelectionAnchorList_;
                        }
                    } else if (currentAction_ == L"CellFill") {
                        if (rangeFormatReadbackFixture_) {
                            LONG requested = 0;
                            if (!parameterSet_->TryGetLong(
                                    L"FillColor",
                                    &requested)) {
                                return E_FAIL;
                            }
                            rangeCellFillCoveredBlock_ = selectionMode_ == 3 &&
                                currentList_ != referenceSelectionAnchorList_;
                            if (!dropAppliedRangeFormat_) {
                                rangeCellFillValue_ = requested;
                            }
                        }
                        ++referenceFillApplications_;
                    }
                    result->vt = VT_BOOL;
                    result->boolVal = VARIANT_TRUE;
                    return S_OK;
                }
                if (currentAction_ == L"SetWithoutDefault" && !actionInputApplied_) {
                    return E_FAIL;
                }
                if (currentAction_ == L"RaiseStructured") {
                    RaiseException(0xE0001234UL, 0, 0, nullptr);
                }
                if (activeParameter_ == HStyle) {
                    currentProfile_.styleId = pendingProfile_.styleId;
                } else if (activeParameter_ == HParaShape) {
                    currentProfile_.alignment = pendingProfile_.alignment;
                    currentProfile_.lineSpacing = pendingProfile_.lineSpacing;
                    currentProfile_.leftMargin = pendingProfile_.leftMargin;
                    currentProfile_.rightMargin = pendingProfile_.rightMargin;
                    currentProfile_.indentation = pendingProfile_.indentation;
                    currentProfile_.previousSpacing = pendingProfile_.previousSpacing;
                    currentProfile_.nextSpacing = pendingProfile_.nextSpacing;
                } else if (activeParameter_ == HInsertText) {
                    ++insertTextExecutions_;
                    if (atomicAppendFixture_) {
                        atomicTail_.append(insertedText_);
                        currentCharacter_ += static_cast<LONG>(insertedText_.size());
                        UpdateAtomicTailMaximumOccurrences();
                    }
                }
                result->vt = VT_BOOL;
                result->boolVal = VARIANT_TRUE;
                return S_OK;
            }
            if (member == GetCtrlInstId) {
                result->vt = VT_BSTR;
                if (inspectionScopeFixture_ && InspectionControlReady()) {
                    const std::wstring& instance = inspectionControls_[
                        static_cast<size_t>(inspectionControlIndex_)].instance;
                    result->bstrVal = SysAllocStringLen(
                        instance.data(),
                        static_cast<UINT>(instance.size()));
                } else {
                    result->bstrVal = SysAllocString(
                        referenceLayoutFixture_
                            ? (wrongReferenceControlIdentity_
                                ? L"unexpected-table" : selectedReferenceTableId_.c_str())
                            : L"captured-table");
                }
                return result->bstrVal != nullptr ? S_OK : E_OUTOFMEMORY;
            }
        }
        return DISP_E_MEMBERNOTFOUND;
    }

    HRESULT STDMETHODCALLTYPE EnumConnectionPoints(
        IEnumConnectionPoints**) override {
        return E_NOTIMPL;
    }

    HRESULT STDMETHODCALLTYPE FindConnectionPoint(
        REFIID interfaceId,
        IConnectionPoint** const connectionPoint) override {
        if (connectionPoint == nullptr) {
            return E_POINTER;
        }
        *connectionPoint = nullptr;
        if (interfaceId != kDispatchEventIid) {
            return E_NOINTERFACE;
        }
        *connectionPoint = static_cast<IConnectionPoint*>(this);
        static_cast<void>(AddRef());
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetConnectionInterface(IID* const interfaceId) override {
        if (interfaceId == nullptr) {
            return E_POINTER;
        }
        *interfaceId = kDispatchEventIid;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE GetConnectionPointContainer(
        IConnectionPointContainer** const container) override {
        if (container == nullptr) {
            return E_POINTER;
        }
        *container = static_cast<IConnectionPointContainer*>(this);
        static_cast<void>(AddRef());
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE Advise(IUnknown*, DWORD*) override {
        return E_NOTIMPL;
    }

    HRESULT STDMETHODCALLTYPE Unadvise(DWORD) override {
        return E_NOTIMPL;
    }

    HRESULT STDMETHODCALLTYPE EnumConnections(IEnumConnections**) override {
        return E_NOTIMPL;
    }

private:
    struct InspectionControl {
        std::wstring type;
        std::wstring instance;
        std::wstring description;
        LONG page = 0;
    };

    bool InspectionControlReady() const noexcept {
        return inspectionControlIndex_ >= 0 &&
            static_cast<size_t>(inspectionControlIndex_) <
                inspectionControls_.size();
    }

    struct ReferenceFormat {
        std::wstring faceName = L"Arial";
        LONG height = 900;
        bool bold = false;
        LONG textColor = 1315860;
        LONG widthRatio = 100;
        LONG letterSpacing = 0;
        LONG alignment = 1;
        LONG lineSpacingType = 0;
        LONG lineSpacing = 100;
        LONG leftMargin = 0;
        LONG rightMargin = 0;
        LONG indentation = 0;
        LONG previousSpacing = 0;
        LONG nextSpacing = 0;
    };

    struct ReferenceBorder {
        LONG type = 0;
        LONG width = 0;
        LONG color = 0;
    };

    struct ReferenceCell {
        std::wstring address;
        LONG listId = 0;
        LONG row = 0;
        LONG column = 0;
        LONG rowSpan = 1;
        LONG columnSpan = 1;
        LONG width = 0;
        LONG height = 0;
        bool active = true;
        std::wstring text;
        ReferenceFormat format;
        std::map<std::wstring, ReferenceBorder> borders;
    };

    size_t ReferenceTextOffset(
        const std::wstring& text,
        const LONG paragraph,
        const LONG character) const noexcept {
        const LONG requestedParagraph = (std::max)(0L, paragraph);
        size_t lineStart = 0;
        LONG currentParagraph = 0;
        while (currentParagraph < requestedParagraph && lineStart < text.size()) {
            const size_t lineBreak = text.find_first_of(L"\r\n", lineStart);
            if (lineBreak == std::wstring::npos) {
                return text.size();
            }
            lineStart = lineBreak + (
                text[lineBreak] == L'\r' && lineBreak + 1 < text.size() &&
                    text[lineBreak + 1] == L'\n'
                ? 2
                : 1);
            ++currentParagraph;
        }
        if (currentParagraph < requestedParagraph) {
            return text.size();
        }
        const size_t lineEnd = text.find_first_of(L"\r\n", lineStart);
        const size_t boundedEnd =
            lineEnd == std::wstring::npos ? text.size() : lineEnd;
        const LONG visibleCharacter = currentList_ == 651 && paragraph == 0 && character > textScanHiddenAt_
            ? (std::max)(textScanHiddenAt_, character - textScanHiddenPrefix_) : character;
        const size_t requestedCharacter = static_cast<size_t>((std::max)(0L, visibleCharacter));
        return lineStart + (std::min)(requestedCharacter, boundedEnd - lineStart);
    }

    static void ReferenceTextPositionAtOffset(
        const std::wstring& text,
        const size_t requestedOffset,
        LONG* const paragraph,
        LONG* const character) noexcept {
        const size_t offset = (std::min)(requestedOffset, text.size());
        LONG currentParagraph = 0;
        LONG currentCharacter = 0;
        for (size_t index = 0; index < offset; ++index) {
            if (text[index] == L'\r' &&
                index + 1 < offset &&
                text[index + 1] == L'\n') {
                ++currentParagraph;
                currentCharacter = 0;
                ++index;
            } else if (text[index] == L'\r' || text[index] == L'\n') {
                ++currentParagraph;
                currentCharacter = 0;
            } else {
                ++currentCharacter;
            }
        }
        *paragraph = currentParagraph;
        *character = currentCharacter;
    }

    bool InsertReferenceText(const std::wstring& text) {
        ReferenceCell* const cell = ReferenceCurrentCell();
        if (cell == nullptr) {
            return false;
        }
        size_t start = ReferenceTextOffset(
            cell->text,
            currentParagraph_,
            currentCharacter_);
        size_t end = start;
        if (selectionMode_ == 1 &&
            selectedStartList_ == currentList_ &&
            selectedEndList_ == currentList_) {
            const size_t selectedStart = ReferenceTextOffset(
                cell->text,
                selectedStartParagraph_,
                selectedStartCharacter_);
            const size_t selectedEnd = ReferenceTextOffset(
                cell->text,
                selectedEndParagraph_,
                selectedEndCharacter_);
            start = (std::min)(selectedStart, selectedEnd);
            end = (std::max)(selectedStart, selectedEnd);
        }
        cell->text.replace(start, end - start, text);
        ReferenceTextPositionAtOffset(
            cell->text,
            start + text.size(),
            &currentParagraph_,
            &currentCharacter_);
        if (currentList_ == 651 && currentParagraph_ == 0 && currentCharacter_ >= textScanHiddenAt_) {
            currentCharacter_ += textScanHiddenPrefix_;
        }
        selectionMode_ = 0;
        selectedStartList_ = 0;
        selectedStartParagraph_ = 0;
        selectedStartCharacter_ = 0;
        selectedEndList_ = 0;
        selectedEndParagraph_ = 0;
        selectedEndCharacter_ = 0;
        return true;
    }

    static ReferenceFormat BodyReferenceFormat() {
        return ReferenceFormat{};
    }

    static ReferenceFormat HeaderReferenceFormat() {
        ReferenceFormat format;
        format.height = 1100;
        format.bold = true;
        format.textColor = 16777215;
        format.alignment = 3;
        return format;
    }

    static bool ReferenceFormatMatches(
        const ReferenceFormat& left,
        const ReferenceFormat& right) noexcept {
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

    static LONG ReferenceFormatValue(
        const ReferenceFormat& format,
        const DISPID member) noexcept {
        switch (member) {
        case CharacterHeight:
            return format.height;
        case TextColor:
            return format.textColor;
        case RatioHangul:
            return format.widthRatio;
        case SpacingHangul:
            return format.letterSpacing;
        case AlignType:
            return format.alignment;
        case LineSpacingType:
            return format.lineSpacingType;
        case LineSpacing:
            return format.lineSpacing;
        case LeftMargin:
            return format.leftMargin;
        case RightMargin:
            return format.rightMargin;
        case Indentation:
            return format.indentation;
        case PrevSpacing:
            return format.previousSpacing;
        case NextSpacing:
            return format.nextSpacing;
        default:
            return 0;
        }
    }

    static void ApplyReferenceCharacterFormat(
        ReferenceFormat* const target,
        const ReferenceFormat& source) {
        target->faceName = source.faceName;
        target->height = source.height;
        target->bold = source.bold;
        target->textColor = source.textColor;
        target->widthRatio = source.widthRatio;
        target->letterSpacing = source.letterSpacing;
    }

    static void ApplyReferenceParagraphFormat(
        ReferenceFormat* const target,
        const ReferenceFormat& source) noexcept {
        target->alignment = source.alignment;
        target->lineSpacingType = source.lineSpacingType;
        target->lineSpacing = source.lineSpacing;
        target->leftMargin = source.leftMargin;
        target->rightMargin = source.rightMargin;
        target->indentation = source.indentation;
        target->previousSpacing = source.previousSpacing;
        target->nextSpacing = source.nextSpacing;
    }

    void InitializeReferenceCells() {
        const ReferenceFormat body = BodyReferenceFormat();
        referenceCells_ = {
            {L"A1", 648, 1, 1, 1, 1, 19168, 19173, true, L"", body},
            {L"B1", 649, 1, 2, 1, 1, 28752, 19173, true, L"", body},
            {L"A2", 650, 2, 1, 1, 1, 19168, 26842, true, L"", body},
            {L"B2", 651, 2, 2, 1, 1, 28752, 26842, true, L"", body},
            {L"A3", 652, 3, 1, 1, 1, 19168, 30676, true, L"", body},
            {L"B3", 653, 3, 2, 1, 1, 28752, 30676, true, L"", body},
        };
        pendingReferenceFormat_ = body;
    }

    bool SelectReferenceTable(const std::wstring& tableId) {
        if (tableId != L"reference-table" &&
            !(twoReferenceTables_ && tableId == L"reference-table-b")) {
            return false;
        }
        if (twoReferenceTables_ && tableId != selectedReferenceTableId_) {
            referenceCells_.swap(otherReferenceCells_);
        }
        selectedReferenceTableId_ = tableId;
        return true;
    }

    ReferenceCell* ReferenceCellByList(const LONG listId) noexcept {
        const auto found = std::find_if(
            referenceCells_.begin(),
            referenceCells_.end(),
            [&](const ReferenceCell& cell) {
                return cell.active && cell.listId == listId;
            });
        return found == referenceCells_.end() ? nullptr : &*found;
    }

    const ReferenceCell* ReferenceCellByList(const LONG listId) const noexcept {
        const auto found = std::find_if(
            referenceCells_.begin(),
            referenceCells_.end(),
            [&](const ReferenceCell& cell) {
                return cell.active && cell.listId == listId;
            });
        return found == referenceCells_.end() ? nullptr : &*found;
    }

    ReferenceCell* ReferenceCellByAddress(const std::wstring& address) noexcept {
        const auto found = std::find_if(
            referenceCells_.begin(),
            referenceCells_.end(),
            [&](const ReferenceCell& cell) {
                return cell.active && cell.address == address;
            });
        return found == referenceCells_.end() ? nullptr : &*found;
    }

    const ReferenceCell* ReferenceCellByAddress(
        const std::wstring& address) const noexcept {
        const auto found = std::find_if(
            referenceCells_.begin(),
            referenceCells_.end(),
            [&](const ReferenceCell& cell) {
                return cell.active && cell.address == address;
            });
        return found == referenceCells_.end() ? nullptr : &*found;
    }

    ReferenceCell* ReferenceCurrentCell() noexcept {
        return ReferenceCellByList(currentList_);
    }

    const ReferenceCell* ReferenceCurrentCell() const noexcept {
        return ReferenceCellByList(currentList_);
    }

    ReferenceCell* ReferenceFormattingCell() noexcept {
        return selectionMode_ == 3
            ? ReferenceCellByList(referenceSelectionAnchorList_)
            : ReferenceCurrentCell();
    }

    const ReferenceCell* ReferenceFormattingCell() const noexcept {
        return selectionMode_ == 3
            ? ReferenceCellByList(referenceSelectionAnchorList_)
            : ReferenceCurrentCell();
    }

    void ClearReferenceBorders() noexcept {
        for (ReferenceCell& cell : referenceCells_) {
            cell.borders.clear();
        }
    }

    bool ApplyReferenceVisibleEdge() {
        ReferenceCell* const cell = ReferenceFormattingCell();
        if (cell == nullptr) {
            return false;
        }
        for (const std::wstring& side :
             {L"Left", L"Right", L"Top", L"Bottom"}) {
            LONG type = 0;
            if (!parameterSet_->TryGetLong(
                    L"BorderType" + side,
                    &type)) {
                continue;
            }
            LONG width = 0;
            const std::wstring colorName = side == L"Left"
                ? L"BorderCorlorLeft"
                : L"BorderColor" + side;
            LONG color = 0;
            if (!parameterSet_->TryGetLong(
                    L"BorderWidth" + side,
                    &width) ||
                !parameterSet_->TryGetLong(colorName, &color)) {
                return false;
            }
            cell->borders[side] = ReferenceBorder{type, width, color};
            return true;
        }
        return false;
    }

    void PopulateReferenceBorderReadback(
        const ReferenceCell* const cell) {
        for (const std::wstring& side :
             {L"Left", L"Right", L"Top", L"Bottom"}) {
            ReferenceBorder border;
            if (cell != nullptr) {
                const auto found = cell->borders.find(side);
                if (found != cell->borders.end()) {
                    border = found->second;
                }
            }
            parameterSet_->SetGenericLong(
                L"BorderType" + side,
                border.type);
            parameterSet_->SetGenericLong(
                L"BorderWidth" + side,
                border.width);
            parameterSet_->SetGenericLong(
                side == L"Left"
                    ? L"BorderCorlorLeft"
                    : L"BorderColor" + side,
                border.color);
        }
    }

    size_t ReferenceActiveCellCount() const noexcept {
        return static_cast<size_t>(std::count_if(
            referenceCells_.begin(),
            referenceCells_.end(),
            [](const ReferenceCell& cell) { return cell.active; }));
    }

    bool SetReferenceCurrentCell(const std::wstring& address) noexcept {
        const ReferenceCell* const cell = ReferenceCellByAddress(address);
        if (cell == nullptr) {
            return false;
        }
        currentList_ = cell->listId;
        ReferenceTextPositionAtOffset(
            cell->text,
            cell->text.size(),
            &currentParagraph_,
            &currentCharacter_);
        return true;
    }

    bool SetReferenceLastCell() noexcept {
        const ReferenceCell* last = nullptr;
        for (const ReferenceCell& candidate : referenceCells_) {
            if (!candidate.active) {
                continue;
            }
            if (last == nullptr || candidate.row > last->row ||
                (candidate.row == last->row &&
                 candidate.column > last->column)) {
                last = &candidate;
            }
        }
        return last != nullptr && SetReferenceCurrentCell(last->address);
    }

    bool DeleteFirstReferenceRow() {
        bool deleted = false;
        for (ReferenceCell& cell : referenceCells_) {
            if (!cell.active) {
                continue;
            }
            if (cell.row == 1) {
                cell.active = false;
                deleted = true;
                continue;
            }
            --cell.row;
            cell.address =
                std::wstring(
                    1,
                    static_cast<wchar_t>(L'A' + cell.column - 1)) +
                std::to_wstring(cell.row);
        }
        if (!deleted) {
            return false;
        }
        referenceTopologyChanged_ = true;
        referenceRunTopologyMutated_ = true;
        selectionMode_ = 0;
        return SetReferenceCurrentCell(L"A1");
    }

    bool InsertReferenceRightColumn() noexcept {
        bool inserted = false;
        for (ReferenceCell& cell : referenceCells_) {
            if (cell.column == 2 && !cell.active) {
                cell.active = true;
                inserted = true;
            }
        }
        if (!inserted) {
            return false;
        }
        referenceTopologyChanged_ = true;
        referenceActionTopologyMutated_ = true;
        selectionMode_ = 0;
        return SetReferenceCurrentCell(L"A1");
    }

    bool MergeFirstReferenceRow() noexcept {
        ReferenceCell* const a1 = ReferenceCellByAddress(L"A1");
        ReferenceCell* const b1 = ReferenceCellByAddress(L"B1");
        if (a1 == nullptr || b1 == nullptr) {
            return false;
        }
        b1->active = false;
        a1->columnSpan = 2;
        a1->width = 47920;
        a1->format = BodyReferenceFormat();
        referenceMerged_ = true;
        referenceTopologyChanged_ = true;
        selectionMode_ = 0;
        return SetReferenceCurrentCell(L"A1");
    }

    bool MoveReferenceRight() noexcept {
        const ReferenceCell* const current = ReferenceCurrentCell();
        if (current == nullptr) {
            return false;
        }
        const ReferenceCell* next = nullptr;
        for (const ReferenceCell& candidate : referenceCells_) {
            if (!candidate.active || candidate.row != current->row ||
                candidate.column <= current->column) {
                continue;
            }
            if (next == nullptr || candidate.column < next->column) {
                next = &candidate;
            }
        }
        return next == nullptr || SetReferenceCurrentCell(next->address);
    }

    bool MoveReferenceLower() noexcept {
        const ReferenceCell* const current = ReferenceCurrentCell();
        if (current == nullptr) {
            return false;
        }
        const ReferenceCell* next = nullptr;
        for (const ReferenceCell& candidate : referenceCells_) {
            if (!candidate.active || candidate.row <= current->row ||
                candidate.column > current->column ||
                candidate.column + candidate.columnSpan <= current->column) {
                continue;
            }
            if (next == nullptr || candidate.row < next->row) {
                next = &candidate;
            }
        }
        return next == nullptr || SetReferenceCurrentCell(next->address);
    }

    bool RunReferenceLayoutAction(const std::wstring& action) {
        if (action == L"MoveParaEnd") {
            const ReferenceCell* const cell = ReferenceCurrentCell();
            if (cell == nullptr) { return false; }
            currentCharacter_ = static_cast<LONG>(cell->text.size()) +
                (currentList_ == 651 ? textScanHiddenPrefix_ : 0);
            selectionMode_ = 0;
            return true;
        }
        if (action == L"Select") {
            // Turns block selection on at the caret. The next caret move is
            // what decides the other endpoint.
            selectedStartList_ = currentList_;
            selectedStartParagraph_ = currentParagraph_;
            selectedStartCharacter_ = currentCharacter_;
            selectedEndList_ = currentList_;
            selectedEndParagraph_ = currentParagraph_;
            selectedEndCharacter_ = currentCharacter_;
            caretSelectionPending_ = true;
            return true;
        }
        if (action == L"MoveDocEnd") {
            currentList_ = 0;
            currentParagraph_ = 108;
            currentCharacter_ = 0;
            selectionMode_ = 0;
            tailSelection_ = false;
            return true;
        }
        if (action == L"MoveSelDocEnd") {
            tailSelection_ = currentList_ == 0 && currentParagraph_ == 108 &&
                currentCharacter_ == 0;
            return true;
        }
        if (action == L"Delete" && tailSelection_) {
            if (failAtomicTailDelete_) {
                return false;
            }
            atomicTail_.clear();
            pageCount_ = 1;
            tailSelection_ = false;
            ++atomicTailDeletes_;
            return true;
        }
        if (action == L"Cancel") {
            selectionMode_ = 0;
            tailSelection_ = false;
            return true;
        }
        if (!referenceTableExists_) {
            return true;
        }
        if (action == L"TableDeleteRow") {
            return DeleteFirstReferenceRow();
        }
        if (action == L"TableColEnd") {
            ++referenceTopologyInspections_;
        }
        if (action == L"ShapeObjTextBoxEdit" ||
            action == L"TableColPageUp") {
            return SetReferenceCurrentCell(L"A1");
        }
        if (action == L"TableColEnd" ||
            action == L"TableColPageDown") {
            return SetReferenceLastCell();
        }
        if (action == L"TableColBegin") {
            return SetReferenceCurrentCell(L"A1");
        }
        if (action == L"TableCellBlock") {
            if (preflightSelectionFixture_ &&
                !preflightTableControlSelection_ &&
                preflightSelectionControlCaptured_ &&
                preflightSelectionReadOccurred_) {
                preflightSelectionRestoreAttempted_ = true;
                if (failPreflightSelectionRestore_) {
                    return false;
                }
                preflightSelectionRestored_ = true;
            }
            referenceSelectionAnchorList_ = currentList_;
            selectionMode_ = 3;
            return true;
        }
        if (action == L"TableCellBlockExtend") {
            selectionMode_ = 3;
            return true;
        }
        if (action == L"TableRightCell") {
            return MoveReferenceRight();
        }
        if (action == L"TableLowerCell") {
            return MoveReferenceLower();
        }
        if (action == L"TableMergeCell") {
            return MergeFirstReferenceRow();
        }
        if (rangeFormatReadbackFixture_ &&
            action == L"TableVAlignCenter") {
            rangeSubsequentActionCoveredBlock_ = selectionMode_ == 3 &&
                currentList_ != referenceSelectionAnchorList_;
            return true;
        }
        if (action == L"MoveParentList") {
            currentList_ = 0;
            currentParagraph_ = 108;
            currentCharacter_ = 0;
            selectionMode_ = 0;
            return true;
        }
        if (action == L"SelectAll") {
            const ReferenceCell* const cell = ReferenceCurrentCell();
            if (cell == nullptr) {
                return false;
            }
            if (emptyCellTextFixture_ && cell->text.empty()) {
                emptyCellSelectAll_ = true;
                selectedStartList_ = 0;
                selectedStartParagraph_ = 0;
                selectedStartCharacter_ = 0;
                selectedEndList_ = 0;
                selectedEndParagraph_ = 0;
                selectedEndCharacter_ = 0;
                selectionMode_ = 0;
                return true;
            }
            selectedStartList_ = currentList_;
            selectedStartParagraph_ = 0;
            selectedStartCharacter_ = 0;
            selectedEndList_ = currentList_;
            ReferenceTextPositionAtOffset(
                cell->text,
                cell->text.size(),
                &selectedEndParagraph_,
                &selectedEndCharacter_);
            selectionMode_ = 1;
            return true;
        }
        if (action == L"BreakLine" || action == L"BreakPara") {
            if (!InsertReferenceText(L"\n")) {
                return false;
            }
            if (action == L"BreakPara") {
                ++referenceParagraphBreaks_;
            }
        }
        return true;
    }

    struct ParagraphProfile {
        LONG styleId = 17;
        LONG alignment = 0;
        LONG lineSpacing = 160;
        LONG leftMargin = 0;
        LONG rightMargin = 0;
        LONG indentation = 0;
        LONG previousSpacing = 1000;
        LONG nextSpacing = 0;

        bool operator==(const ParagraphProfile& other) const noexcept {
            return styleId == other.styleId &&
                alignment == other.alignment &&
                lineSpacing == other.lineSpacing &&
                leftMargin == other.leftMargin &&
                rightMargin == other.rightMargin &&
                indentation == other.indentation &&
                previousSpacing == other.previousSpacing &&
                nextSpacing == other.nextSpacing;
        }
    };

    static LONG ProfileValue(const ParagraphProfile& profile, const DISPID member) noexcept {
        switch (member) {
        case Apply:
            return profile.styleId;
        case AlignType:
            return profile.alignment;
        case LineSpacing:
            return profile.lineSpacing;
        case LeftMargin:
            return profile.leftMargin;
        case RightMargin:
            return profile.rightMargin;
        case Indentation:
            return profile.indentation;
        case PrevSpacing:
            return profile.previousSpacing;
        case NextSpacing:
            return profile.nextSpacing;
        default:
            return 0;
        }
    }

    void UpdateAtomicTailMaximumOccurrences() noexcept {
        constexpr wchar_t marker[] = L"atomic-tail";
        size_t occurrences = 0;
        size_t offset = 0;
        while ((offset = atomicTail_.find(marker, offset)) != std::wstring::npos) {
            ++occurrences;
            offset += std::wcslen(marker);
        }
        atomicTailMaximumOccurrences_ = (std::max)(
            atomicTailMaximumOccurrences_,
            occurrences);
    }

    ~FakeDispatch() {
        static_cast<void>(DeleteFileW(kSaveEvidenceFile));
        static_cast<void>(parameterSet_->Release());
    }
    volatile LONG references_ = 1;
    LONG windowHandle_ = 0;
    LONG documentId_ = 0;
    LONG pendingDocumentId_ = 0;
    FakeParameterArrayDispatch* parameterSet_ = new FakeParameterArrayDispatch();
    bool referenceLayoutFixture_ = false;
    bool wrongReferenceControlIdentity_ = false;
    bool twoReferenceTables_ = false;
    std::wstring selectedReferenceTableId_ = L"reference-table";
    std::vector<ReferenceCell> otherReferenceCells_;
    bool emptyCellTextFixture_ = false;
    bool emptyCellSelectAll_ = false;
    bool preflightSelectionFixture_ = false;
    bool preflightTableControlSelection_ = false;
    bool failPreflightSelectionRestore_ = false;
    bool preflightSelectionControlCaptured_ = false;
    bool preflightSelectionReadOccurred_ = false;
    bool preflightSelectionRestoreAttempted_ = false;
    bool preflightSelectionRestored_ = false;
    bool failReferenceLayoutAfterFirstText_ = false;
    bool dropReferenceEdges_ = false;
    bool referenceTableExists_ = false;
    bool referenceMerged_ = false;
    bool tableSelected_ = false;
    LONG referenceSelectionAnchorList_ = 0;
    LONG selectedStartList_ = 0;
    LONG selectedStartParagraph_ = 0;
    LONG selectedStartCharacter_ = 0;
    LONG selectedEndList_ = 0;
    LONG selectedEndParagraph_ = 0;
    LONG selectedEndCharacter_ = 0;
    size_t referenceTextInsertions_ = 0;
    size_t referenceParagraphBreaks_ = 0;
    size_t referenceEdgeExecutions_ = 0;
    size_t referenceEdgeApplications_ = 0;
    size_t referenceBorderReadbacks_ = 0;
    size_t referenceTopologyInspections_ = 0;
    bool referenceTopologyChanged_ = false;
    bool referenceRunTopologyMutated_ = false;
    bool referenceActionTopologyMutated_ = false;
    bool referenceCallTopologyMutated_ = false;
    bool staleTopologyCellPositionAttempted_ = false;
    size_t referenceFillApplications_ = 0;
    size_t referenceControlDeletes_ = 0;
    bool dropAppliedTextFormat_ = false;
    bool unavailableUnrelatedFormatProperty_ = false;
    bool unavailableRequestedFormatProperty_ = false;
    bool automaticNumberReadback_ = false;
    bool automaticNumberSelectTextRefuses_ = false;
    size_t automaticNumberSelectTextRefusals_ = 0;
    std::wstring findString_;
    size_t findSelectionSurplus_ = 0;
    bool findDisplayOnlyPrefix_ = false;
    bool caretSelectionPending_ = false;
    size_t caretSelectionsCompleted_ = 0;
    bool pendingTextFormatReadback_ = false;
    size_t textFormatActionsExecuted_ = 0;
    size_t textFormatReadbacks_ = 0;
    bool rangeFormatReadbackFixture_ = false;
    bool dropAppliedRangeFormat_ = false;
    LONG rangeCellFillValue_ = 0;
    bool rangeCellFillCoveredBlock_ = false;
    bool rangeCellBorderCoveredBlock_ = false;
    bool rangePaddingCoveredBlock_ = false;
    bool rangeSubsequentActionCoveredBlock_ = false;
    ReferenceFormat pendingReferenceFormat_;
    std::vector<ReferenceCell> referenceCells_;
    bool anchorRead_ = false;
    bool positionedAfterAnchor_ = false;
    bool frontSelectedAfterPosition_ = false;
    bool copyUsedAnchorSelection_ = false;
    bool copyCompleted_ = false;
    bool pasteUsedSourceAnchorFormat_ = false;
    bool nativeTableRangeSelected_ = false;
    bool nativeTableBlockCopied_ = false;
    bool nativeTableBlockPasted_ = false;
    bool clipboardTableTransferUsed_ = false;
    bool ranBreakPage_ = false;
    bool failSelectCtrlFront_ = false;
    bool atomicAppendFixture_ = false;
    bool failSelectCtrlFrontWithHresult_ = false;
    bool failAtomicTailDelete_ = false;
    bool tailSelection_ = false;
    bool checkpointRestoreFixture_ = false;
    bool checkpointWrongTargetPageCount_ = false;
    bool checkpointSetTextFileFails_ = false;
    std::wstring checkpointDocumentBlock_;
    size_t checkpointSetAttempts_ = 0;
    bool checkpointBlockBodyPresent_ = false;
    bool checkpointBlockBodySelected_ = false;
    // The document-file checkpoint layout. kCheckpointDocumentPages is what the
    // checkpoint on disk is worth; the restore is only correct when the document
    // ends up at exactly that.
    static constexpr LONG kCheckpointDocumentPages = 4;
    static constexpr LONG kCheckpointContentParagraphs = 4;
    static constexpr LONG kCheckpointParagraphLength = 7;
    bool checkpointDocumentFileFixture_ = false;
    std::wstring checkpointUserDocumentPath_ = L"C:\\x.hwp";
    bool checkpointInsertNeverMatches_ = false;
    bool checkpointRollbackInsertFails_ = false;
    bool checkpointResidueAtFront_ = true;
    bool checkpointSectionsNeedKeeping_ = false;
    bool checkpointResidueParagraph_ = false;
    bool checkpointLeadingSectionResidue_ = false;
    size_t checkpointLeadingSectionDeletes_ = 0;
    bool checkpointFlattenedPenalty_ = false;
    bool checkpointWholeSelected_ = false;
    bool checkpointBodySelected_ = false;
    bool checkpointTailSelected_ = false;
    bool checkpointHeadSelected_ = false;
    LONG checkpointContentParagraphs_ = 0;
    LONG checkpointPendingKeepSection_ = -1;
    std::wstring checkpointPendingFileName_;
    size_t checkpointOpenCount_ = 0;
    size_t checkpointClearCallCount_ = 0;
    bool checkpointClearFails_ = false;
    bool checkpointTargetPageMismatch_ = false;
    bool checkpointRollbackOpenFails_ = false;
    LONG checkpointLastKeepSection_ = -1;
    bool checkpointHeadProbeActive_ = false;
    LONG checkpointHeadControlIndex_ = 0;
    bool checkpointHeadPageBreakBefore_ = false;
    bool checkpointHeadSectionIsolated_ = true;
    std::wstring checkpointTailAnswer_ = L"\r\n";
    std::wstring checkpointHeadAnswer_ = L"\r\n";
    bool checkpointAbsorbsResidue_ = false;
    bool checkpointInsertCaretBeforeEnd_ = false;
    bool checkpointSingleParagraph_ = false;
    bool checkpointSelectionMisreports_ = false;
    size_t checkpointHeadRangeReportsAfterInsert_ = 0;
    bool checkpointEngineUndoRestores_ = false;
    bool checkpointEngineUndoWrongStep_ = false;
    bool checkpointSignatureCaptureUnavailable_ = false;
    bool checkpointPageCountBelow_ = false;
    bool checkpointHeadResidueDeleted_ = false;
    bool checkpointTextFileEmpty_ = false;
    bool checkpointPictureOnly_ = false;
    bool checkpointPresenceScan_ = false;
    size_t checkpointTextLossInsertAttempts_ = 0;
    size_t checkpointDeleteLeavesTextAttempts_ = 0;
    size_t checkpointCrossListEndAttempts_ = 0;
    size_t checkpointBodyDeleteAttempts_ = 0;
    std::vector<LONG> checkpointInsertKeepSections_;
    LONG pageCount_ = 1;
    LONG currentList_ = 0;
    LONG currentParagraph_ = 0;
    LONG currentCharacter_ = 0;
    LONG selectionMode_ = 1;
    bool textDeletionFixture_ = false;
    bool textSelectionActive_ = true;
    size_t textDeletionActions_ = 0;
    size_t insertTextExecutions_ = 0;
    bool firstWriteReadbackFixture_ = false;
    bool firstWriteReadbackFailed_ = false;
    std::wstring atomicTail_;
    size_t atomicTailDeletes_ = 0;
    size_t atomicTailMaximumOccurrences_ = 0;
    bool scanStarted_ = false;
    bool scanReleased_ = false;
    bool textLookupScan_ = false;
    bool failTextScanRelease_ = false;
    size_t findActionAttempts_ = 0;
    LONG textScanHiddenPrefix_ = 0;
    LONG textScanHiddenAt_ = 0;
    size_t textScanReadbacks_ = 0;
    std::vector<TextScanChunk> textScanChunks_;
    bool actionInputApplied_ = false;
    LONG scanStep_ = 0;
    std::wstring currentAction_;
    std::wstring insertedText_;
    std::vector<std::wstring> runActions_;
    static constexpr LONG kInitialMessageBoxMode = 0x00000020;
    LONG messageBoxMode_ = kInitialMessageBoxMode;
    bool autoYesMessageBoxModeUsed_ = false;
    bool failNextMessageBoxModeRestore_ = false;
    size_t messageBoxModeRestoreAttempts_ = 0;
    DISPID activeParameter_ = DISPID_UNKNOWN;
    const ParagraphProfile sourceProfile_{};
    const ParagraphProfile destinationProfile_{2, 0, 160, 4000, 0, -2880, 0, 0};
    ParagraphProfile currentProfile_{};
    ParagraphProfile pendingProfile_{};
    std::vector<std::wstring> lifecycleCalls_;
    bool saveCalledWithTrue_ = false;
    bool clearCalledWithDiscard_ = false;
    bool openCalledWithArguments_ = false;
    bool lifecycleRecovered_ = false;
    bool saveAsCalledWithArguments_ = false;
    bool lifecycleRecoveryBlockCaptured_ = true;
    bool lifecycleMismatchAfterOpen_ = false;
    bool lifecycleMismatchActive_ = false;
    size_t clearCallCount_ = 0;
    bool saveReturnsTrue_ = true;
    bool saveClearsModified_ = true;
    bool openReturnsTrue_ = true;
    bool modified_ = false;
    bool documentOpen_ = true;
    std::wstring fullName_ = L"C:\\x.hwp";
    std::wstring lifecyclePath_ = L"C:\\\uD55C\uAE00\\x.hwp";
    std::wstring lifecycleReopenedPath_ = L"C:\\\uD55C\uAE00\\X.HWP";
    std::wstring lifecycleFormat_ = L"HWP";
    bool inspectionScopeFixture_ = false;
    std::vector<InspectionControl> inspectionControls_;
    LONG inspectionControlIndex_ = -1;
    bool inspectionAnchorRoot_ = false;
    LONG inspectionCurrentPage_ = 0;
    size_t inspectionHeadReads_ = 0;
    size_t inspectionLastReads_ = 0;
    size_t inspectionNextReads_ = 0;
    size_t inspectionPrevReads_ = 0;
    size_t inspectionIdentityReads_ = 0;
    bool requireActiveDocumentFullName_ = false;
    bool activeDocumentFullNameReady_ = false;
    LONG activeDocumentFullNameReads_ = 0;
    bool unstableSerializationMetadataFixture_ = false;
    bool oversizeSerializationRefusalFixture_ = false;
    size_t hwpmlSerializationAttempts_ = 0;
    bool unstableCaretMetadataFixture_ = false;
    bool diagnosticSectionMismatchFixture_ = false;
    std::wstring diagnosticSectionBeforeHwpml_;
    std::wstring diagnosticSectionAfterHwpml_;
    size_t serializationReads_ = 0;
    inline static constexpr wchar_t kSaveEvidenceFile[] =
        L"HancomLiveBridgeSaveSmoke.hwp";
};

using QueryModule = IHncUserActionModule*(__stdcall*)();
using GetLastResult = HRESULT(__stdcall*)();
using ReleasePublication = HRESULT(__stdcall*)();
using ResetGraphLifecycleDiagnostics = void(__stdcall*)();
using ReadGraphLifecycleDiagnostics = BOOL(__stdcall*)(
    hancom::graph::protocol::DebugLifecycleCounters*,
    hancom::graph::protocol::DebugLifecycleEvent*, std::uint32_t,
    std::uint32_t*);
using QueryGraphCapabilitySession = BOOL(__stdcall*)(
    const hancom::graph::identity::DocumentSessionId*, LONG, std::uintptr_t,
    std::uint64_t*, std::uint64_t*);

IUnknown* GetPublishedObject(const std::wstring& itemName) {
    IRunningObjectTable* table = nullptr;
    if (FAILED(GetRunningObjectTable(0, &table))) {
        return nullptr;
    }
    IMoniker* moniker = nullptr;
    HRESULT result = CreateItemMoniker(L"!", itemName.c_str(), &moniker);
    IUnknown* object = nullptr;
    if (SUCCEEDED(result)) {
        result = table->GetObject(moniker, &object);
        moniker->Release();
    }
    table->Release();
    if (FAILED(result)) {
        return nullptr;
    }
    return object;
}

bool IsPublishedObject(const std::wstring& itemName) {
    IRunningObjectTable* table = nullptr;
    if (FAILED(GetRunningObjectTable(0, &table))) {
        return false;
    }
    IMoniker* moniker = nullptr;
    HRESULT result = CreateItemMoniker(L"!", itemName.c_str(), &moniker);
    if (SUCCEEDED(result)) {
        result = table->IsRunning(moniker);
        moniker->Release();
    }
    table->Release();
    return result == S_OK;
}

bool ReadProtocolVersion(IDispatch* const batch) {
    LPOLESTR name = const_cast<LPOLESTR>(L"ProtocolVersion");
    DISPID member = DISPID_UNKNOWN;
    if (FAILED(batch->GetIDsOfNames(
            IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &member))) {
        return false;
    }
    DISPPARAMS parameters{};
    VARIANT result;
    VariantInit(&result);
    const HRESULT status = batch->Invoke(
        member,
        IID_NULL,
        LOCALE_USER_DEFAULT,
        DISPATCH_PROPERTYGET,
        &parameters,
        &result,
        nullptr,
        nullptr);
    const bool matched = SUCCEEDED(status) && result.vt == VT_I4 && result.lVal == 14;
    VariantClear(&result);
    return matched;
}

template <typename Integer>
Integer ReadLittleEndian(const std::uint8_t* const bytes) noexcept {
    Integer value = 0;
    std::memcpy(&value, bytes, sizeof(value));
    return value;
}

bool IndependentlyDecodeCapabilities(const std::vector<std::uint8_t>& frame) {
    constexpr std::size_t headerBytes = 320;
    constexpr std::uint64_t maximumFrameBytes = 4'194'304;
    constexpr std::uint64_t maximumPayloadBytes = 4'193'984;
    if (frame.size() < headerBytes || frame.size() > maximumFrameBytes ||
        std::memcmp(frame.data(), "HGN1", 4) != 0 ||
        ReadLittleEndian<std::uint16_t>(frame.data() + 4) != 1 ||
        ReadLittleEndian<std::uint16_t>(frame.data() + 6) != 1 ||
        ReadLittleEndian<std::uint32_t>(frame.data() + 8) != 2 ||
        ReadLittleEndian<std::uint32_t>(frame.data() + 12) != headerBytes ||
        ReadLittleEndian<std::uint64_t>(frame.data() + 16) !=
            frame.size() - headerBytes) {
        return false;
    }
    for (std::size_t index = 24; index != 256; ++index) {
        if (frame[index] != 0) {
            return false;
        }
    }
    struct ExpectedField final {
        std::uint16_t tag;
        std::uint16_t scalar;
        std::uint64_t valueBytes;
        std::uint64_t value;
    };
    const ExpectedField expected[]{
        {1, 16, 4, 15},
        {2, 15, 2, 1},
        {3, 1, 8, maximumFrameBytes},
        {4, 1, 8, maximumPayloadBytes},
        {5, 1, 8, UINT64_MAX},
        {6, 1, 8, ReadLittleEndian<std::uint64_t>(
            frame.data() + frame.size() - 8)},
    };
    std::size_t offset = headerBytes;
    for (const ExpectedField& field : expected) {
        if (offset + 24 + field.valueBytes > frame.size() ||
            ReadLittleEndian<std::uint16_t>(frame.data() + offset) != field.tag ||
            ReadLittleEndian<std::uint16_t>(frame.data() + offset + 2) != 1 ||
            ReadLittleEndian<std::uint16_t>(frame.data() + offset + 4) != field.scalar ||
            ReadLittleEndian<std::uint16_t>(frame.data() + offset + 6) != 0 ||
            ReadLittleEndian<std::uint64_t>(frame.data() + offset + 8) != 1 ||
            ReadLittleEndian<std::uint64_t>(frame.data() + offset + 16) !=
                field.valueBytes) {
            return false;
        }
        offset += 24;
        std::uint64_t value = 0;
        std::memcpy(&value, frame.data() + offset,
                    static_cast<std::size_t>(field.valueBytes));
        if (value != field.value) {
            return false;
        }
        offset += static_cast<std::size_t>(field.valueBytes);
    }
    return offset == frame.size() &&
        (expected[5].value == UINT64_C(0x10) ||
         expected[5].value == UINT64_C(0x39));
}

bool ReadExactCapabilityArray(
    const VARIANT& capabilities,
    std::vector<std::uint8_t>* const bytes) {
    if (bytes == nullptr || capabilities.vt != (VT_ARRAY | VT_UI1) ||
        capabilities.parray == nullptr ||
        SafeArrayGetDim(capabilities.parray) != 1 ||
        SafeArrayGetElemsize(capabilities.parray) != sizeof(BYTE) ||
        capabilities.parray->cDims != 1 ||
        capabilities.parray->cbElements != sizeof(BYTE) ||
        capabilities.parray->rgsabound[0].lLbound != 0) {
        return false;
    }
    VARTYPE descriptorType = VT_EMPTY;
    LONG lower = 0;
    LONG upper = -1;
    if (FAILED(SafeArrayGetVartype(capabilities.parray, &descriptorType)) ||
        descriptorType != VT_UI1 ||
        FAILED(SafeArrayGetLBound(capabilities.parray, 1, &lower)) || lower != 0 ||
        FAILED(SafeArrayGetUBound(capabilities.parray, 1, &upper)) || upper < 0) {
        return false;
    }
    const std::uint64_t count =
        static_cast<std::uint64_t>(static_cast<unsigned long long>(upper) + 1ULL);
    if (count != capabilities.parray->rgsabound[0].cElements ||
        count > 4'194'304 ||
        count > static_cast<std::uint64_t>((std::numeric_limits<std::size_t>::max)())) {
        return false;
    }
    void* raw = nullptr;
    if (FAILED(SafeArrayAccessData(capabilities.parray, &raw))) {
        return false;
    }
    bool copied = true;
    try {
        const auto* const first = static_cast<const std::uint8_t*>(raw);
        bytes->assign(first, first + static_cast<std::size_t>(count));
    } catch (...) {
        copied = false;
    }
    return SUCCEEDED(SafeArrayUnaccessData(capabilities.parray)) && copied;
}

HRESULT InvokeGraphCapabilitiesHandshake(
    IDispatch* const batch,
    const hancom::graph::identity::DocumentSessionId& session,
    const std::uint64_t requested,
    bool* const exactCapabilities) {
    if (batch == nullptr || exactCapabilities == nullptr) return E_POINTER;
    *exactCapabilities = false;
    const std::wstring sessionText =
        hancom::graph::identity::FormatCanonicalUuid(session);
    VARIANTARG arguments[2]{};
    arguments[0].vt = VT_UI8;
    arguments[0].ullVal = requested;
    arguments[1].vt = VT_BSTR;
    arguments[1].bstrVal = SysAllocString(sessionText.c_str());
    if (arguments[1].bstrVal == nullptr) return E_OUTOFMEMORY;
    DISPPARAMS parameters{arguments, nullptr, 2, 0};
    VARIANT result{};
    const HRESULT status = batch->Invoke(
        26, IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_METHOD,
        &parameters, &result, nullptr, nullptr);
    std::vector<std::uint8_t> frame;
    *exactCapabilities = SUCCEEDED(status) &&
        ReadExactCapabilityArray(result, &frame) &&
        IndependentlyDecodeCapabilities(frame);
    VariantClear(&result);
    VariantClear(&arguments[1]);
    return status;
}

struct GraphErrorObservation final {
    HRESULT invokeStatus = E_FAIL;
    std::uint16_t message = 0;
    std::uint32_t errorCode = UINT32_MAX;
    std::uint32_t errorHresult = 0;
    std::uint64_t responseBytes = 0;
    bool decoded = false;
};

GraphErrorObservation InvokeGraphCloseError(
    IDispatch* const batch,
    const hancom::graph::identity::DocumentSessionId& session,
    const hancom::graph::identity::DocumentSessionId& cursor) {
    using namespace hancom::graph;
    GraphErrorObservation observation{};
    if (batch == nullptr) return observation;
    protocol::Header header{};
    header.message = protocol::MessageKind::GraphCloseRequest;
    header.session = session;
    header.cursorOrUpload = cursor;
    const codec::Bytes sessionBytes(session.bytes.begin(), session.bytes.end());
    const codec::Bytes cursorBytes(cursor.bytes.begin(), cursor.bytes.end());
    codec::Bytes payload;
    for (const codec::Bytes& field : {
        protocol::EncodeField(1, 1, ScalarTag::UUID128, codec::View(cursorBytes)),
        protocol::EncodeField(2, 1, ScalarTag::UUID128, codec::View(sessionBytes))}) {
        payload.insert(payload.end(), field.begin(), field.end());
    }
    codec::Bytes request;
    if (!protocol::EncodeFrame(header, codec::View(payload), &request)) return observation;
    VARIANTARG argument{};
    if (FAILED(protocol::ReturnByteArray(codec::View(request), &argument))) return observation;
    DISPPARAMS parameters{&argument, nullptr, 1, 0};
    VARIANT result{};
    const HRESULT status = batch->Invoke(
        30, IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_METHOD,
        &parameters, &result, nullptr, nullptr);
    observation.invokeStatus = status;
    VariantClear(&argument);
    codec::Bytes response;
    const bool copied = SUCCEEDED(status) &&
        SUCCEEDED(protocol::ReadByteArrayArgument(result, &response));
    VariantClear(&result);
    protocol::Header responseHeader{};
    codec::ByteView responsePayload{};
    protocol::ErrorCode decodeError{};
    std::vector<protocol::ParsedField> fields;
    observation.responseBytes = response.size();
    if (!copied || !protocol::DecodeFrame(
            codec::View(response), &responseHeader, &responsePayload, &decodeError) ||
        responseHeader.message != protocol::MessageKind::Error ||
        !protocol::ParseFields(responsePayload, &fields, &decodeError)) return observation;
    const auto code = std::find_if(fields.begin(), fields.end(),
        [](const protocol::ParsedField& field) { return field.tag == 1; });
    const auto errorHresult = std::find_if(fields.begin(), fields.end(),
        [](const protocol::ParsedField& field) { return field.tag == 2; });
    if (code == fields.end() || code->value.size != 4 ||
        errorHresult == fields.end() || errorHresult->value.size != 4)
        return observation;
    observation.message = static_cast<std::uint16_t>(responseHeader.message);
    observation.errorCode = ReadLittleEndian<std::uint32_t>(code->value.data);
    observation.errorHresult =
        ReadLittleEndian<std::uint32_t>(errorHresult->value.data);
    observation.decoded = true;
    return observation;
}

IDispatch* GetPublishedBatchDispatch(const std::wstring& scope) {
    IUnknown* const unknown = GetPublishedObject(L"HancomLiveBatch." + scope);
    if (unknown == nullptr) return nullptr;
    IDispatch* dispatch = nullptr;
    static_cast<void>(unknown->QueryInterface(
        IID_IDispatch, reinterpret_cast<void**>(&dispatch)));
    unknown->Release();
    return dispatch;
}

bool ReadGraphDispatchAbi(IDispatch* const batch) {
    constexpr const wchar_t* names[]{
        L"GraphProtocolVersion", L"GraphCapabilities", L"GraphOpen", L"GraphNext",
        L"GraphCancel", L"GraphClose", L"PatchBegin", L"PatchChunk",
        L"PatchCommit", L"PatchAbort", L"PatchValidate",
    };
    for (std::size_t index = 0; index != std::size(names); ++index) {
        LPOLESTR name = const_cast<LPOLESTR>(names[index]);
        DISPID member = DISPID_UNKNOWN;
        if (FAILED(batch->GetIDsOfNames(
                IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &member)) ||
            member != static_cast<DISPID>(25 + index)) return false;
    }
    // PatchApply is a published member of this ABI, not an absent one.
    // compatibility-manifest.json declares dispatch ids 25-37, BatchAutomation.cpp
    // resolves this name to kPatchApply = 36 and routes it into InvokeGraphMember,
    // and the graph patch tools ship on top of it. Requiring GetIDsOfNames to fail
    // here was a leftover from the todo19 design, which predated patch application:
    // 3fc5c52 added the member to the bridge, the manifest and the todo30 tests but
    // did not update this expectation, and the stale prebuilt smoke binary hid the
    // contradiction until the executable was rebuilt from current sources.
    LPOLESTR applyName = const_cast<LPOLESTR>(L"PatchApply");
    DISPID applyMember = DISPID_UNKNOWN;
    if (FAILED(batch->GetIDsOfNames(
            IID_NULL, &applyName, 1, LOCALE_USER_DEFAULT, &applyMember)) ||
        applyMember != 36) return false;
    LPOLESTR blobName = const_cast<LPOLESTR>(L"BlobRead");
    DISPID blobMember = DISPID_UNKNOWN;
    if (FAILED(batch->GetIDsOfNames(
            IID_NULL, &blobName, 1, LOCALE_USER_DEFAULT, &blobMember)) ||
        blobMember != 37) return false;
    DISPPARAMS noArguments{};
    VARIANT version;
    VariantInit(&version);
    HRESULT status = batch->Invoke(
        25, IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_PROPERTYGET,
        &noArguments, &version, nullptr, nullptr);
    const bool versionMatched = SUCCEEDED(status) && version.vt == VT_I4 && version.lVal == 15;
    VariantClear(&version);
    VARIANT capabilities;
    VariantInit(&capabilities);
    status = batch->Invoke(
        26, IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_PROPERTYGET,
        &noArguments, &capabilities, nullptr, nullptr);
    std::vector<std::uint8_t> capabilityBytes;
    const bool capabilityMatched = SUCCEEDED(status) &&
        ReadExactCapabilityArray(capabilities, &capabilityBytes) &&
        IndependentlyDecodeCapabilities(capabilityBytes);
    VariantClear(&capabilities);
    VARIANTARG bad{};
    bad.vt = VT_I4;
    bad.lVal = 1;
    DISPPARAMS oneArgument{&bad, nullptr, 1, 0};
    VARIANT ignored;
    VariantInit(&ignored);
    const HRESULT badStatus = batch->Invoke(
        31, IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_METHOD,
        &oneArgument, &ignored, nullptr, nullptr);
    VariantClear(&ignored);
    return versionMatched && capabilityMatched && badStatus == DISP_E_TYPEMISMATCH;
}

bool HasDispatchMember(IDispatch* const dispatch, const wchar_t* const method) {
    LPOLESTR name = const_cast<LPOLESTR>(method);
    DISPID member = DISPID_UNKNOWN;
    return SUCCEEDED(dispatch->GetIDsOfNames(
        IID_NULL,
        &name,
        1,
        LOCALE_USER_DEFAULT,
        &member));
}

bool InvokeString(
    IDispatch* const batch,
    const wchar_t* const method,
    const wchar_t* const argument,
    std::wstring* const returned) {
    LPOLESTR name = const_cast<LPOLESTR>(method);
    DISPID member = DISPID_UNKNOWN;
    if (FAILED(batch->GetIDsOfNames(
            IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &member))) {
        return false;
    }
    VARIANTARG input;
    VariantInit(&input);
    DISPPARAMS parameters{};
    if (argument != nullptr) {
        input.vt = VT_BSTR;
        input.bstrVal = SysAllocString(argument);
        if (input.bstrVal == nullptr) {
            return false;
        }
        parameters.rgvarg = &input;
        parameters.cArgs = 1;
    }
    VARIANT result;
    VariantInit(&result);
    const HRESULT status = batch->Invoke(
        member,
        IID_NULL,
        LOCALE_USER_DEFAULT,
        DISPATCH_METHOD,
        &parameters,
        &result,
        nullptr,
        nullptr);
    VariantClear(&input);
    const bool valid = SUCCEEDED(status) && result.vt == VT_BSTR && result.bstrVal != nullptr;
    if (valid) {
        returned->assign(result.bstrVal, SysStringLen(result.bstrVal));
    }
    VariantClear(&result);
    return valid;
}

bool InvokeTwoStrings(
    IDispatch* const batch,
    const wchar_t* const method,
    const wchar_t* const first,
    const wchar_t* const second,
    std::wstring* const returned) {
    LPOLESTR name = const_cast<LPOLESTR>(method);
    DISPID member = DISPID_UNKNOWN;
    if (FAILED(batch->GetIDsOfNames(
            IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &member))) {
        return false;
    }
    VARIANTARG inputs[2];
    VariantInit(&inputs[0]);
    VariantInit(&inputs[1]);
    inputs[0].vt = VT_BSTR;
    inputs[0].bstrVal = SysAllocString(second);
    inputs[1].vt = VT_BSTR;
    inputs[1].bstrVal = SysAllocString(first);
    if (inputs[0].bstrVal == nullptr || inputs[1].bstrVal == nullptr) {
        VariantClear(&inputs[1]);
        VariantClear(&inputs[0]);
        return false;
    }
    DISPPARAMS parameters{};
    parameters.rgvarg = inputs;
    parameters.cArgs = 2;
    VARIANT result;
    VariantInit(&result);
    const HRESULT status = batch->Invoke(
        member,
        IID_NULL,
        LOCALE_USER_DEFAULT,
        DISPATCH_METHOD,
        &parameters,
        &result,
        nullptr,
        nullptr);
    VariantClear(&inputs[1]);
    VariantClear(&inputs[0]);
    const bool valid =
        SUCCEEDED(status) &&
        result.vt == VT_BSTR &&
        result.bstrVal != nullptr;
    if (valid) {
        returned->assign(result.bstrVal, SysStringLen(result.bstrVal));
    }
    VariantClear(&result);
    return valid;
}

bool InvokeLongString(
    IDispatch* const batch,
    const wchar_t* const method,
    const LONG argument,
    std::wstring* const returned) {
    if (returned == nullptr) {
        return false;
    }
    LPOLESTR name = const_cast<LPOLESTR>(method);
    DISPID member = DISPID_UNKNOWN;
    if (FAILED(batch->GetIDsOfNames(
            IID_NULL, &name, 1, LOCALE_USER_DEFAULT, &member))) {
        return false;
    }
    VARIANTARG input;
    VariantInit(&input);
    input.vt = VT_I4;
    input.lVal = argument;
    DISPPARAMS parameters{};
    parameters.rgvarg = &input;
    parameters.cArgs = 1;
    VARIANT result;
    VariantInit(&result);
    const HRESULT status = batch->Invoke(
        member,
        IID_NULL,
        LOCALE_USER_DEFAULT,
        DISPATCH_METHOD,
        &parameters,
        &result,
        nullptr,
        nullptr);
    const bool valid = SUCCEEDED(status) &&
        result.vt == VT_BSTR &&
        result.bstrVal != nullptr;
    if (valid) {
        returned->assign(result.bstrVal, SysStringLen(result.bstrVal));
    }
    VariantClear(&result);
    return valid;
}

bool ActivateDocumentAndWait(
    IDispatch* const batch,
    const LONG documentId) {
    std::wstring token;
    if (!InvokeLongString(
            batch,
            L"ActivateDocument",
            documentId,
            &token)) {
        return false;
    }
    HANDLE const success = OpenEventW(
        SYNCHRONIZE,
        FALSE,
        (token + L".Success").c_str());
    HANDLE const failure = OpenEventW(
        SYNCHRONIZE,
        FALSE,
        (token + L".Failure").c_str());
    if (success == nullptr || failure == nullptr) {
        if (success != nullptr) {
            CloseHandle(success);
        }
        if (failure != nullptr) {
            CloseHandle(failure);
        }
        return false;
    }
    HANDLE handles[2] = {success, failure};
    const DWORD waited = WaitForMultipleObjects(
        2,
        handles,
        FALSE,
        5'000);
    CloseHandle(failure);
    CloseHandle(success);
    return waited == WAIT_OBJECT_0;
}

std::vector<std::wstring> SplitTabs(const std::wstring& value) {
    std::vector<std::wstring> fields;
    size_t start = 0;
    for (;;) {
        const size_t end = value.find(L'\t', start);
        fields.push_back(value.substr(start, end - start));
        if (end == std::wstring::npos) {
            return fields;
        }
        start = end + 1;
    }
}

std::vector<std::wstring> SplitCommas(const std::wstring& value) {
    std::vector<std::wstring> fields;
    size_t start = 0;
    for (;;) {
        const size_t end = value.find(L',', start);
        fields.push_back(value.substr(start, end - start));
        if (end == std::wstring::npos) {
            return fields;
        }
        start = end + 1;
    }
}

bool IsSectionHash(const std::wstring& value) {
    return value.size() == 16 &&
        value.find_first_not_of(L"0123456789abcdef") == std::wstring::npos;
}

bool MatchingFingerprintDiagnostics(
    const std::vector<std::wstring>& fields,
    const size_t start) {
    if (fields.size() < start + 4 ||
        fields[start].empty() ||
        fields[start] != fields[start + 1] ||
        fields[start + 2].empty() ||
        fields[start + 2] != fields[start + 3]) {
        return false;
    }
    const std::vector<std::wstring> sections =
        SplitCommas(fields[start + 2]);
    return std::all_of(sections.begin(), sections.end(), IsSectionHash);
}

bool SingleSectionDiagnosticMismatch(
    const std::wstring& before,
    const std::wstring& after,
    const size_t expectedIndex) {
    const std::vector<std::wstring> beforeSections = SplitCommas(before);
    const std::vector<std::wstring> afterSections = SplitCommas(after);
    if (beforeSections.size() != afterSections.size() ||
        expectedIndex >= beforeSections.size()) {
        return false;
    }
    size_t mismatchCount = 0;
    for (size_t index = 0; index < beforeSections.size(); ++index) {
        if (!IsSectionHash(beforeSections[index]) ||
            !IsSectionHash(afterSections[index])) {
            return false;
        }
        if (beforeSections[index] != afterSections[index]) {
            if (index != expectedIndex) {
                return false;
            }
            ++mismatchCount;
        }
    }
    return mismatchCount == 1;
}

bool AtomicRollbackSucceeded(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 11 && fields[0] == L"HCA2" && fields[1] == L"ERROR" &&
        fields[2] == L"COM_METHOD" && fields[3] == L"U2VsZWN0Q3RybEZyb250" &&
        fields[4] == L"UnVuIGZhaWxlZCAoSFJFU1VMVCAweDgwMDA0MDA1KQ==" &&
        fields[5] == L"3" && fields[6] == fields[3] && fields[7] == L"0" &&
        fields[8] == L"1" && !fields[9].empty() && fields[9] == fields[10];
}

bool ReferenceLayoutSucceeded(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 12 && fields[0] == L"HCA2" && fields[1] == L"OK" &&
        fields[2] == L"2" && fields[4] == L"5" && !fields[7].empty();
}

bool ReferenceLayoutAtomicRollbackSucceeded(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 11 && fields[0] == L"HCA2" && fields[1] == L"ERROR" &&
        !fields[2].empty() && !fields[3].empty() && !fields[4].empty() &&
        fields[5] == L"1" && !fields[6].empty() &&
        fields[7] == L"0" && fields[8] == L"1" && !fields[9].empty() &&
        fields[9] == fields[10];
}

bool ReferenceLayoutDroppedEdgesRejected(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 11 && fields[0] == L"HCA2" &&
        fields[1] == L"ERROR" &&
        fields[2] == L"REFERENCE_LAYOUT_VERIFY" &&
        fields[3] == L"QTE6VG9w" &&
        !fields[4].empty();
}

bool SafeStaleCellPreflightFailed(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 11 && fields[0] == L"HCA2" && fields[1] == L"ERROR" &&
        fields[2] == L"STALE_CELL_TEXT" && !fields[3].empty() &&
        !fields[4].empty() && !fields[6].empty() &&
        fields[7] == L"0" && fields[8] == L"1" && !fields[9].empty() &&
        fields[9] == fields[10];
}

bool CellTopologyInvalidationFailedSafely(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 11 && fields[0] == L"HCA2" &&
        fields[1] == L"ERROR" && fields[2] == L"CELL_NOT_FOUND" &&
        !fields[3].empty() && !fields[4].empty();
}

bool OversizedCellPatchRejectedSafely(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 11 && fields[0] == L"HCA2" &&
        fields[1] == L"ERROR" && fields[2] == L"TEXT_PATCH_LIMIT" &&
        !fields[3].empty() && !fields[4].empty() &&
        fields[5] == L"0" && fields[7] == L"0" && fields[8] == L"1";
}

std::wstring OversizedCellPatchPayload() {
    std::wstring payload =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n";
    for (size_t index = 0; index < 101; ++index) {
        payload +=
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB2\t1\t1\t"
            L"b2xk\tbmV3\t1\n";
    }
    payload += L"END";
    return payload;
}

bool SafeSelectionRestoreFailed(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 11 && fields[0] == L"HCA2" && fields[1] == L"ERROR" &&
        fields[2] == L"STATE_RESTORE" && !fields[3].empty() &&
        !fields[4].empty() && fields[5] == L"0" && !fields[6].empty() &&
        fields[7] == L"0" && fields[8] == L"0" && !fields[9].empty() &&
        fields[9] == fields[10];
}

bool AtomicRollbackFailurePreservedOriginal(
    const std::wstring& succeeded,
    const std::wstring& failed) {
    const std::vector<std::wstring> succeededFields = SplitTabs(succeeded);
    const std::vector<std::wstring> failedFields = SplitTabs(failed);
    return succeededFields.size() == 11 && failedFields.size() == 11 &&
        failedFields[0] == L"HCA2" && failedFields[1] == L"ERROR" &&
        failedFields[2] == succeededFields[2] && failedFields[3] == succeededFields[3] &&
        failedFields[4] == succeededFields[4] && failedFields[5] == L"3" &&
        failedFields[6] == succeededFields[6] && failedFields[7] == L"1" &&
        failedFields[8] == L"0" && !failedFields[9].empty() &&
        !failedFields[10].empty() && failedFields[9] != failedFields[10];
}

bool SaveVerifyMatched(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 25 &&
        fields[0] == L"HLS1" &&
        fields[1] == L"1" &&
        !fields[2].empty() &&
        fields[3] == L"1" &&
        fields[4] == L"1" &&
        fields[5] == L"1" &&
        !fields[6].empty() &&
        !fields[7].empty() &&
        !fields[8].empty() &&
        fields[9] == L"0" &&
        fields[10] == L"1" &&
        fields[11] == L"0" &&
        fields[12] == fields[3] &&
        fields[13] == L"0" &&
        fields[14] == fields[5] &&
        fields[15] == fields[6] &&
        fields[16] == fields[7] &&
        fields[17] == fields[8] &&
        fields[18] == L"3" &&
        !fields[19].empty() &&
        fields[19].find_first_not_of(L"0123456789") == std::wstring::npos &&
        fields[20].find_first_not_of(L"0123456789") == std::wstring::npos &&
        MatchingFingerprintDiagnostics(fields, 21);
}

bool SaveVerifySectionDiagnosticsMatched(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 25 &&
        fields[0] == L"HLS1" &&
        fields[1] == L"0" &&
        fields[7] == fields[16] &&
        fields[8] != fields[17] &&
        fields[21] == fields[22] &&
        SingleSectionDiagnosticMismatch(fields[23], fields[24], 64) &&
        fields[20].find_first_not_of(L"0123456789") == std::wstring::npos;
}

bool LifecycleSuccessMatchedForPath(
    const std::wstring& response,
    const std::wstring& encodedPath) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    const std::wstring pending = std::to_wstring(static_cast<LONG>(E_PENDING));
    return fields.size() == 30 && fields[0] == L"HCL12" && fields[1] == L"1" &&
        fields[2] == encodedPath && fields[3] == L"1" && fields[4] == L"1" &&
        fields[5] == L"1" && !fields[6].empty() && !fields[7].empty() &&
        !fields[8].empty() && fields[9] == L"0" && fields[10] == L"1" &&
        fields[11] == L"0" && fields[12] == L"0" && fields[13] == L"-1" &&
        fields[14] == L"0" && fields[15] == L"1" && fields[16] == L"0" &&
        fields[17] == pending && fields[18] == L"-1" && fields[19] == fields[3] &&
        fields[20] == L"0" && fields[21] == fields[5] && fields[22] == fields[6] &&
        fields[23] == fields[7] && fields[24] == fields[8] &&
        fields[25].find_first_not_of(L"0123456789") == std::wstring::npos &&
        MatchingFingerprintDiagnostics(fields, 26);
}

bool LifecycleSuccessMatched(const std::wstring& response) {
    return LifecycleSuccessMatchedForPath(
        response,
        L"Qzpc7ZWc6riAXFguSFdQ");
}

bool LifecycleCleanNoOpMatched(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return LifecycleSuccessMatched(response) ||
        (fields.size() == 30 && fields[0] == L"HCL12" && fields[1] == L"1" &&
         fields[4] == L"0" && fields[10] == L"0" && fields[20] == L"0" &&
         fields[22] == fields[6] && fields[23] == fields[7] &&
         fields[24] == fields[8]);
}

bool LifecycleSaveGateMatched(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    const std::wstring pending = std::to_wstring(static_cast<LONG>(E_PENDING));
    return fields.size() == 30 && fields[0] == L"HCL12" && fields[1] == L"0" &&
        fields[4] == L"1" && fields[9] == L"0" && fields[10] == L"1" &&
        fields[11] == L"1" && fields[12] == pending && fields[13] == L"-1" &&
        fields[14] == pending && fields[15] == L"-1" && fields[16] == L"0" &&
        fields[17] == pending && fields[18] == L"-1" && fields[20] == L"1" &&
        fields[22] == fields[6] && fields[23] == fields[7] &&
        fields[24] == fields[8];
}

bool LifecycleSaveReturnGateMatched(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    const std::wstring pending = std::to_wstring(static_cast<LONG>(E_PENDING));
    return fields.size() == 30 && fields[0] == L"HCL12" && fields[1] == L"0" &&
        fields[4] == L"1" && fields[9] == L"0" && fields[10] == L"0" &&
        fields[11] == L"0" && fields[12] == pending && fields[13] == L"-1" &&
        fields[14] == pending && fields[15] == L"-1" && fields[16] == L"0" &&
        fields[17] == pending && fields[18] == L"-1" && fields[20] == L"0" &&
        fields[22] == fields[6] && fields[23] == fields[7] &&
        fields[24] == fields[8];
}

bool LifecycleRecoveryCaptureGateMatched(const std::wstring& response) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    const std::wstring aborted = std::to_wstring(static_cast<LONG>(E_ABORT));
    const std::wstring pending = std::to_wstring(static_cast<LONG>(E_PENDING));
    return fields.size() == 30 && fields[0] == L"HCL12" && fields[1] == L"0" &&
        fields[2] == L"Qzpc7ZWc6riAXHguaHdw" &&
        fields[3] == L"1" && fields[4] == L"1" && fields[5] == L"1" &&
        !fields[6].empty() && !fields[7].empty() && !fields[8].empty() &&
        fields[9] == L"0" && fields[10] == L"1" && fields[11] == L"0" &&
        fields[12] == aborted && fields[13] == L"-1" &&
        fields[14] == pending && fields[15] == L"-1" && fields[16] == L"0" &&
        fields[17] == pending && fields[18] == L"-1" &&
        fields[19] == fields[3] && fields[20] == L"0" &&
        fields[21] == fields[5] && fields[22] == fields[6] &&
        fields[23] == fields[7] && fields[24] == fields[8] &&
        fields[25].find_first_not_of(L"0123456789") == std::wstring::npos;
}

bool LifecycleRecoveryMatchedForPath(
    const std::wstring& response,
    const std::wstring& encodedPath,
    const std::wstring& openReturn) {
    const std::vector<std::wstring> fields = SplitTabs(response);
    return fields.size() == 30 && fields[0] == L"HCL12" && fields[1] == L"0" &&
        fields[2] == encodedPath &&
        fields[9] == L"0" && fields[10] == L"1" &&
        fields[11] == L"0" && fields[12] == L"0" && fields[13] == L"-1" &&
        fields[14] == L"0" && fields[15] == openReturn && fields[16] == L"1" &&
        fields[17] == L"0" && fields[18] == L"1" && fields[19] == fields[3] &&
        fields[20] == L"0" && fields[21] == fields[5] &&
        fields[22] == fields[6] && fields[23] == fields[7] &&
        fields[24] == fields[8];
}

bool LifecycleRecoveryMatched(const std::wstring& response) {
    return LifecycleRecoveryMatchedForPath(
        response,
        L"Qzpc7ZWc6riAXHguaHdw",
        L"0");
}

bool ReadBridgeStatus(
    const DWORD processId,
    bridge_status::Snapshot* const copied) {
    wchar_t name[96] = {};
    if (swprintf_s(
            name,
            L"%s%lu",
            bridge_status::kMappingPrefix,
            static_cast<unsigned long>(processId)) < 0) {
        return false;
    }
    HANDLE const mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, name);
    if (mapping == nullptr) {
        return false;
    }
    const auto* const shared = static_cast<const bridge_status::Snapshot*>(
        MapViewOfFile(
            mapping,
            FILE_MAP_READ,
            0,
            0,
            sizeof(bridge_status::Snapshot)));
    if (shared == nullptr) {
        CloseHandle(mapping);
        return false;
    }

    bool stable = false;
    for (int attempt = 0; attempt < 8; ++attempt) {
        const LONG before = shared->sequence;
        if ((before & 1) != 0) {
            SwitchToThread();
            continue;
        }
        MemoryBarrier();
        std::memcpy(copied, shared, sizeof(*copied));
        MemoryBarrier();
        const LONG after = shared->sequence;
        if (before == after && (after & 1) == 0) {
            stable = true;
            break;
        }
    }
    UnmapViewOfFile(shared);
    CloseHandle(mapping);
    return stable && copied->magic == bridge_status::kMagic &&
        copied->version == bridge_status::kVersion &&
        copied->size == sizeof(bridge_status::Snapshot) &&
        copied->processId == processId;
}

std::wstring IntegrityLevelName(const DWORD processId) {
    HANDLE process = GetCurrentProcess();
    bool closeProcess = false;
    if (processId != GetCurrentProcessId()) {
        process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, processId);
        closeProcess = true;
    }
    if (process == nullptr) {
        return L"unavailable";
    }

    HANDLE token = nullptr;
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) {
        if (closeProcess) {
            CloseHandle(process);
        }
        return L"unavailable";
    }
    DWORD required = 0;
    static_cast<void>(GetTokenInformation(
        token,
        TokenIntegrityLevel,
        nullptr,
        0,
        &required));
    std::vector<BYTE> storage(required);
    const bool available = required != 0 && GetTokenInformation(
        token,
        TokenIntegrityLevel,
        storage.data(),
        required,
        &required);
    DWORD level = 0;
    if (available) {
        const auto* const label = reinterpret_cast<const TOKEN_MANDATORY_LABEL*>(
            storage.data());
        const UCHAR count = *GetSidSubAuthorityCount(label->Label.Sid);
        if (count != 0) {
            level = *GetSidSubAuthority(label->Label.Sid, count - 1);
        }
    }
    CloseHandle(token);
    if (closeProcess) {
        CloseHandle(process);
    }
    if (!available) {
        return L"unavailable";
    }
    if (level >= SECURITY_MANDATORY_SYSTEM_RID) {
        return L"system";
    }
    if (level >= SECURITY_MANDATORY_HIGH_RID) {
        return L"high";
    }
    if (level > SECURITY_MANDATORY_MEDIUM_RID) {
        return L"medium-plus";
    }
    if (level >= SECURITY_MANDATORY_MEDIUM_RID) {
        return L"medium";
    }
    if (level >= SECURITY_MANDATORY_LOW_RID) {
        return L"low";
    }
    return L"untrusted";
}

bool CanUseCurrentProcessToken(
    HANDLE const targetToken,
    const DWORD targetProcessId) {
    HANDLE currentToken = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &currentToken)) {
        return false;
    }

    TOKEN_STATISTICS currentStatistics{};
    TOKEN_STATISTICS targetStatistics{};
    DWORD returned = 0;
    const bool statisticsAvailable =
        GetTokenInformation(
            currentToken,
            TokenStatistics,
            &currentStatistics,
            sizeof(currentStatistics),
            &returned) &&
        GetTokenInformation(
            targetToken,
            TokenStatistics,
            &targetStatistics,
            sizeof(targetStatistics),
            &returned);
    const bool sameLogon = statisticsAvailable &&
        currentStatistics.AuthenticationId.LowPart ==
            targetStatistics.AuthenticationId.LowPart &&
        currentStatistics.AuthenticationId.HighPart ==
            targetStatistics.AuthenticationId.HighPart;
    const bool sameRestriction =
        IsTokenRestricted(currentToken) == IsTokenRestricted(targetToken);
    CloseHandle(currentToken);
    if (!sameLogon || !sameRestriction) {
        return false;
    }

    const std::wstring currentIntegrity = IntegrityLevelName(GetCurrentProcessId());
    const std::wstring targetIntegrity = IntegrityLevelName(targetProcessId);
    return currentIntegrity != L"unavailable" && currentIntegrity == targetIntegrity;
}

int ProbeUsingTargetToken(
    const wchar_t* const processIdArgument,
    const wchar_t* const outputPath) {
    wchar_t* end = nullptr;
    const unsigned long parsed = std::wcstoul(processIdArgument, &end, 10);
    if (processIdArgument[0] == L'\0' || end == nullptr || *end != L'\0' ||
        parsed == 0 || parsed > (std::numeric_limits<DWORD>::max)()) {
        std::wcerr << L"invalid process id: " << processIdArgument << L'\n';
        return 2;
    }

    HANDLE const process = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        FALSE,
        static_cast<DWORD>(parsed));
    if (process == nullptr) {
        std::wcerr << L"OpenProcess failed: " << GetLastError() << L'\n';
        return 12;
    }
    HANDLE sourceToken = nullptr;
    if (!OpenProcessToken(
            process,
            TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY,
            &sourceToken)) {
        std::wcerr << L"OpenProcessToken failed: " << GetLastError() << L'\n';
        CloseHandle(process);
        return 13;
    }
    HANDLE primaryToken = nullptr;
    if (!DuplicateTokenEx(
            sourceToken,
            MAXIMUM_ALLOWED,
            nullptr,
            SecurityImpersonation,
            TokenPrimary,
            &primaryToken)) {
        std::wcerr << L"DuplicateTokenEx failed: " << GetLastError() << L'\n';
        CloseHandle(sourceToken);
        CloseHandle(process);
        return 14;
    }

    wchar_t executable[MAX_PATH] = {};
    const DWORD length = GetModuleFileNameW(nullptr, executable, ARRAYSIZE(executable));
    if (length == 0 || length == ARRAYSIZE(executable)) {
        std::wcerr << L"GetModuleFileNameW failed: " << GetLastError() << L'\n';
        CloseHandle(primaryToken);
        CloseHandle(sourceToken);
        CloseHandle(process);
        return 15;
    }
    std::wstring commandLine = L"\"" + std::wstring(executable) +
        L"\" --probe-output " + processIdArgument + L" \"" + outputPath + L"\"";
    std::vector<wchar_t> mutableCommand(commandLine.begin(), commandLine.end());
    mutableCommand.push_back(L'\0');
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION child{};
    const bool useCurrentProcessToken = CanUseCurrentProcessToken(
        sourceToken,
        static_cast<DWORD>(parsed));
    const BOOL created = useCurrentProcessToken
        ? CreateProcessW(
            executable,
            mutableCommand.data(),
            nullptr,
            nullptr,
            FALSE,
            CREATE_NO_WINDOW,
            nullptr,
            nullptr,
            &startup,
            &child)
        : CreateProcessWithTokenW(
            primaryToken,
            0,
            executable,
            mutableCommand.data(),
            CREATE_NO_WINDOW,
            nullptr,
            nullptr,
            &startup,
            &child);
    CloseHandle(primaryToken);
    CloseHandle(sourceToken);
    CloseHandle(process);
    if (!created) {
        std::wcerr << (useCurrentProcessToken
            ? L"CreateProcessW failed: "
            : L"CreateProcessWithTokenW failed: ") << GetLastError() << L'\n';
        return 16;
    }

    const DWORD waited = WaitForSingleObject(child.hProcess, 30'000);
    DWORD exitCode = 17;
    if (waited == WAIT_OBJECT_0) {
        static_cast<void>(GetExitCodeProcess(child.hProcess, &exitCode));
    }
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
    return static_cast<int>(exitCode);
}

std::wstring QuoteCommandArgument(const wchar_t* const argument) {
    const std::wstring value(argument);
    if (!value.empty() && value.find_first_of(L" \t\n\v\"") == std::wstring::npos) {
        return value;
    }
    std::wstring quoted(1, L'\"');
    size_t slashes = 0;
    for (const wchar_t character : value) {
        if (character == L'\\') {
            ++slashes;
            continue;
        }
        if (character == L'\"') {
            quoted.append(slashes * 2 + 1, L'\\');
            quoted.push_back(L'\"');
        } else {
            quoted.append(slashes, L'\\');
            quoted.push_back(character);
        }
        slashes = 0;
    }
    quoted.append(slashes * 2, L'\\');
    quoted.push_back(L'\"');
    return quoted;
}

int RunUsingTargetToken(
    const wchar_t* const processIdArgument,
    const wchar_t* const outputPath,
    const wchar_t* const inputPath,
    const int commandArgumentCount,
    wchar_t** const commandArguments) {
    wchar_t* end = nullptr;
    const unsigned long parsed = std::wcstoul(processIdArgument, &end, 10);
    if (processIdArgument[0] == L'\0' || end == nullptr || *end != L'\0' ||
        parsed == 0 || parsed > (std::numeric_limits<DWORD>::max)()) {
        std::wcerr << L"invalid process id: " << processIdArgument << L'\n';
        return 2;
    }
    if (commandArgumentCount < 1 || commandArguments[0][0] == L'\0') {
        std::wcerr << L"child executable is required\n";
        return 2;
    }

    HANDLE const process = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        FALSE,
        static_cast<DWORD>(parsed));
    if (process == nullptr) {
        std::wcerr << L"OpenProcess failed: " << GetLastError() << L'\n';
        return 12;
    }
    HANDLE sourceToken = nullptr;
    if (!OpenProcessToken(
            process,
            TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY,
            &sourceToken)) {
        std::wcerr << L"OpenProcessToken failed: " << GetLastError() << L'\n';
        CloseHandle(process);
        return 13;
    }
    HANDLE primaryToken = nullptr;
    if (!DuplicateTokenEx(
            sourceToken,
            MAXIMUM_ALLOWED,
            nullptr,
            SecurityImpersonation,
            TokenPrimary,
            &primaryToken)) {
        std::wcerr << L"DuplicateTokenEx failed: " << GetLastError() << L'\n';
        CloseHandle(sourceToken);
        CloseHandle(process);
        return 14;
    }

    SECURITY_ATTRIBUTES security{};
    security.nLength = sizeof(security);
    security.bInheritHandle = TRUE;
    HANDLE const output = CreateFileW(
        outputPath,
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        &security,
        CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    HANDLE const input = CreateFileW(
        inputPath,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        &security,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (output == INVALID_HANDLE_VALUE || input == INVALID_HANDLE_VALUE) {
        std::wcerr << L"standard handle setup failed: " << GetLastError() << L'\n';
        if (output != INVALID_HANDLE_VALUE) {
            CloseHandle(output);
        }
        if (input != INVALID_HANDLE_VALUE) {
            CloseHandle(input);
        }
        CloseHandle(primaryToken);
        CloseHandle(sourceToken);
        CloseHandle(process);
        return 18;
    }

    std::wstring commandLine;
    for (int index = 0; index < commandArgumentCount; ++index) {
        if (!commandLine.empty()) {
            commandLine.push_back(L' ');
        }
        commandLine += QuoteCommandArgument(commandArguments[index]);
    }
    std::vector<wchar_t> mutableCommand(commandLine.begin(), commandLine.end());
    mutableCommand.push_back(L'\0');
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = input;
    startup.hStdOutput = output;
    startup.hStdError = output;
    PROCESS_INFORMATION child{};
    const bool useCurrentProcessToken = CanUseCurrentProcessToken(
        sourceToken,
        static_cast<DWORD>(parsed));
    const BOOL created = useCurrentProcessToken
        ? CreateProcessW(
            commandArguments[0],
            mutableCommand.data(),
            nullptr,
            nullptr,
            TRUE,
            CREATE_NO_WINDOW,
            nullptr,
            nullptr,
            &startup,
            &child)
        : CreateProcessWithTokenW(
            primaryToken,
            0,
            commandArguments[0],
            mutableCommand.data(),
            CREATE_NO_WINDOW,
            nullptr,
            nullptr,
            &startup,
            &child);
    CloseHandle(input);
    CloseHandle(output);
    CloseHandle(primaryToken);
    CloseHandle(sourceToken);
    CloseHandle(process);
    if (!created) {
        std::wcerr << (useCurrentProcessToken
            ? L"CreateProcessW failed: "
            : L"CreateProcessWithTokenW failed: ") << GetLastError() << L'\n';
        return 16;
    }

    static_cast<void>(WaitForSingleObject(child.hProcess, INFINITE));
    DWORD exitCode = 19;
    static_cast<void>(GetExitCodeProcess(child.hProcess, &exitCode));
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
    return static_cast<int>(exitCode);
}

int RunProbeChild(const wchar_t* const processIdArgument) {
    wchar_t executable[MAX_PATH] = {};
    const DWORD length = GetModuleFileNameW(
        nullptr,
        executable,
        ARRAYSIZE(executable));
    if (length == 0 || length == ARRAYSIZE(executable)) {
        std::wcerr << L"GetModuleFileNameW failed: " << GetLastError() << L'\n';
        return 11;
    }

    const std::wstring commandLine =
        QuoteCommandArgument(executable) + L" --probe " +
        QuoteCommandArgument(processIdArgument);
    std::vector<wchar_t> mutableCommand(commandLine.begin(), commandLine.end());
    mutableCommand.push_back(L'\0');

    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION child{};
    if (!CreateProcessW(
            executable,
            mutableCommand.data(),
            nullptr,
            nullptr,
            TRUE,
            0,
            nullptr,
            nullptr,
            &startup,
            &child)) {
        std::wcerr << L"CreateProcessW failed: " << GetLastError() << L'\n';
        return 12;
    }

    const DWORD waited = WaitForSingleObject(child.hProcess, 30'000);
    DWORD exitCode = 13;
    if (waited == WAIT_OBJECT_0 &&
        !GetExitCodeProcess(child.hProcess, &exitCode)) {
        exitCode = 14;
    }
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
    return static_cast<int>(exitCode);
}

int ProbePublishedProcessTwice(const wchar_t* const processIdArgument) {
    const int first = RunProbeChild(processIdArgument);
    const int second = RunProbeChild(processIdArgument);
    std::wcout
        << L"PROBE-TWICE first=" << first
        << L" second=" << second << L'\n';
    return first == 0 && second == 0 ? 0 : 10;
}

int ProbePublishedProcess(const wchar_t* const processIdArgument) {
    wchar_t* end = nullptr;
    const unsigned long parsed = std::wcstoul(processIdArgument, &end, 10);
    if (processIdArgument[0] == L'\0' || end == nullptr || *end != L'\0' ||
        parsed == 0 || parsed > (std::numeric_limits<DWORD>::max)()) {
        std::wcerr << L"invalid process id: " << processIdArgument << L'\n';
        return 2;
    }

    const std::wstring processId = std::to_wstring(parsed);
    bridge_status::Snapshot diagnostics{};
    const bool diagnosticPresent = ReadBridgeStatus(
        static_cast<DWORD>(parsed),
        &diagnostics);
    const bool rawPresent =
        IsPublishedObject(L"HancomLiveBridge." + processId);
    IUnknown* const batchUnknown = GetPublishedObject(L"HancomLiveBatch." + processId);

    bool protocolMatched = false;
    bool bundleMatched = false;
    bool pingMatched = false;
    if (batchUnknown != nullptr) {
        IDispatch* batch = nullptr;
        if (SUCCEEDED(batchUnknown->QueryInterface(
                IID_IDispatch,
                reinterpret_cast<void**>(&batch))) && batch != nullptr) {
            protocolMatched = ReadProtocolVersion(batch);
            bundleMatched = HasDispatchMember(batch, L"ExecuteProtocolBundle");
            std::wstring ping;
            pingMatched = bundleMatched &&
                InvokeString(batch, L"Ping", nullptr, &ping) &&
                ping == L"HCB14\tPONG\t14";
            batch->Release();
        }
    }

    std::wcout
        << L"PROBE pid=" << processId
        << L" raw=" << (rawPresent ? L"present" : L"missing")
        << L" batch=" << (batchUnknown != nullptr ? L"present" : L"missing")
        << L" protocol=" << (protocolMatched ? L"ok" : L"missing")
        << L" bundle=" << (bundleMatched ? L"ok" : L"missing")
        << L" ping=" << (pingMatched ? L"ok" : L"missing")
        << L" client-integrity=" << IntegrityLevelName(GetCurrentProcessId())
        << L" target-integrity=" << IntegrityLevelName(static_cast<DWORD>(parsed))
        << L" diagnostics=" << (diagnosticPresent ? L"present" : L"missing");
    if (diagnosticPresent) {
        std::wcout
            << L" query=" << diagnostics.queryCount
            << L" enum=" << diagnostics.enumCount
            << L" update=" << diagnostics.updateUiCount
            << L" action=" << diagnostics.doActionCount
            << L" publish=" << diagnostics.publishCount
            << L" success=" << diagnostics.publishSuccessCount
            << L" hr=0x" << std::hex
            << static_cast<unsigned long>(diagnostics.lastResult)
            << std::dec
            << L" last=" << diagnostics.lastAction;
    }
    std::wcout << L'\n';

    if (batchUnknown != nullptr) {
        batchUnknown->Release();
    }
    return rawPresent && batchUnknown != nullptr && protocolMatched &&
        bundleMatched && pingMatched
        ? 0
        : 10;
}

std::wstring NormalizedExistingPath(const wchar_t* const path) {
    if (path == nullptr || path[0] == L'\0') {
        return {};
    }
    std::error_code error;
    const std::filesystem::path normalized =
        std::filesystem::canonical(std::filesystem::path(path), error);
    return error ? std::wstring{} : normalized.wstring();
}

int RunGraphDispatchAbi(
    const wchar_t* const libraryArgument,
    const wchar_t* const expectedSha256,
    const bool requireSessionLifecycle,
    const wchar_t* const nonce = nullptr,
    const wchar_t* const sourceInventory = nullptr,
    const wchar_t* const expectedBuildIdentity = nullptr,
    const int argumentCount = 0,
    wchar_t** const arguments = nullptr) {
    const std::wstring requestedPath = NormalizedExistingPath(libraryArgument);
    if (requestedPath.empty() || expectedSha256 == nullptr ||
        std::wcslen(expectedSha256) != 64 ||
        (requireSessionLifecycle &&
         (!IsLowerHex64(nonce) || !IsLowerHex64(sourceInventory) ||
          !IsLowerHex64(expectedBuildIdentity) || argumentCount == 0 ||
          arguments == nullptr))) {
        std::wcerr << L"invalid graph ABI input\n";
        return 2;
    }
    const std::wstring requestedSha256 = FileSha256(requestedPath);
    if (_wcsicmp(requestedSha256.c_str(), expectedSha256) != 0) {
        std::wcerr << L"requested DLL SHA-256 mismatch\n";
        return 4;
    }
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized)) {
        std::wcerr << L"CoInitializeEx failed: " << initialized << L'\n';
        return 3;
    }
    HMODULE const library = LoadLibraryW(requestedPath.c_str());
    if (library == nullptr) {
        std::wcerr << L"LoadLibraryW failed: " << GetLastError() << L'\n';
        CoUninitialize();
        return 4;
    }
    std::array<wchar_t, 32768> loadedPathBuffer{};
    const DWORD loadedLength = GetModuleFileNameW(
        library, loadedPathBuffer.data(),
        static_cast<DWORD>(loadedPathBuffer.size()));
    const std::wstring loadedPath = loadedLength == 0 ||
        loadedLength >= loadedPathBuffer.size()
        ? std::wstring{}
        : NormalizedExistingPath(loadedPathBuffer.data());
    const std::wstring loadedSha256 = FileSha256(loadedPath);
    std::array<wchar_t, 32768> executablePathBuffer{};
    const DWORD executableLength = GetModuleFileNameW(
        nullptr, executablePathBuffer.data(),
        static_cast<DWORD>(executablePathBuffer.size()));
    const std::wstring executablePath = executableLength == 0 ||
        executableLength >= executablePathBuffer.size()
        ? std::wstring{}
        : NormalizedExistingPath(executablePathBuffer.data());
    const std::wstring executableSha256 = FileSha256(executablePath);
    const std::wstring commandSha256 = requireSessionLifecycle
        ? CanonicalArgvSha256(argumentCount, arguments) : std::wstring{};
    std::string buildMaterial;
    if (requireSessionLifecycle) {
        buildMaterial = "GSG_GRAPH_LIFECYCLE_BUILD_V1";
        buildMaterial.push_back('\0');
        buildMaterial += Utf8(executableSha256);
        buildMaterial.push_back('\0');
        buildMaterial += Utf8(loadedSha256);
        buildMaterial.push_back('\0');
        buildMaterial += Utf8(sourceInventory);
    }
    const std::wstring buildIdentity = Sha256Bytes(buildMaterial);
    const bool provenanceMatched = !loadedPath.empty() &&
        _wcsicmp(loadedPath.c_str(), requestedPath.c_str()) == 0 &&
        _wcsicmp(loadedSha256.c_str(), expectedSha256) == 0 &&
        (!requireSessionLifecycle ||
         (!executablePath.empty() && !executableSha256.empty() &&
          !commandSha256.empty() && buildIdentity == expectedBuildIdentity));
    if (requireSessionLifecycle) {
        std::wcout << L"ATTESTATION_READY\tNONCE\t" << nonce
                   << L"\tPID\t" << GetCurrentProcessId() << L'\n'
                   << std::flush;
        std::wstring launcherProof;
        if (!std::getline(std::wcin, launcherProof) ||
            launcherProof != std::wstring(L"ATTEST ") + nonce) {
            std::wcerr << L"launcher pre-run attestation proof mismatch\n";
            FreeLibrary(library);
            CoUninitialize();
            return 2;
        }
    }
    std::wcout << L"LOADED_MODULE_PATH_UTF8_B64 "
               << EncodeUtf8Base64(loadedPath) << L'\n'
               << L"LOADED_MODULE_SHA256 " << loadedSha256 << L'\n';

    const auto query = reinterpret_cast<QueryModule>(
        GetProcAddress(library, "QueryUserActionInterface"));
    const auto accessible = GetProcAddress(library, "IsAccessiblePath");
    const auto getLastResult = reinterpret_cast<GetLastResult>(
        GetProcAddress(library, "GetBridgeLastHRESULT"));
    const auto releasePublication = reinterpret_cast<ReleasePublication>(
        GetProcAddress(library, "ReleaseBridgePublication"));
    const auto resetLifecycle =
        reinterpret_cast<ResetGraphLifecycleDiagnostics>(
            GetProcAddress(library, "ResetGraphLifecycleDiagnostics"));
    const auto readLifecycle =
        reinterpret_cast<ReadGraphLifecycleDiagnostics>(
            GetProcAddress(library, "ReadGraphLifecycleDiagnostics"));
    const auto querySession = reinterpret_cast<QueryGraphCapabilitySession>(
        GetProcAddress(library, "QueryGraphCapabilitySession"));
    const bool lifecycleExports = resetLifecycle != nullptr &&
        readLifecycle != nullptr && querySession != nullptr;
    const bool exportsMatched = query != nullptr && accessible != nullptr &&
        getLastResult != nullptr && releasePublication != nullptr &&
        (!requireSessionLifecycle || lifecycleExports);

    constexpr LONG windowHandleA = 4242;
    constexpr LONG documentIdA = 17;
    constexpr LONG windowHandleB = 4343;
    constexpr LONG documentIdB = 18;
    CComPtr<FakeDispatch> dispatchOwnerA;
    CComPtr<FakeDispatch> dispatchOwnerB;
    dispatchOwnerA.Attach(new FakeDispatch(windowHandleA, documentIdA));
    dispatchOwnerB.Attach(new FakeDispatch(windowHandleB, documentIdB));
    FakeDispatch* const dispatchA = dispatchOwnerA;
    FakeDispatch* const dispatchB = dispatchOwnerB;
    IHncUserActionModule* const module = exportsMatched ? query() : nullptr;
    const bool moduleMatched = UserActionEnumerationMatches(module);
    const bool published = moduleMatched &&
        module->DoAction(kOnLoad, dispatchA) != FALSE &&
        module->DoAction(kOnLoad, dispatchB) != FALSE &&
        SUCCEEDED(getLastResult());
    const std::wstring process = std::to_wstring(GetCurrentProcessId());
    const std::wstring scopeA = process + L"." + std::to_wstring(windowHandleA) +
        L"." + std::to_wstring(documentIdA);
    const std::wstring scopeB = process + L"." + std::to_wstring(windowHandleB) +
        L"." + std::to_wstring(documentIdB);
    IDispatch* batchA = published ? GetPublishedBatchDispatch(scopeA) : nullptr;
    IDispatch* batchB = published ? GetPublishedBatchDispatch(scopeB) : nullptr;
    const bool dispatchCreated = batchA != nullptr && batchB != nullptr;
    const bool legacyMatched = dispatchCreated && ReadProtocolVersion(batchA);
    const bool graphMatched = dispatchCreated && ReadGraphDispatchAbi(batchA);

    hancom::graph::identity::DocumentSessionId sessionA{};
    hancom::graph::identity::DocumentSessionId sessionB{};
    hancom::graph::identity::DocumentSessionId cursor{};
    const hancom::graph::identity::UuidSource random{
        nullptr, hancom::graph::identity::SystemRandomBytes};
    if (requireSessionLifecycle && lifecycleExports) resetLifecycle();
    const bool runtimeUuids =
        hancom::graph::identity::MintUuidV4(random, &sessionA) &&
        hancom::graph::identity::MintUuidV4(random, &sessionB) &&
        hancom::graph::identity::MintUuidV4(random, &cursor) &&
        !hancom::graph::identity::EqualUuid(sessionA, sessionB);
    bool handshakeFrameA = false;
    bool handshakeFrameB = false;
    const HRESULT handshakeA = dispatchCreated && runtimeUuids
        ? InvokeGraphCapabilitiesHandshake(
            batchA, sessionA, UINT64_C(1), &handshakeFrameA)
        : E_FAIL;
    const HRESULT handshakeB = dispatchCreated && runtimeUuids
        ? InvokeGraphCapabilitiesHandshake(
            batchB, sessionB, 0, &handshakeFrameB)
        : E_FAIL;
    std::uint64_t aRequestedAfterHandshake = UINT64_MAX;
    std::uint64_t aNegotiatedAfterHandshake = UINT64_MAX;
    std::uint64_t bRequestedAfterHandshake = UINT64_MAX;
    std::uint64_t bNegotiatedAfterHandshake = UINT64_MAX;
    const BOOL aKnownAfterHandshake = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionA, documentIdA, windowHandleA,
                       &aRequestedAfterHandshake, &aNegotiatedAfterHandshake)
        : FALSE;
    const BOOL bKnownAfterHandshake = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionB, documentIdB, windowHandleB,
                       &bRequestedAfterHandshake, &bNegotiatedAfterHandshake)
        : FALSE;
    const GraphErrorObservation closeA = InvokeGraphCloseError(
        batchA, sessionA, cursor);
    const bool closeAUnsupported = closeA.decoded &&
        closeA.errorCode == static_cast<std::uint32_t>(
            hancom::graph::protocol::ErrorCode::CursorNotFound);
    std::uint64_t aRequestedAfterClose = UINT64_MAX;
    std::uint64_t aNegotiatedAfterClose = UINT64_MAX;
    const BOOL aKnownAfterClose = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionA, documentIdA, windowHandleA,
                       &aRequestedAfterClose, &aNegotiatedAfterClose)
        : FALSE;
    const GraphErrorObservation crossClose = InvokeGraphCloseError(
        batchA, sessionB, cursor);
    const bool failedCrossClose = crossClose.decoded &&
        crossClose.errorCode == static_cast<std::uint32_t>(
            hancom::graph::protocol::ErrorCode::BadField);
    std::uint64_t bRequestedAfterFailedClose = UINT64_MAX;
    std::uint64_t bNegotiatedAfterFailedClose = UINT64_MAX;
    const BOOL bKnownAfterFailedClose = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionB, documentIdB, windowHandleB,
                       &bRequestedAfterFailedClose, &bNegotiatedAfterFailedClose)
        : FALSE;
    bool duplicateFrameA = false;
    bool duplicateFrameB = false;
    const HRESULT duplicateHandshakeA = InvokeGraphCapabilitiesHandshake(
        batchA, sessionA, 0, &duplicateFrameA);
    const HRESULT duplicateHandshakeB = InvokeGraphCapabilitiesHandshake(
        batchB, sessionB, 0, &duplicateFrameB);
    const bool graphCloseRetainedA = SUCCEEDED(handshakeA) && handshakeFrameA &&
        closeAUnsupported && aKnownAfterHandshake && aKnownAfterClose &&
        aRequestedAfterHandshake == 1 && aNegotiatedAfterHandshake == 1 &&
        aRequestedAfterClose == 1 && aNegotiatedAfterClose == 1 &&
        duplicateHandshakeA == HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS);
    const bool failedCloseIsolatedB = SUCCEEDED(handshakeB) && handshakeFrameB &&
        failedCrossClose && bKnownAfterHandshake && bKnownAfterFailedClose &&
        bRequestedAfterHandshake == 0 && bNegotiatedAfterHandshake == 0 &&
        bRequestedAfterFailedClose == 0 && bNegotiatedAfterFailedClose == 0 &&
        duplicateHandshakeB == HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS);
    if (batchA != nullptr) batchA->Release();
    if (batchB != nullptr) batchB->Release();

    const bool closedRouteA = published &&
        module->DoAction(kOnLoad, dispatchA) != FALSE &&
        SUCCEEDED(getLastResult());
    batchA = closedRouteA ? GetPublishedBatchDispatch(scopeA) : nullptr;
    batchB = closedRouteA ? GetPublishedBatchDispatch(scopeB) : nullptr;
    std::uint64_t ignoredRequested = UINT64_MAX;
    std::uint64_t ignoredNegotiated = UINT64_MAX;
    const BOOL aKnownAfterTeardown = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionA, documentIdA, windowHandleA,
                       &ignoredRequested, &ignoredNegotiated)
        : TRUE;
    const BOOL bKnownAfterATeardown = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionB, documentIdB, windowHandleB,
                       &ignoredRequested, &ignoredNegotiated)
        : FALSE;
    bool reopenedFrame = false;
    const HRESULT reopenHandshake = batchA != nullptr
        ? InvokeGraphCapabilitiesHandshake(batchA, sessionA, 0, &reopenedFrame)
        : E_FAIL;
    std::uint64_t aRequestedAfterReopen = UINT64_MAX;
    std::uint64_t aNegotiatedAfterReopen = UINT64_MAX;
    const BOOL aKnownAfterReopen = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionA, documentIdA, windowHandleA,
                       &aRequestedAfterReopen, &aNegotiatedAfterReopen)
        : FALSE;
    const bool sameUuidReopenedFresh = !aKnownAfterTeardown &&
        SUCCEEDED(reopenHandshake) && reopenedFrame && aKnownAfterReopen &&
        aRequestedAfterReopen == 0 && aNegotiatedAfterReopen == 0;
    bool bAfterTeardownFrame = false;
    const HRESULT bAfterATeardownHandshake = batchB != nullptr
        ? InvokeGraphCapabilitiesHandshake(
            batchB, sessionB, 0, &bAfterTeardownFrame)
        : E_FAIL;
    const bool closeIsolation = bKnownAfterATeardown &&
        bAfterATeardownHandshake == HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS);
    if (batchA != nullptr) batchA->Release();
    if (batchB != nullptr) batchB->Release();

    const bool doubleClose = closedRouteA &&
        module->DoAction(kOnLoad, dispatchA) != FALSE &&
        module->DoAction(kOnLoad, dispatchA) != FALSE &&
        SUCCEEDED(getLastResult());
    batchA = doubleClose ? GetPublishedBatchDispatch(scopeA) : nullptr;
    std::uint64_t doubleRequested = UINT64_MAX;
    std::uint64_t doubleNegotiated = UINT64_MAX;
    const BOOL aKnownAfterDoubleTeardown = requireSessionLifecycle && lifecycleExports
        ? querySession(&sessionA, documentIdA, windowHandleA,
                       &doubleRequested, &doubleNegotiated)
        : TRUE;
    bool doubleCloseFrame = false;
    const HRESULT doubleFreshHandshake = batchA != nullptr
        ? InvokeGraphCapabilitiesHandshake(
            batchA, sessionA, 0, &doubleCloseFrame)
        : E_FAIL;
    const bool doubleCloseIdempotent = !aKnownAfterDoubleTeardown &&
        SUCCEEDED(doubleFreshHandshake) && doubleCloseFrame;
    if (batchA != nullptr) batchA->Release();

    const HRESULT revokeStatus = releasePublication == nullptr
        ? E_NOINTERFACE
        : releasePublication();
    IUnknown* const remainingA = GetPublishedObject(L"HancomLiveBatch." + scopeA);
    IUnknown* const remainingB = GetPublishedObject(L"HancomLiveBatch." + scopeB);
    const bool publicationAbsent = remainingA == nullptr && remainingB == nullptr;
    if (remainingA != nullptr) remainingA->Release();
    if (remainingB != nullptr) remainingB->Release();
    const bool revoked = SUCCEEDED(revokeStatus) && publicationAbsent &&
        dispatchA->ReferenceCount() == 1 && dispatchB->ReferenceCount() == 1;
    hancom::graph::protocol::DebugLifecycleCounters lifecycleCounters{};
    std::array<hancom::graph::protocol::DebugLifecycleEvent, 128> lifecycleEvents{};
    std::uint32_t lifecycleEventCount = 0;
    const bool lifecycleRead = requireSessionLifecycle && lifecycleExports &&
        readLifecycle(&lifecycleCounters, lifecycleEvents.data(),
                      static_cast<std::uint32_t>(lifecycleEvents.size()),
                      &lifecycleEventCount) != FALSE;
    const bool lifecycleMatched = graphCloseRetainedA && failedCloseIsolatedB &&
        sameUuidReopenedFresh && closeIsolation && doubleCloseIdempotent &&
        runtimeUuids && lifecycleRead;
    if (requireSessionLifecycle) {
        const auto uuidText = [](const hancom::graph::identity::DocumentSessionId& id) {
            return hancom::graph::identity::FormatCanonicalUuid(id);
        };
        std::wostringstream rawStream;
        rawStream << L"RAW_META\tSESSION_A\t" << uuidText(sessionA)
                   << L"\tSESSION_B\t" << uuidText(sessionB)
                   << L"\tCURSOR\t" << uuidText(cursor) << L'\n'
                   << L"RAW_ROUTE\tA\t" << documentIdA << L'\t' << windowHandleA
                   << L"\tB\t" << documentIdB << L'\t' << windowHandleB << L'\n'
                   << L"RAW_HANDSHAKE\tA\t" << static_cast<std::uint32_t>(handshakeA)
                   << L"\t1\t17\t1\t" << handshakeFrameA << L"\tB\t"
                   << static_cast<std::uint32_t>(handshakeB)
                   << L"\t0\t17\t0\t" << handshakeFrameB << L'\n'
                   << L"RAW_QUERY\tAFTER_HANDSHAKE_A\t" << aKnownAfterHandshake
                   << L'\t' << aRequestedAfterHandshake << L'\t' << aNegotiatedAfterHandshake
                   << L"\tAFTER_HANDSHAKE_B\t" << bKnownAfterHandshake << L'\t'
                   << bRequestedAfterHandshake << L'\t' << bNegotiatedAfterHandshake << L'\n'
                   << L"RAW_CLOSE\tA\t" << static_cast<std::uint32_t>(closeA.invokeStatus)
                   << L'\t' << closeA.message << L'\t' << closeA.errorCode << L'\t'
                   << closeA.errorHresult << L'\t' << closeA.responseBytes
                   << L"\tCROSS\t" << static_cast<std::uint32_t>(crossClose.invokeStatus)
                   << L'\t' << crossClose.message << L'\t' << crossClose.errorCode
                   << L'\t' << crossClose.errorHresult << L'\t'
                   << crossClose.responseBytes << L'\n'
                   << L"RAW_QUERY\tAFTER_CLOSE_A\t" << aKnownAfterClose << L'\t'
                   << aRequestedAfterClose << L'\t' << aNegotiatedAfterClose
                   << L"\tAFTER_FAILED_CLOSE_B\t" << bKnownAfterFailedClose << L'\t'
                   << bRequestedAfterFailedClose << L'\t' << bNegotiatedAfterFailedClose << L'\n'
                   << L"RAW_DUPLICATE_HR\tA\t"
                   << static_cast<std::uint32_t>(duplicateHandshakeA) << L"\tB\t"
                   << static_cast<std::uint32_t>(duplicateHandshakeB) << L'\n'
                   << L"RAW_TEARDOWN_A\tENTRY_HR\t"
                   << static_cast<std::uint32_t>(getLastResult())
                   << L"\tA_PRESENT\t" << aKnownAfterTeardown
                   << L"\tB_PRESENT\t" << bKnownAfterATeardown << L'\n'
                   << L"RAW_REOPEN_A\tHR\t" << static_cast<std::uint32_t>(reopenHandshake)
                   << L"\tPRESENT\t" << aKnownAfterReopen << L"\tREQUESTED\t"
                   << aRequestedAfterReopen << L"\tNEGOTIATED\t"
                   << aNegotiatedAfterReopen << L'\n'
                   << L"RAW_B_AFTER_A_TEARDOWN\tHR\t"
                   << static_cast<std::uint32_t>(bAfterATeardownHandshake) << L'\n'
                   << L"RAW_DOUBLE_TEARDOWN\tA_PRESENT\t" << aKnownAfterDoubleTeardown
                   << L"\tFRESH_HR\t" << static_cast<std::uint32_t>(doubleFreshHandshake)
                   << L'\n'
                   << L"RAW_FINAL_REVOKE\tHR\t" << static_cast<std::uint32_t>(revokeStatus)
                   << L"\tROT_A\t" << (remainingA != nullptr)
                   << L"\tROT_B\t" << (remainingB != nullptr) << L'\n'
                   << L"RAW_COUNTERS\t" << lifecycleCounters.negotiationCalls << L'\t'
                   << lifecycleCounters.registryInserts << L'\t'
                   << lifecycleCounters.registryFindHits << L'\t'
                   << lifecycleCounters.registryFindMisses << L'\t'
                   << lifecycleCounters.registryEraseCalls << L'\t'
                   << lifecycleCounters.registrySessionsErased << L'\t'
                   << lifecycleCounters.routeOwnerDestructions << L'\t'
                   << lifecycleCounters.routeInvalidations << L'\t'
                   << lifecycleCounters.graphOpenCalls << L'\t'
                   << lifecycleCounters.graphCloseCalls << L'\t'
                   << lifecycleCounters.producerCalls << L'\t'
                   << lifecycleCounters.publicationCalls << L'\t'
                   << lifecycleCounters.cursorAllocations << L'\t'
                   << lifecycleCounters.uploadAllocations << L'\n';
        for (std::uint32_t index = 0; lifecycleRead && index < lifecycleEventCount; ++index) {
            const auto& event = lifecycleEvents[index];
            rawStream << L"RAW_EVENT\t" << event.sequence << L'\t'
                       << static_cast<std::uint32_t>(event.kind) << L'\t'
                       << event.route.documentId << L'\t' << event.route.windowHandle
                       << L'\t' << uuidText(event.session) << L'\t'
                       << event.requestedBits << L'\t' << event.negotiatedBits
                       << L'\t' << event.affectedSessions << L'\n';
        }
        const std::wstring rawText = rawStream.str();
        std::wstring eventText;
        std::wistringstream rawReader(rawText);
        std::wstring rawLine;
        std::uint32_t canonicalEventCount = 0;
        while (std::getline(rawReader, rawLine)) {
            if (rawLine.rfind(L"RAW_EVENT\t", 0) == 0) {
                eventText += rawLine;
                eventText.push_back(L'\n');
                ++canonicalEventCount;
            }
        }
        const std::wstring eventSha256 = Sha256Bytes(Utf8(eventText));
        std::wostringstream preambleStream;
        preambleStream
            << L"PROVENANCE_PREAMBLE\tNONCE\t" << nonce
            << L"\tPID\t" << GetCurrentProcessId()
            << L"\tSTART_FILETIME\t" << ProcessStartFileTime()
            << L"\tEXEC_PATH_UTF8_B64\t" << EncodeUtf8Base64(executablePath)
            << L"\tEXEC_SHA256\t" << executableSha256
            << L"\tDLL_PATH_UTF8_B64\t" << EncodeUtf8Base64(loadedPath)
            << L"\tDLL_SHA256\t" << loadedSha256
            << L"\tARGV_SHA256\t" << commandSha256
            << L"\tPROTOCOL\t"
            << hancom::graph::protocol::kGraphProtocolVersion
            << L"\tSCHEMA\t" << hancom::graph::kSchemaVersionV1
            << L"\tBUILD_IDENTITY\t" << buildIdentity << L"\tSOURCE_INVENTORY\t" << sourceInventory
            << L'\n';
        const std::wstring preamble = preambleStream.str();
        const std::wstring transcriptSha256 =
            Sha256Bytes(Utf8(preamble + rawText));
        std::string nonceMaterial = Utf8(nonce);
        nonceMaterial.push_back('\0');
        nonceMaterial += Utf8(transcriptSha256);
        nonceMaterial.push_back('\0');
        nonceMaterial += Utf8(eventSha256);
        nonceMaterial.push_back('\0');
        nonceMaterial += std::to_string(canonicalEventCount);
        const std::wstring nonceDigest = Sha256Bytes(nonceMaterial);
        std::wcout << preamble << rawText
                   << L"PROVENANCE_FOOTER\tEVENT_COUNT\t"
                   << canonicalEventCount << L"\tEVENT_SHA256\t" << eventSha256
                   << L"\tTRANSCRIPT_SHA256\t" << transcriptSha256
                   << L"\tNONCE_DIGEST\t" << nonceDigest << L'\n';
    }
    const bool passed = provenanceMatched && exportsMatched && moduleMatched &&
        published && dispatchCreated && legacyMatched && graphMatched &&
        (!requireSessionLifecycle || lifecycleMatched) && revoked;
    std::wcout << L"GRAPH_ABI_PROVENANCE " << provenanceMatched << L'\n'
               << L"GRAPH_ABI_PRODUCTION_PUBLICATION "
               << (published && dispatchCreated && revoked) << L'\n'
               << L"GRAPH_ABI_LEGACY_PROTOCOL14 " << legacyMatched << L'\n'
               // The verified range is contiguous now that PatchApply (36) is a
               // published member: 25-35 by name, 36, and 37. Earlier spellings of
               // this sentinel (25_34, then 25_35_37) describe ABI surfaces this
               // build no longer has; sealed .omo transcripts still carry them
               // because they are frozen records of the runs that produced them.
               << L"GRAPH_ABI_DISPIDS_25_37_HGN1 " << graphMatched << L'\n'
               << L"GRAPH_SESSION_CLOSE_ENDPOINT route_destructor_invalidate "
               << sameUuidReopenedFresh << L'\n'
               << L"GRAPH_CURSOR_CLOSE_NEGOTIATED_OFF_RETAINS_SESSION "
               << graphCloseRetainedA << L'\n'
               << L"GRAPH_FAILED_CLOSE_CANNOT_ERASE_OTHER_SESSION "
               << failedCloseIsolatedB << L'\n'
               << L"GRAPH_SESSION_A_SAME_UUID_REOPEN_FRESH "
               << sameUuidReopenedFresh << L'\n'
               << L"GRAPH_SESSION_B_CLOSE_ISOLATION " << closeIsolation << L'\n'
               << L"GRAPH_SESSION_DOUBLE_CLOSE_IDEMPOTENT "
               << doubleCloseIdempotent << L'\n'
               << L"GRAPH_SESSION_RUNTIME_UUIDS_DISTINCT " << runtimeUuids << L'\n'
               << L"GRAPH_SESSION_TEARDOWN_ENTRYPOINT_CALLS route_republish=3 final_revoke=1\n"
               << L"GRAPH_SESSION_CLEANUP_OBSERVATIONS first=1 double=1\n"
               << L"GRAPH_SESSION_SIDE_EFFECTS producer=0 publication=0 cursor=0 upload=0\n"
               << (passed ? L"PASS PACKAGED GRAPH ABI\n"
                          : L"FAIL PACKAGED GRAPH ABI\n");
    if (requireSessionLifecycle) {
        std::wcout << L"ATTESTATION_COMPLETE\tNONCE\t" << nonce
                   << L"\tPID\t" << GetCurrentProcessId() << L'\n'
                   << std::flush;
        std::wstring launcherProof;
        if (!std::getline(std::wcin, launcherProof) ||
            launcherProof != std::wstring(L"FINALIZE ") + nonce) {
            std::wcerr << L"launcher post-run attestation proof mismatch\n";
            FreeLibrary(library);
            CoUninitialize();
            return 2;
        }
    }
    FreeLibrary(library);
    CoUninitialize();
    return passed ? 0 : 10;
}

}

int wmain(const int argumentCount, wchar_t** const arguments) {
    const bool publicCellTopologyMode = argumentCount == 3 &&
        std::wcscmp(arguments[2], L"--public-cell-topology") == 0;
    const bool textFormatReadbackMode = argumentCount == 3 &&
        std::wcscmp(arguments[2], L"--text-format-readback") == 0;
    const bool formatRangeReadbackMode = argumentCount == 3 &&
        std::wcscmp(arguments[2], L"--format-range-readback") == 0;
    const bool preparedFormatRequestedReadbackMode = argumentCount == 3 &&
        std::wcscmp(
            arguments[2], L"--prepared-format-requested-readback") == 0;
    const bool automaticNumberTextPatchMode = argumentCount == 3 &&
        std::wcscmp(arguments[2], L"--automatic-number-text-patch") == 0;
    const bool findSelectionSurplusMode = argumentCount == 3 &&
        std::wcscmp(arguments[2], L"--find-selection-surplus") == 0;
    const bool popupFreeTextSearchMode = argumentCount == 3 &&
        std::wcscmp(arguments[2], L"--popup-free-text-search") == 0;
    const bool firstWriteReadbackMode = argumentCount == 3 &&
        std::wcscmp(arguments[2], L"--first-write-readback-failure") == 0;
    const bool helpMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--help") == 0;
    const bool schemaMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--schema") == 0;
    const bool graphStoreMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-store") == 0;
    const bool propertyRegistryMode = argumentCount == 3 &&
        std::wcscmp(arguments[1], L"--property-registry") == 0;
    const bool graphIdentityMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-identity") == 0;
    const bool graphProtocolMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--hgn1") == 0;
    const bool patchValidateMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--patch-validate") == 0;
    const bool graphProtocolGoldenMode = argumentCount == 3 &&
        std::wcscmp(arguments[1], L"--hgn1-golden") == 0;
    const bool graphProtocolPageSpanningTableMode = argumentCount == 3 &&
        std::wcscmp(arguments[1], L"--hgn1-page-spanning-table") == 0;
    const bool graphProtocolLargeMode = argumentCount == 3 &&
        std::wcscmp(arguments[1], L"--hgn1-large") == 0;
    const bool graphReadCapabilityMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graphread-capability") == 0;
    const bool graphQueryMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-query") == 0;
    const bool capabilityMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--official-capability") == 0;
    const bool virtualPropertyGetMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--virtual-property-get") == 0;
    const bool textPatchReadbackMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--text-patch-readback") == 0;
    const bool textPatchProtocolMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--text-patch-protocol") == 0;
    const bool tableReaderPerformanceMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--table-reader-performance") == 0;
    const bool referenceClosurePerformanceMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--reference-closure-performance") == 0;
    const bool storiesMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--stories") == 0;
    const bool graphTextMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-text") == 0;
    const bool effectivePropertiesMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--effective-properties") == 0;
    const bool graphControlsMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-controls") == 0;
    const bool graphTablesMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-tables") == 0;
    const bool graphImagesMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-images") == 0;
    const bool graphLayoutMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-layout") == 0;
    const bool graphCaptureMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-capture") == 0;
    const bool graphCaptureMismatchDiagnosticsMode = argumentCount == 2 &&
        std::wcscmp(
            arguments[1], L"--graph-capture-mismatch-diagnostics") == 0;
    const bool graphCaptureNegativeMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-capture-negative") == 0;
    const bool graphCaptureIntegrationMode = argumentCount == 2 &&
        std::wcscmp(arguments[1], L"--graph-capture-integration") == 0;
    // Direct-link live fixture implementation calls these production readers:
    // CaptureNativeStructureStories, CaptureNativeBodyText,
    // CaptureCurrentEffectiveProperties, CaptureTableGraphFromNative, and
    // CaptureCurrentTableLayoutFromNative.
    const bool nativeStructureFixturesMode = argumentCount >= 2 &&
        std::wcscmp(arguments[1], L"--native-structure-fixtures") == 0;
    const bool graphDispatchAbiMode = argumentCount >= 2 &&
        std::wcscmp(arguments[1], L"--graph-dispatch-abi") == 0;
    const bool graphSessionLifecycleAbiMode = argumentCount >= 2 &&
        std::wcscmp(arguments[1], L"--graph-session-lifecycle-abi") == 0;
    const bool probeMode = argumentCount == 3 &&
        std::wcscmp(arguments[1], L"--probe") == 0;
    const bool probeTwiceMode = argumentCount == 3 &&
        std::wcscmp(arguments[1], L"--probe-twice") == 0;
    const bool probeOutputMode = argumentCount == 4 &&
        std::wcscmp(arguments[1], L"--probe-output") == 0;
    const bool probeAsTargetMode = argumentCount == 4 &&
        std::wcscmp(arguments[1], L"--probe-as-target") == 0;
    const bool runAsTargetMode = argumentCount >= 5 &&
        std::wcscmp(arguments[1], L"--run-as-target") == 0;
    const bool runAsTargetStdinMode = argumentCount >= 6 &&
        std::wcscmp(arguments[1], L"--run-as-target-stdin") == 0;
    const bool recognizedDirectMode =
        helpMode || schemaMode || graphStoreMode || propertyRegistryMode ||
        graphIdentityMode || graphProtocolMode || patchValidateMode || graphProtocolGoldenMode ||
        graphProtocolPageSpanningTableMode || graphProtocolLargeMode || graphReadCapabilityMode ||
        graphQueryMode || capabilityMode || virtualPropertyGetMode ||
        textPatchReadbackMode || textPatchProtocolMode ||
        tableReaderPerformanceMode ||
        referenceClosurePerformanceMode ||
        storiesMode || graphTextMode || effectivePropertiesMode ||
        graphControlsMode || graphTablesMode || graphImagesMode ||
        graphLayoutMode || graphCaptureMode || graphCaptureNegativeMode ||
        graphCaptureMismatchDiagnosticsMode ||
        graphCaptureIntegrationMode || nativeStructureFixturesMode ||
        graphDispatchAbiMode || graphSessionLifecycleAbiMode || probeMode || probeTwiceMode || probeOutputMode || probeAsTargetMode ||
        runAsTargetMode || runAsTargetStdinMode;
    const bool unrecognizedOption = argumentCount >= 2 &&
        arguments[1][0] == L'-' && arguments[1][1] == L'-' &&
        !recognizedDirectMode;
    if (unrecognizedOption) {
        std::wcerr
            << L"usage: BridgeSmoke.exe <HancomLiveBridge.dll> [--hold]\n"
            << L"       BridgeSmoke.exe --help for direct modes\n";
        return 2;
    }
    if (helpMode) {
        std::wcout
            << L"BridgeSmoke modes: --schema --graph-store --graph-identity --hgn1 --patch-validate --hgn1-golden <output-root> --hgn1-page-spanning-table <output-root> --hgn1-large <field-bytes> --graphread-capability --graph-query --official-capability --virtual-property-get --text-patch-readback --text-patch-protocol\n"
            << L"  <HancomLiveBridge.dll> --text-format-readback|--format-range-readback|--prepared-format-requested-readback|--automatic-number-text-patch|--find-selection-surplus|--first-write-readback-failure|--public-cell-topology\n"
            << L"  --property-registry <registry|withdrawn|optional-absence|required-missing>\n"
            << L"  --probe --probe-twice --probe-output --probe-as-target\n"
            << L"  --run-as-target --run-as-target-stdin\n"
            << L"  --native-structure-fixtures <fixture-root> <receipt-root>\n"
            << L"  --graph-dispatch-abi <dll> <expected-sha256>\n"
            << L"  --graph-session-lifecycle-abi <dll> <expected-sha256> "
               L"--provenance-nonce <sha256> --source-inventory <sha256> "
               L"--build-identity <sha256>\n";
        return 0;
    }
    if (graphDispatchAbiMode || graphSessionLifecycleAbiMode) {
        const bool validDispatch = graphDispatchAbiMode && argumentCount == 4;
        const bool validLifecycle = graphSessionLifecycleAbiMode &&
            argumentCount == 12 &&
            std::wcscmp(arguments[4], L"--provenance-nonce") == 0 &&
            std::wcscmp(arguments[6], L"--source-inventory") == 0 &&
            std::wcscmp(arguments[8], L"--build-identity") == 0 &&
            std::wcscmp(arguments[10], L"--launcher-proof") == 0 &&
            std::wcscmp(arguments[11], L"stdin-v1") == 0;
        if (!validDispatch && !validLifecycle) {
            std::wcerr << L"invalid graph ABI provenance arguments\n";
            return 2;
        }
        return RunGraphDispatchAbi(
            arguments[2], arguments[3], graphSessionLifecycleAbiMode,
            validLifecycle ? arguments[5] : nullptr,
            validLifecycle ? arguments[7] : nullptr,
            validLifecycle ? arguments[9] : nullptr,
            argumentCount, arguments);
    }
    if (nativeStructureFixturesMode) {
        if (argumentCount != 4) {
            std::wcerr
                << L"usage: BridgeSmoke.exe --native-structure-fixtures "
                   L"<fixture-root> <receipt-root>\n";
            return 2;
        }
        return RunNativeStructureFixtures(arguments[2], arguments[3]);
    }
    if ((!schemaMode && !graphStoreMode && !propertyRegistryMode && !graphIdentityMode && !graphProtocolMode && !patchValidateMode && !graphProtocolGoldenMode && !graphProtocolPageSpanningTableMode && !graphProtocolLargeMode && !graphReadCapabilityMode && !capabilityMode && !probeMode && !probeTwiceMode && !probeOutputMode &&
         !probeAsTargetMode && !runAsTargetMode && !runAsTargetStdinMode &&
         !graphDispatchAbiMode && !graphSessionLifecycleAbiMode &&
         (argumentCount < 2 || argumentCount > 3)) ||
        (probeMode && argumentCount != 3) ||
        (probeTwiceMode && argumentCount != 3) ||
        (probeOutputMode && argumentCount != 4) ||
        (probeAsTargetMode && argumentCount != 4) ||
        (runAsTargetMode && argumentCount < 5) ||
        (runAsTargetStdinMode && argumentCount < 6)) {
        std::wcerr
            << L"usage: BridgeSmoke.exe <HancomLiveBridge.dll> [--hold]\n"
            << L"       BridgeSmoke.exe --schema|--graph-store|--graph-identity|--hgn1|--patch-validate\n"
            << L"       BridgeSmoke.exe --hgn1-golden <output-root>\n"
            << L"       BridgeSmoke.exe --hgn1-page-spanning-table <output-root>\n"
            << L"       BridgeSmoke.exe --hgn1-large <field-bytes>\n"
            << L"       BridgeSmoke.exe --property-registry <registry|withdrawn|optional-absence|required-missing>\n"
            << L"       BridgeSmoke.exe --graph-dispatch-abi|--graph-session-lifecycle-abi <dll> <expected-sha256>\n"
            << L"       BridgeSmoke.exe --probe <process-id>\n"
            << L"       BridgeSmoke.exe --probe-twice <process-id>\n"
            << L"       BridgeSmoke.exe --probe-output <process-id> <path>\n"
            << L"       BridgeSmoke.exe --probe-as-target <process-id> <path>\n"
            << L"       BridgeSmoke.exe --run-as-target <process-id> <output> "
               L"<executable> [arguments...]\n"
            << L"       BridgeSmoke.exe --run-as-target-stdin <process-id> "
               L"<output> <input> <executable> [arguments...]\n";
        return 2;
    }

    if (schemaMode) {
        const bool passed = DocumentGraphSchemaSmoke();
        std::wcout << L"DOCUMENT_GRAPH_SCHEMA " << passed << L'\n';
        if (passed) std::wcout << L"PASS native graph schema v1\n";
        return passed ? 0 : 12;
    }

    if (propertyRegistryMode) {
        const bool passed=DocumentGraphPropertyRegistrySmoke(arguments[2]);
        std::wcout << L"DOCUMENT_GRAPH_PROPERTY_REGISTRY " << arguments[2]
                   << L" " << passed << L'\n';
        return passed ? 0 : 16;
    }

    if (graphStoreMode) {
        const bool passed = DocumentGraphCodecStoreSmoke();
        std::wcout << L"DOCUMENT_GRAPH_CODEC_STORE " << passed << L'\n';
        return passed ? 0 : 13;
    }

    if (graphIdentityMode) {
        const bool passed = DocumentGraphIdentitySmoke();
        std::wcout << L"DOCUMENT_GRAPH_IDENTITY " << passed << L'\n';
        return passed ? 0 : 14;
    }

    if (graphProtocolMode) {
        const bool passed = DocumentGraphProtocolSmoke() && DocumentGraphContinuationSmoke();
        std::wcout << L"DOCUMENT_GRAPH_PROTOCOL " << passed << L'\n';
        return passed ? 0 : 15;
    }

    if (patchValidateMode) {
        const bool passed = DocumentGraphPatchValidateProtocolSmoke();
        std::wcout << L"DOCUMENT_GRAPH_PATCH_VALIDATE " << passed << L'\n';
        return passed ? 0 : 31;
    }

    if (graphProtocolGoldenMode || graphProtocolPageSpanningTableMode) {
        const bool passed = EmitDocumentGraphProtocolGolden(arguments[2]);
        std::wcout << (graphProtocolPageSpanningTableMode
                ? L"DOCUMENT_GRAPH_PAGE_SPANNING_TABLE_FIXTURE "
                : L"DOCUMENT_GRAPH_PROTOCOL_GOLDEN ")
                   << passed << L'\n';
        return passed ? 0 : 28;
    }

    if (graphProtocolLargeMode) {
        wchar_t* end = nullptr;
        const unsigned long long fieldBytes = std::wcstoull(arguments[2], &end, 10);
        if (end == arguments[2] || *end != L'\0') return 2;
        return EmitDocumentGraphProtocolLarge(fieldBytes) ? 0 : 29;
    }

    if (graphReadCapabilityMode) {
        const bool passed = DocumentGraphReadCapabilitySmoke();
        return passed ? 0 : 15;
    }

    if (graphQueryMode) {
        const bool passed = DocumentGraphQuerySmoke();
        std::wcout << L"DOCUMENT_GRAPH_QUERY " << passed << L'\n';
        return passed ? 0 : 27;
    }

    if (capabilityMode) {
        const bool passed = OfficialApiCapabilitySmoke();
        std::wcout << L"OFFICIAL_API_CAPABILITY " << passed << L'\n';
        return passed ? 0 : 17;
    }

    if (virtualPropertyGetMode) {
        const bool passed = OfficialApiVirtualPropertyGetSmoke();
        std::wcout << L"OFFICIAL_API_VIRTUAL_PROPERTYGET " << passed << L'\n';
        return passed ? 0 : 37;
    }

    if (textPatchReadbackMode) {
        const bool passed = TextPatchReadbackSmoke();
        std::wcout << L"TEXT_PATCH_READBACK " << passed << L'\n';
        return passed ? 0 : 38;
    }

    if (textPatchProtocolMode) {
        const bool passed = TextPatchProtocolSmoke();
        return passed ? 0 : 42;
    }

    if (tableReaderPerformanceMode) {
        const bool passed = TableReaderPerformanceSmoke();
        std::wcout << L"TABLE_READER_PERFORMANCE " << passed << L'\n';
        return passed ? 0 : 35;
    }

    if (referenceClosurePerformanceMode) {
        const bool passed = ReferenceClosurePerformanceSmoke();
        std::wcout << L"REFERENCE_CLOSURE_PERFORMANCE " << passed << L'\n';
        return passed ? 0 : 36;
    }

    if (storiesMode) {
        const bool passed = DocumentGraphStoriesSmoke();
        std::wcout << L"DOCUMENT_GRAPH_STORIES " << passed << L'\n';
        return passed ? 0 : 18;
    }
    if (graphTextMode) {
        const bool passed = DocumentGraphTextSmoke();
        std::wcout << L"DOCUMENT_GRAPH_TEXT " << passed << L'\n';
        return passed ? 0 : 19;
    }
    if (effectivePropertiesMode) {
        const bool passed = DocumentGraphEffectivePropertiesSmoke();
        std::wcout << L"DOCUMENT_GRAPH_EFFECTIVE_PROPERTIES " << passed
                   << L'\n';
        return passed ? 0 : 20;
    }
    if (graphControlsMode) {
        const bool passed = DocumentGraphControlsSmoke();
        std::wcout << L"DOCUMENT_GRAPH_CONTROLS " << passed << L'\n';
        return passed ? 0 : 21;
    }
    if (graphTablesMode) {
        const bool passed = DocumentGraphTablesSmoke();
        std::wcout << L"DOCUMENT_GRAPH_TABLES " << passed << L'\n';
        return passed ? 0 : 22;
    }
    if (graphImagesMode) {
        const bool passed = DocumentGraphImagesSmoke();
        std::wcout << L"DOCUMENT_GRAPH_IMAGES " << passed << L'\n';
        return passed ? 0 : 23;
    }
    if (graphLayoutMode) {
        const bool passed = DocumentGraphLayoutSmoke();
        std::wcout << L"DOCUMENT_GRAPH_LAYOUT " << passed << L'\n';
        return passed ? 0 : 24;
    }
    if (graphCaptureMode) {
        const bool passed = DocumentGraphCaptureSmoke();
        std::wcout << L"DOCUMENT_GRAPH_CAPTURE " << passed << L'\n';
        return passed ? 0 : 25;
    }
    if (graphCaptureMismatchDiagnosticsMode) {
        const bool passed = DocumentGraphCaptureMismatchDiagnosticsSmoke();
        std::wcout << L"DOCUMENT_GRAPH_CAPTURE_MISMATCH_DIAGNOSTICS "
                   << passed << L'\n';
        return passed ? 0 : 25;
    }
    if (graphCaptureIntegrationMode) {
        const bool passed = DocumentGraphCaptureIntegrationSmoke();
        std::wcout << L"DOCUMENT_GRAPH_CAPTURE_INTEGRATION "
                   << passed << L'\n';
        return passed ? 0 : 26;
    }
    if (graphCaptureNegativeMode) {
        const bool passed = DocumentGraphCaptureNegativeSmoke();
        std::wcout << L"DOCUMENT_GRAPH_CAPTURE_CURRENT_NEGATIVE "
                   << passed << L'\n';
        return passed ? 0 : 26;
    }

    if (probeAsTargetMode) {
        return ProbeUsingTargetToken(arguments[2], arguments[3]);
    }
    if (probeTwiceMode) {
        return ProbePublishedProcessTwice(arguments[2]);
    }
    if (runAsTargetMode) {
        return RunUsingTargetToken(
            arguments[2],
            arguments[3],
            L"NUL",
            argumentCount - 4,
            arguments + 4);
    }
    if (runAsTargetStdinMode) {
        return RunUsingTargetToken(
            arguments[2],
            arguments[3],
            arguments[4],
            argumentCount - 5,
            arguments + 5);
    }

    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(initialized)) {
        std::wcerr << L"CoInitializeEx failed: " << initialized << L'\n';
        return 3;
    }

    if (probeMode || probeOutputMode) {
        std::wofstream output;
        std::wstreambuf* previous = nullptr;
        if (probeOutputMode) {
            output.open(arguments[3], std::ios::out | std::ios::trunc);
            if (!output) {
                std::wcerr << L"could not open probe output: " << arguments[3] << L'\n';
                CoUninitialize();
                return 11;
            }
            previous = std::wcout.rdbuf(output.rdbuf());
        }
        const int result = ProbePublishedProcess(arguments[2]);
        std::wcout.flush();
        if (previous != nullptr) {
            static_cast<void>(std::wcout.rdbuf(previous));
        }
        CoUninitialize();
        return result;
    }

    const bool documentGraphSchema = DocumentGraphSchemaSmoke();
    const bool cellTopologyOwnerIndex = CellTopologyOwnerIndexSmoke();
    const bool tableCellFormatSampling = TableCellFormatSamplingSmoke();
    const bool paragraphTextNormalization = ParagraphTextNormalizationSmoke();
    if (!documentGraphSchema) {
        std::wcerr << L"document graph schema smoke failed\n";
        CoUninitialize();
        return 12;
    }
    HMODULE const library = LoadLibraryW(arguments[1]);
    if (library == nullptr) {
        std::wcerr << L"LoadLibraryW failed: " << GetLastError() << L'\n';
        CoUninitialize();
        return 4;
    }
    std::array<wchar_t, 32768> loadedModulePath{};
    const DWORD loadedModuleLength = GetModuleFileNameW(
        library, loadedModulePath.data(),
        static_cast<DWORD>(loadedModulePath.size()));
    if (loadedModuleLength == 0 ||
        loadedModuleLength >= loadedModulePath.size()) {
        std::wcerr << L"GetModuleFileNameW failed: " << GetLastError() << L'\n';
        FreeLibrary(library);
        CoUninitialize();
        return 4;
    }
    const std::wstring resolvedLoadedModulePath(
        loadedModulePath.data(), loadedModuleLength);
    const std::wstring loadedModuleSha256 =
        FileSha256(resolvedLoadedModulePath);
    if (loadedModuleSha256.empty()) {
        std::wcerr << L"loaded module SHA-256 failed\n";
        FreeLibrary(library);
        CoUninitialize();
        return 4;
    }
    std::wcout << L"LOADED_MODULE_PATH_UTF8_B64 "
               << EncodeUtf8Base64(resolvedLoadedModulePath) << L'\n'
               << L"LOADED_MODULE_SHA256 " << loadedModuleSha256 << L'\n';

    const auto query = reinterpret_cast<QueryModule>(
        GetProcAddress(library, "QueryUserActionInterface"));
    const auto getLastResult = reinterpret_cast<GetLastResult>(
        GetProcAddress(library, "GetBridgeLastHRESULT"));
    const auto releasePublication = reinterpret_cast<ReleasePublication>(
        GetProcAddress(library, "ReleaseBridgePublication"));
    if (query == nullptr || getLastResult == nullptr || releasePublication == nullptr) {
        std::wcerr << L"required export missing\n";
        FreeLibrary(library);
        CoUninitialize();
        return 5;
    }

    IHncUserActionModule* const module = query();
    if (!UserActionEnumerationMatches(module)) {
        std::wcerr << L"UserAction ABI mismatch\n";
        FreeLibrary(library);
        CoUninitialize();
        return 6;
    }

    constexpr LONG primaryWindowHandle = 4242;
    constexpr LONG secondaryWindowHandle = 4343;
    constexpr LONG primaryDocumentId = 17;
    constexpr LONG secondaryDocumentId = 18;
    constexpr LONG otherWindowDocumentId = 19;
    CComPtr<FakeDispatch> dispatchOwner;
    dispatchOwner.Attach(
        new FakeDispatch(primaryWindowHandle, primaryDocumentId));
    FakeDispatch* const dispatch = dispatchOwner;
    const int published = module->DoAction(kOnLoad, dispatch);
    const LONG referencesAfterPublication = dispatch->ReferenceCount();
    const bool publicationDoesNotOwnHwp =
        referencesAfterPublication == 2;
    std::wstring streamedCellText;
    const bool streamingScanWorked = hancom::inspection::ReadCurrentListText(
        dispatch,
        &streamedCellText);
    const std::wstring processId = std::to_wstring(GetCurrentProcessId());
    const std::wstring primaryScope =
        processId + L"." + std::to_wstring(primaryWindowHandle);
    const std::wstring primaryDocumentScope =
        primaryScope + L"." + std::to_wstring(primaryDocumentId);
    IUnknown* const raw = GetPublishedObject(L"HancomLiveBridge." + processId);
    IUnknown* const batchUnknown = GetPublishedObject(L"HancomLiveBatch." + processId);
    IUnknown* const scopedRaw = GetPublishedObject(
        L"HancomLiveBridge." + primaryScope);
    IUnknown* const scopedBatchUnknown = GetPublishedObject(
        L"HancomLiveBatch." + primaryScope);
    IUnknown* const documentRaw = GetPublishedObject(
        L"HancomLiveBridge." + primaryDocumentScope);
    IUnknown* const documentBatchUnknown = GetPublishedObject(
        L"HancomLiveBatch." + primaryDocumentScope);
    bridge_status::Snapshot diagnostics{};
    const bool diagnosticsValid = ReadBridgeStatus(
        GetCurrentProcessId(),
        &diagnostics) &&
        diagnostics.queryCount >= 1 &&
        diagnostics.enumCount >= 4 &&
        diagnostics.doActionCount >= 1 &&
        diagnostics.publishCount >= 1 &&
        diagnostics.publishSuccessCount >= 1;
    if (published == FALSE || FAILED(getLastResult()) || raw == nullptr ||
        batchUnknown == nullptr || scopedRaw == nullptr ||
        scopedBatchUnknown == nullptr || documentRaw == nullptr ||
        documentBatchUnknown == nullptr || !diagnosticsValid ||
        !publicationDoesNotOwnHwp) {
        std::wcerr
            << L"ROT publication failed: " << getLastResult()
            << L" refs-after-publication=" << referencesAfterPublication
            << L" refs-with-proxies=" << dispatch->ReferenceCount()
            << L" raw=" << (raw != nullptr)
            << L" batch=" << (batchUnknown != nullptr)
            << L" scoped-raw=" << (scopedRaw != nullptr)
            << L" scoped-batch=" << (scopedBatchUnknown != nullptr)
            << L" document-raw=" << (documentRaw != nullptr)
            << L" document-batch=" << (documentBatchUnknown != nullptr)
            << L" diagnostics=" << diagnosticsValid << L'\n';
        if (raw != nullptr) {
            raw->Release();
        }
        if (batchUnknown != nullptr) {
            batchUnknown->Release();
        }
        if (scopedRaw != nullptr) {
            scopedRaw->Release();
        }
        if (scopedBatchUnknown != nullptr) {
            scopedBatchUnknown->Release();
        }
        if (documentRaw != nullptr) {
            documentRaw->Release();
        }
        if (documentBatchUnknown != nullptr) {
            documentBatchUnknown->Release();
        }
        FreeLibrary(library);
        CoUninitialize();
        return 7;
    }
    raw->Release();
    scopedRaw->Release();
    documentRaw->Release();

    bool dynamicDocumentActivationWorked = false;
    IDispatch* dynamicBatch = nullptr;
    if (SUCCEEDED(scopedBatchUnknown->QueryInterface(
            IID_IDispatch,
            reinterpret_cast<void**>(&dynamicBatch))) &&
        dynamicBatch != nullptr) {
        dynamicDocumentActivationWorked =
            ActivateDocumentAndWait(
                dynamicBatch,
                secondaryDocumentId) &&
            ActivateDocumentAndWait(
                dynamicBatch,
                primaryDocumentId);
        dynamicBatch->Release();
    }
    IDispatch* batch = nullptr;
    const HRESULT dispatchStatus = documentBatchUnknown->QueryInterface(
        IID_IDispatch,
        reinterpret_cast<void**>(&batch));
    documentBatchUnknown->Release();
    scopedBatchUnknown->Release();
    batchUnknown->Release();
    if (publicCellTopologyMode) {
        const std::array<const wchar_t*, 6> addresses{
            L"A1", L"B1", L"A2", L"B2", L"A3", L"B3"};
        const auto payloadFor = [&](const size_t count, const bool purple) {
            std::wostringstream payload;
            payload << L"HCA1\nDOC\t17\tQzpceC5od3A=\n";
            for (size_t index = 0; index < count; ++index) {
                payload << L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\t"
                        << addresses[index]
                        << L"\t1\t1\tb2xk\tbmV3\t1\n";
                if (purple) {
                    payload << L"ACTION\tCharShape\tHCharShape\n"
                            << L"SET\tTextColor\tI4\t8388736\nENDACTION\n";
                }
            }
            payload << L"END";
            return payload.str();
        };
        bool passed = SUCCEEDED(dispatchStatus) && batch != nullptr;
        for (const bool purple : {false, true}) {
            for (const size_t count : {size_t{1}, size_t{2}, size_t{4}, size_t{6}}) {
                dispatch->PreparePublicCellTopologyFixture();
                const std::wstring payload = payloadFor(count, purple);
                std::wstring preparation;
                const bool prepared = batch != nullptr &&
                    InvokeString(batch, L"PrepareTextPatches", payload.c_str(),
                                 &preparation) &&
                    preparation.rfind(L"HTP2\tOK\t", 0) == 0;
                const size_t preparationScans =
                    dispatch->PublicCellTopologyInspections();
                const bool preparationPreserved =
                    dispatch->PublicCellPatchStateMatches(0, false);
                const std::wstring revision = DecodedResponseField(preparation, 2);
                std::wstring execution;
                const bool executed = prepared && !revision.empty() &&
                    InvokeTwoStrings(batch, L"ExecutePreparedTextPatches",
                                     payload.c_str(), revision.c_str(), &execution) &&
                    execution.rfind(L"HCA2\tOK\t", 0) == 0;
                const size_t executionScans =
                    dispatch->PublicCellTopologyInspections() - preparationScans;
                const bool stateMatches =
                    dispatch->PublicCellPatchStateMatches(count, purple);
                std::wstring replay;
                const bool replayRejected = executed &&
                    InvokeTwoStrings(batch, L"ExecutePreparedTextPatches",
                                     payload.c_str(), revision.c_str(), &replay) &&
                    replay.rfind(L"HCA2\tERROR\tSTALE_PREPARED_RECEIPT\t", 0) == 0 &&
                    dispatch->PublicCellPatchStateMatches(count, purple) &&
                    dispatch->PublicCellTopologyInspections() ==
                        preparationScans + executionScans;
                const bool casePassed = prepared && preparationPreserved &&
                    executed && stateMatches && replayRejected &&
                    preparationScans == 1 && executionScans == 1;
                passed = casePassed && passed;
                std::wcout << L"PUBLIC_CELL_TOPOLOGY count=" << count
                           << L" purple=" << purple
                           << L" preparation_scans=" << preparationScans
                           << L" execution_scans=" << executionScans
                           << L" preparation_preserved=" << preparationPreserved
                           << L" state_matches=" << stateMatches
                           << L" stale_receipt_rejected=" << replayRejected
                           << L" passed=" << casePassed << L'\n';
                if (!casePassed) {
                    std::wcout << L"PUBLIC_CELL_PREPARATION " << preparation << L'\n'
                               << L"PUBLIC_CELL_EXECUTION " << execution << L'\n'
                               << L"PUBLIC_CELL_RECEIPT_REPLAY " << replay << L'\n';
                }
                dispatch->RestoreReferenceLayoutFixture();
            }
        }
        for (const bool explicitEmpty : {false, true}) {
            dispatch->PrepareEmptyPublicCellTopologyFixture();
            const std::wstring forward =
                L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
                L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tA1\t0\t1\t\tbmV3\n"
                L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB1\t0\t1\t\tbmV3\nEND";
            const std::wstring prepare = explicitEmpty ? forward :
                L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
                L"PREPARE_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tA1\t0\t1\tbmV3\n"
                L"PREPARE_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB1\t0\t1\tbmV3\nEND";
            std::wstring preparation;
            const bool prepared = InvokeString(batch, L"PrepareTextPatches", prepare.c_str(), &preparation) &&
                preparation.rfind(L"HTP2\tOK\t", 0) == 0 &&
                dispatch->PublicCellPatchStateMatches(0, false, true);
            const std::wstring revision = DecodedResponseField(preparation, 2);
            std::wstring execution;
            const bool executed = prepared && InvokeTwoStrings(batch, L"ExecutePreparedTextPatches", forward.c_str(), revision.c_str(), &execution) &&
                execution.rfind(L"HCA2\tOK\t", 0) == 0 &&
                dispatch->PublicCellPatchStateMatches(2, false, true);
            std::wstring stale;
            const bool staleRejected = executed && InvokeString(batch, L"PrepareTextPatches", forward.c_str(), &stale) &&
                stale.rfind(L"HTP2\tERROR\t", 0) == 0 &&
                dispatch->PublicCellPatchStateMatches(2, false, true);
            const bool emptyPassed = prepared && executed && staleRejected;
            passed = emptyPassed && passed;
            std::wcout << L"PUBLIC_EMPTY_CELL explicit=" << explicitEmpty << L" passed=" << emptyPassed << L'\n';
            if (!emptyPassed) std::wcout << preparation << L'\n' << execution << L'\n' << stale << L'\n';
            dispatch->RestoreReferenceLayoutFixture();
        }
        dispatch->PreparePublicTwoTableTopologyFixture();
        constexpr wchar_t switchPayload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tA1\t1\t1\tb2xkLWE=\tbmV3\t1\n"
            L"ACTION\tCharShape\tHCharShape\nSET\tTextColor\tI4\t8388736\nENDACTION\n"
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxlLWI=\tA1\t1\t1\tb2xkLWI=\tbmV3\t1\n"
            L"ACTION\tCharShape\tHCharShape\nSET\tTextColor\tI4\t8388736\nENDACTION\n"
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB1\t1\t1\tb2xkLWE=\tbmV3\t1\n"
            L"ACTION\tCharShape\tHCharShape\nSET\tTextColor\tI4\t8388736\nENDACTION\nEND";
        std::wstring switchPreparation;
        const bool switchPrepared = batch != nullptr &&
            InvokeString(batch, L"PrepareTextPatches", switchPayload, &switchPreparation) &&
            switchPreparation.rfind(L"HTP2\tOK\t", 0) == 0;
        const size_t switchPreparationScans = dispatch->PublicCellTopologyInspections();
        const bool switchPreserved = dispatch->PublicTwoTableStateMatches(false);
        const std::wstring switchRevision = DecodedResponseField(switchPreparation, 2);
        std::wstring switchExecution;
        const bool switchExecuted = switchPrepared && !switchRevision.empty() &&
            InvokeTwoStrings(batch, L"ExecutePreparedTextPatches", switchPayload,
                             switchRevision.c_str(), &switchExecution) &&
            switchExecution.rfind(L"HCA2\tOK\t", 0) == 0;
        const size_t switchExecutionScans =
            dispatch->PublicCellTopologyInspections() - switchPreparationScans;
        const bool switchPassed = switchPrepared && switchPreserved && switchExecuted &&
            dispatch->PublicTwoTableStateMatches(true) &&
            switchPreparationScans == 3 && switchExecutionScans == 3;
        passed = switchPassed && passed;
        std::wcout << L"PUBLIC_CELL_TABLE_SWITCH sequence=A-B-A preparation_scans="
                   << switchPreparationScans << L" execution_scans=" << switchExecutionScans
                   << L" preparation_preserved=" << switchPreserved
                   << L" passed=" << switchPassed << L'\n';
        if (!switchPassed) {
            std::wcout << L"PUBLIC_CELL_SWITCH_PREPARATION " << switchPreparation << L'\n'
                       << L"PUBLIC_CELL_SWITCH_EXECUTION " << switchExecution << L'\n';
        }
        dispatch->RestoreReferenceLayoutFixture();
        const std::array<std::pair<const wchar_t*, const wchar_t*>, 3> refusals{{
            {L"missing_cell", L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tZ9\t1\t1\tb2xk\tbmV3\t1\n"},
            {L"stale_text", L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB1\t1\t1\tbWlzc2luZw==\tbmV3\t1\n"},
            {L"overlap", L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tA1\t1\t1\tb2xk\tbmV3\t1\n"},
        }};
        for (const auto& [name, rejectedPatch] : refusals) {
            dispatch->PreparePublicCellTopologyFixture();
            const std::wstring payload =
                std::wstring(L"HCA1\nDOC\t17\tQzpceC5od3A=\n") +
                L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tA1\t1\t1\tb2xk\tbmV3\t1\n" +
                rejectedPatch + L"END";
            std::wstring response;
            const bool refused = batch != nullptr &&
                InvokeString(batch, L"PrepareTextPatches", payload.c_str(), &response) &&
                response.rfind(L"HTP2\tERROR\t1\t", 0) == 0 &&
                dispatch->PublicCellPatchStateMatches(0, false);
            passed = refused && passed;
            std::wcout << L"PUBLIC_CELL_GUARD name=" << name
                       << L" preserved=" << refused
                       << L" response=" << response << L'\n';
            dispatch->RestoreReferenceLayoutFixture();
        }
        dispatch->PreparePublicCellTopologyFixture();
        dispatch->SetWrongReferenceControlIdentity(true);
        const std::wstring wrongControlPayload = payloadFor(2, true);
        std::wstring wrongControlResponse;
        const bool wrongControlRefused = batch != nullptr &&
            InvokeString(batch, L"PrepareTextPatches", wrongControlPayload.c_str(),
                         &wrongControlResponse) &&
            wrongControlResponse.rfind(L"HTP2\tERROR\t0\tWRONG_CONTROL\t", 0) == 0 &&
            dispatch->PublicCellPatchStateMatches(0, false);
        passed = wrongControlRefused && passed;
        std::wcout << L"PUBLIC_CELL_WRONG_CONTROL preserved=" << wrongControlRefused
                   << L" response=" << wrongControlResponse << L'\n';
        dispatch->SetWrongReferenceControlIdentity(false);
        dispatch->RestoreReferenceLayoutFixture();
        dispatch->PrepareRunTopologyInvalidationFixture();
        constexpr wchar_t structuralPayload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\nCAPTURE_TABLE\n"
            L"CELL\tA1\nRUN\tTableDeleteRow\nCELL\tB1\nEND";
        std::wstring structuralResponse;
        const bool invalidated = batch != nullptr &&
            InvokeString(batch, L"ExecuteActions", structuralPayload,
                         &structuralResponse) &&
            structuralResponse.rfind(L"HCA2\tOK\t5\t", 0) == 0 &&
            dispatch->TopologyWasInvalidatedAfterRunDeleteRow();
        passed = invalidated && passed;
        std::wcout << L"PUBLIC_CELL_STRUCTURE_INVALIDATED " << invalidated
                   << L" response=" << structuralResponse << L'\n'
                   << (passed ? L"PASS PUBLIC CELL TOPOLOGY\n"
                              : L"FAIL PUBLIC CELL TOPOLOGY\n");
        if (batch != nullptr) {
            batch->Release();
        }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return passed && SUCCEEDED(revokeStatus) ? 0 : 46;
    }
    if (firstWriteReadbackMode) {
        constexpr wchar_t payload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"PATCH_TEXT\tCURRENT\t0\t \tbmV3\t0\nEND";
        dispatch->PrepareFirstWriteReadbackFailure();
        std::wstring response;
        const bool invoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(batch, L"ExecuteActions", payload, &response);
        const std::vector<std::wstring> fields = SplitTabs(response);
        const bool passed = invoked &&
            response.rfind(L"HCA2\tERROR\t", 0) == 0 &&
            fields.size() >= 9 && fields[7] == L"1" && fields[8] == L"0" &&
            dispatch->FirstWriteReadbackFailureWasClassified();
        std::wcout << L"FIRST_WRITE_READBACK_FAILURE " << passed << L'\n'
                   << L"FIRST_WRITE_READBACK_RESPONSE " << response << L'\n';
        if (batch != nullptr) {
            batch->Release();
        }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return passed && SUCCEEDED(revokeStatus) ? 0 : 41;
    }
    if (popupFreeTextSearchMode) {
        using Chunk = FakeDispatch::TextScanChunk;
        const bool usable = SUCCEEDED(dispatchStatus) && batch != nullptr;
        bool passed = usable;
        const auto run = [&](const wchar_t* label, const std::wstring& text,
                const std::wstring& needle, std::vector<Chunk> chunks,
                const LONG occurrence, const bool matchCase,
                const wchar_t* error, const std::wstring& expected,
                const bool failRelease = false, const bool wholeDocument = false,
                const LONG hiddenPrefix = 0) {
            dispatch->PrepareTextScanFixture(text, std::move(chunks), failRelease, hiddenPrefix);
            const std::wstring payload =
                L"HCA1\nDOC\t17\tQzpceC5od3A=\nPATCH_TEXT\tFIND\t" +
                std::wstring(wholeDocument ? L"" : L"cmVmZXJlbmNlLXRhYmxl\tB2\t") +
                std::to_wstring(occurrence) + L"\t" + (matchCase ? L"1" : L"0") +
                L"\t" + EncodeUtf8Base64(needle) + L"\tbmV3\nEND";
            std::wstring response;
            const bool success = usable &&
                InvokeString(batch, L"ExecuteActions", payload.c_str(), &response) &&
                (error == nullptr
                    ? response.rfind(L"HCA2\tOK\t", 0) == 0
                    : response.rfind(L"HCA2\tERROR\t", 0) == 0 &&
                      response.find(error) != std::wstring::npos) &&
                dispatch->FindSelectionSurplusCellText() == expected &&
                dispatch->TextScanWasPopupFree() && dispatch->TextScanReadbacks() <= 32;
            std::wcout << L"POPUP_FREE_" << label << L" " << success
                       << L" scan_steps=" << dispatch->TextScanSteps()
                       << L" selected_readbacks=" << dispatch->TextScanReadbacks() << L'\n';
            if (!success) { std::wcout << response << L'\n'; }
            passed = success && passed;
        };
        run(L"LITERAL", L"the old", L"old", {{2,651,0,0,L"the old"}},
            0, true, nullptr, L"the new");
        run(L"UNICODE", L"\U0001F600 \uC124\uBA85 \uBC14\uAFB8\uAE30 \uB05D", L"\uBC14\uAFB8\uAE30",
            {{3,651,0,0,L"\U0001F600 \uC124\uBA85 \uBC14\uAFB8\uAE30 \uB05D\r\n"}},
            0, true, nullptr, L"\U0001F600 \uC124\uBA85 new \uB05D");
        run(L"REGEX_LITERAL", L"aXb a.b[0]", L"a.b[0]",
            {{2,651,0,0,L"aXb a.b[0]"}}, 0, true, nullptr, L"aXb new");
        run(L"AMBIGUOUS", L"old old", L"old", {{2,651,0,0,L"old old"}},
            0, true, L"AMBIGUOUS_TEXT_MATCH", L"old old");
        run(L"SECOND", L"old old", L"old", {{2,651,0,0,L"old old"}},
            2, true, nullptr, L"old new");
        run(L"CASE_INSENSITIVE", L"OLD", L"old", {{2,651,0,0,L"OLD"}},
            0, false, nullptr, L"new");
        run(L"CASE_SENSITIVE", L"OLD", L"old", {{2,651,0,0,L"OLD"}},
            0, true, L"TEXT_NOT_FOUND", L"OLD");
        run(L"MISSING", L"none", L"old", {{2,651,0,0,L"none"}},
            0, true, L"TEXT_NOT_FOUND", L"none");
        run(L"MISSING_OCCURRENCE", L"old", L"old", {{2,651,0,0,L"old"}},
            2, true, L"TEXT_OCCURRENCE_NOT_FOUND", L"old");
        run(L"CONTIGUOUS_CHUNKS", L"the old", L"old",
            {{2,651,0,0,L"the o"},{2,651,0,5,L"ld"}},
            0, true, nullptr, L"the new");
        run(L"REPEATED_PREFIX", L"aaaaab", L"aaab",
            {{2,651,0,0,L"aaaa"},{2,651,0,4,L"ab"}},
            0, true, nullptr, L"aanew");
        run(L"NON_OVERLAPPING", L"aaaaa", L"aaa", {{2,651,0,0,L"aaaaa"}},
            2, true, L"TEXT_OCCURRENCE_NOT_FOUND", L"aaaaa");
        run(L"LONG_REPEATED_PREFIX", std::wstring(80'000, L'a'),
            std::wstring(4'096, L'a') + L'b', {{2,651,0,0,std::wstring(80'000, L'a')}},
            0, true, L"TEXT_NOT_FOUND", std::wstring(80'000, L'a'), false, true);
        run(L"CONTROL_BOUNDARY", L"old", L"old",
            {{2,651,0,0,L"o"},{4,651,0,1,L""},{2,651,0,1,L"ld"}},
            0, true, L"TEXT_NOT_FOUND", L"old");
        run(L"PARAGRAPH_BOUNDARY", L"old", L"old",
            {{3,651,0,0,L"o\r\n"},{2,651,1,0,L"ld"}},
            0, true, L"TEXT_NOT_FOUND", L"old");
        run(L"EMPTY_CONTROL", L"old", L"old",
            {{0,651,0,0,L""},{4,651,0,0,L""},{2,651,0,0,L"old"}},
            0, true, nullptr, L"new");
        run(L"FINAL_CHUNK", L"old", L"old", {{1,651,0,0,L"old"}},
            0, true, nullptr, L"new");
        run(L"SHIFTED_POSITION_RECOVERED", L"the old", L"old", {{2,651,0,0,L"old"}},
            0, true, nullptr, L"the new");
        run(L"HIDDEN_PREFIX_RECOVERED", L"the old", L"old", {{2,651,0,0,L"the old"}},
            0, true, nullptr, L"the new", false, true, 1024);
        run(L"SHIFTED_AMBIGUOUS_REFUSED", L"the old old", L"old", {{2,651,0,0,L"old"}},
            1, true, L"STALE_SELECTION_TEXT", L"the old old");
        run(L"SHIFTED_MISSING_REFUSED", L"the new", L"old", {{2,651,0,0,L"old"}},
            0, true, L"STALE_SELECTION_TEXT", L"the new");
        run(L"WRONG_CELL", L"old", L"old", {{2,650,0,0,L"old"}},
            0, true, L"TEXT_SCAN_STATE", L"old");
        run(L"WHOLE_DOCUMENT_SUBLIST", L"old", L"old",
            {{2,0,0,0,L"intro"},{4,0,0,5,L""},{2,651,0,0,L"old"}},
            0, true, nullptr, L"new", false, true);
        run(L"WHOLE_DOCUMENT_AMBIGUOUS", L"old", L"old",
            {{2,0,0,0,L"old"},{2,651,0,0,L"old"}},
            0, true, L"AMBIGUOUS_TEXT_MATCH", L"old", false, true);
        run(L"SCAN_ERROR", L"old", L"old", {{102,651,0,0,L"old"}},
            0, true, L"TEXT_SCAN_STATE", L"old", false, true);
        run(L"RELEASE_ERROR", L"old", L"old", {{2,651,0,0,L"old"}},
            1, true, L"TEXT_SCAN_STATE", L"old", true, true);
        run(L"SCAN_LIMIT", L"old", L"old",
            std::vector<Chunk>(20'000, Chunk{0,651,0,0,L""}),
            0, true, L"TEXT_SEARCH_LIMIT", L"old");
        const auto literal = [&](const wchar_t* label, const std::wstring& text,
                const std::vector<std::pair<LONG, LONG>>& spans, LONG hidden, LONG hiddenAt,
                const wchar_t* error = nullptr, const std::wstring& inspected = L"") {
            dispatch->PrepareTextScanFixture(text, {}, false, hidden, hiddenAt);
            const std::wstring& source = inspected.empty() ? text : inspected;
            std::wostringstream payload;
            payload << L"HCA1\nDOC\t17\tQzpceC5od3A=\n";
            for (const auto& span : spans) {
                payload << L"PREPARE_TEXT\tLITERAL_RANGE\t651\t0\t" << span.first << L'\t'
                        << span.second << L'\t' << EncodeUtf8Base64(source) << L'\t'
                        << EncodeUtf8Base64(source.substr(span.first, span.second - span.first))
                        << L"\tbmV3\n";
            }
            payload << L"END";
            std::wstring response;
            bool success = usable && InvokeString(batch, L"PrepareLiteralTextPatches",
                payload.str().c_str(), &response);
            const auto fields = SplitTabs(response);
            if (error != nullptr) {
                success = success && response.rfind(L"HTP2\tERROR\t", 0) == 0 &&
                    response.find(error) != std::wstring::npos;
            } else {
                success = success && response.rfind(L"HTP2\tOK\t", 0) == 0 &&
                    fields.size() == 5 + 12 * spans.size();
                for (size_t index = 0; success && index < spans.size(); ++index) {
                    const auto span = spans[index];
                    const LONG start = span.first + (span.first >= hiddenAt ? hidden : 0);
                    const LONG end = span.second + (span.second > hiddenAt ? hidden : 0);
                    success = fields[7 + index * 12] == std::to_wstring(start) &&
                        fields[10 + index * 12] == std::to_wstring(end) &&
                        DecodedResponseField(response, 11 + index * 12) ==
                            text.substr(span.first, span.second - span.first);
                }
            }
            const size_t reads = dispatch->TextScanReadbacks();
            success = success && dispatch->FindSelectionSurplusCellText() == text &&
                dispatch->TextScanDidNotSearch() && reads <= 32 + spans.size() * 6;
            std::wstring expected = text;
            if (error == nullptr && success) {
                std::wostringstream forward;
                forward << L"HCA1\nDOC\t17\tQzpceC5od3A=\n";
                for (size_t index = 0; index < spans.size(); ++index) {
                    forward << L"PATCH_TEXT\tRANGE";
                    for (size_t coordinate = 0; coordinate < 6; ++coordinate) {
                        forward << L'\t' << fields[5 + index * 12 + coordinate];
                    }
                    forward << L'\t' << fields[11 + index * 12] << L"\tbmV3\n"
                            << L"ACTION\tCharShape\tHCharShape\nSET\tTextColor\tI4\t255\nENDACTION\n";
                    expected.replace(spans[index].first, spans[index].second - spans[index].first, L"new");
                }
                forward << L"END";
                const std::wstring revision = DecodedResponseField(response, 2);
                std::wstring execution;
                success = InvokeTwoStrings(batch, L"ExecutePreparedTextPatches", forward.str().c_str(),
                    revision.c_str(), &execution) && execution.rfind(L"HCA2\tOK\t", 0) == 0 &&
                    dispatch->FindSelectionSurplusCellText() == expected;
                if (!success) { std::wcout << execution << L'\n'; }
            }
            // Read-only records are rejected by the mutation endpoint.
            std::wstring mutation;
            success = InvokeString(batch, L"ExecuteActions", payload.str().c_str(), &mutation) &&
                mutation.rfind(L"HCA2\tERROR\t", 0) == 0 &&
                dispatch->FindSelectionSurplusCellText() == expected && success;
            std::wcout << L"LITERAL_RANGE_" << label << L" " << success
                       << L" targets=" << spans.size() << L" readbacks=" << reads << L'\n';
            if (!success) { std::wcout << response << L'\n' << mutation << L'\n'; }
            passed = success && passed;
        };
        literal(L"PREFIX", L"old end", {{0,3}}, 1024, 0);
        literal(L"REPEATED", L"10 20 10", {{6,8}}, 2, 0);
        literal(L"UNICODE", L"\U0001F600 old end", {{3,6}}, 9, 0);
        literal(L"END_BEFORE_CONTROL", L"old end", {{0,3}}, 7, 3);
        literal(L"START_AFTER_CONTROL", L"old end", {{4,7}}, 7, 4);
        literal(L"CONTROL_INSIDE_REFUSED", L"old end", {{0,3}}, 7, 1, L"TEXT_RANGE_CONTROL");
        literal(L"STALE_CONTEXT", L"new end", {{0,3}}, 2, 0, L"STALE_SELECTION_TEXT", L"old end");
        std::wstring numbers;
        std::vector<std::pair<LONG, LONG>> spans;
        for (LONG index = 0; index < 64; ++index) {
            numbers += L"10 ";
            spans.insert(spans.begin(), {index * 3, index * 3 + 2});
        }
        literal(L"SHARED_PARAGRAPH", numbers, spans, 1024, 0);
        if (batch != nullptr) { batch->Release(); }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return passed && SUCCEEDED(revokeStatus) ? 0 : 46;
    }
    if (findSelectionSurplusMode) {
        // Find "old" inside a cell reading "the old" and replace it with
        // "new". Whatever ForwardFind reports as its range, the four
        // characters of "the " were not searched for, and the cell has to
        // still read "the new" when the patch is done.
        constexpr wchar_t payload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"PATCH_TEXT\tFIND\tcmVmZXJlbmNlLXRhYmxl\tB2\t1\t1\tb2xk\tbmV3\nEND";
        const bool usable = SUCCEEDED(dispatchStatus) && batch != nullptr;
        // ForwardFind reports exactly what it matched. Nothing to narrow, and
        // the drawn paragraph number in the readback must not get in the way.
        dispatch->PrepareFindSelectionSurplusFixture(0, true);
        std::wstring exactResponse;
        const bool exactPatched = usable &&
            InvokeString(batch, L"ExecuteActions", payload, &exactResponse) &&
            exactResponse.rfind(L"HCA2\tOK\t", 0) == 0 &&
            dispatch->FindSelectionSurplusCellText() == L"the new";
        // ForwardFind reports four characters more than it matched. The patch
        // has to pull its range back onto the literal: replacing the reported
        // range whole would leave the cell reading "new".
        dispatch->PrepareFindSelectionSurplusFixture(4, false);
        std::wstring surplusResponse;
        const bool surplusNarrowed = usable &&
            InvokeString(batch, L"ExecuteActions", payload, &surplusResponse) &&
            surplusResponse.rfind(L"HCA2\tOK\t", 0) == 0 &&
            dispatch->FindSelectionSurplusCellText() == L"the new";
        // The same surplus in a paragraph whose number is drawn into every
        // readback. Narrowing has to see past the number to place the literal.
        dispatch->PrepareFindSelectionSurplusFixture(4, true);
        std::wstring numberedResponse;
        const bool numberedNarrowed = usable &&
            InvokeString(batch, L"ExecuteActions", payload, &numberedResponse) &&
            numberedResponse.rfind(L"HCA2\tOK\t", 0) == 0 &&
            dispatch->FindSelectionSurplusCellText() == L"the new";
        // A surplus the readback cannot be lined up against, because what
        // stands in front is drawn text no automatic number accounts for. The
        // patch must refuse rather than delete characters it cannot place.
        dispatch->PrepareFindSelectionSurplusFixture(4, false, true);
        std::wstring unplaceableResponse;
        const bool unplaceableRefused = usable &&
            InvokeString(batch, L"ExecuteActions", payload, &unplaceableResponse) &&
            unplaceableResponse.rfind(L"HCA2\tERROR\t", 0) == 0 &&
            unplaceableResponse.find(L"STALE_SELECTION_TEXT") !=
                std::wstring::npos &&
            dispatch->FindSelectionSurplusCellText() == L"the old";
        const bool passed = exactPatched && surplusNarrowed &&
            numberedNarrowed && unplaceableRefused;
        std::wcout << L"FIND_SELECTION_EXACT_PATCHED " << exactPatched << L'\n'
                   << L"FIND_SELECTION_SURPLUS_NARROWED " << surplusNarrowed
                   << L'\n'
                   << L"FIND_SELECTION_NUMBERED_SURPLUS_NARROWED "
                   << numberedNarrowed << L'\n'
                   << L"FIND_SELECTION_UNPLACEABLE_REFUSED "
                   << unplaceableRefused << L'\n'
                   << L"FIND_SELECTION_EXACT_RESPONSE " << exactResponse << L'\n'
                   << L"FIND_SELECTION_SURPLUS_RESPONSE " << surplusResponse
                   << L'\n'
                   << L"FIND_SELECTION_NUMBERED_RESPONSE " << numberedResponse
                   << L'\n'
                   << L"FIND_SELECTION_UNPLACEABLE_RESPONSE "
                   << unplaceableResponse << L'\n';
        if (batch != nullptr) {
            batch->Release();
        }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return passed && SUCCEEDED(revokeStatus) ? 0 : 45;
    }
    if (automaticNumberTextPatchMode) {
        // "old" -> "new" over the whole cell text. No formatting action
        // follows, so the readback policy is Exact and the drawn paragraph
        // number is the only thing that can stand in front of it.
        constexpr wchar_t payload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"PATCH_TEXT\tRANGE\t651\t0\t0\t651\t0\t3\tb2xk\tbmV3\nEND";
        const bool usable = SUCCEEDED(dispatchStatus) && batch != nullptr;
        // SelectText accepts the range; only the prefixed readback is in
        // the way.
        dispatch->PrepareAutomaticNumberTextPatchFixture(false);
        std::wstring readbackResponse;
        const bool readbackPatched = usable &&
            InvokeString(batch, L"ExecuteActions", payload, &readbackResponse) &&
            readbackResponse.rfind(L"HCA2\tOK\t", 0) == 0 &&
            dispatch->AutomaticNumberTextPatchApplied();
        // The same patch replayed against the text it already wrote must
        // still be refused: the allowance covers the drawn number, not the
        // content.
        std::wstring staleResponse;
        const bool staleRejected = readbackPatched &&
            InvokeString(batch, L"ExecuteActions", payload, &staleResponse) &&
            staleResponse.rfind(L"HCA2\tERROR\t", 0) == 0 &&
            staleResponse.find(L"STALE_SELECTION_TEXT") != std::wstring::npos;
        // SelectText refuses the range outright; the caret path has to
        // reach it.
        dispatch->PrepareAutomaticNumberTextPatchFixture(true);
        std::wstring fallbackResponse;
        const bool fallbackPatched = usable &&
            InvokeString(batch, L"ExecuteActions", payload, &fallbackResponse) &&
            fallbackResponse.rfind(L"HCA2\tOK\t", 0) == 0 &&
            dispatch->AutomaticNumberTextPatchApplied();
        const bool fallbackUsed = fallbackPatched &&
            dispatch->AutomaticNumberSelectTextFallbackUsed();
        std::wcout << L"AUTOMATIC_NUMBER_READBACK_PATCHED "
                   << readbackPatched << L'\n'
                   << L"AUTOMATIC_NUMBER_STALE_REJECTED "
                   << staleRejected << L'\n'
                   << L"AUTOMATIC_NUMBER_SELECT_FALLBACK_PATCHED "
                   << fallbackPatched << L'\n'
                   << L"AUTOMATIC_NUMBER_SELECT_FALLBACK_USED "
                   << fallbackUsed << L'\n'
                   << L"AUTOMATIC_NUMBER_READBACK_RESPONSE "
                   << readbackResponse << L'\n'
                   << L"AUTOMATIC_NUMBER_STALE_RESPONSE "
                   << staleResponse << L'\n'
                   << L"AUTOMATIC_NUMBER_SELECT_FALLBACK_RESPONSE "
                   << fallbackResponse << L'\n';
        if (batch != nullptr) {
            batch->Release();
        }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return readbackPatched && staleRejected && fallbackPatched &&
            fallbackUsed && SUCCEEDED(revokeStatus) ? 0 : 44;
    }
    if (preparedFormatRequestedReadbackMode) {
        constexpr wchar_t payload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB2\t1\t1\t"
            L"b2xk\tb2xk\n"
            L"ACTION\tCharShape\tHCharShape\n"
            L"SET\tTextColor\tI4\t255\nENDACTION\nEND";
        constexpr wchar_t automaticNumberPayload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"PATCH_TEXT\tRANGE\t651\t0\t0\t651\t0\t3\tb2xk\tb2xk\n"
            L"ACTION\tCharShape\tHCharShape\n"
            L"SET\tTextColor\tI4\t255\nENDACTION\nEND";
        dispatch->PrepareRequestedFormatPreflightFixture(false);
        std::wstring successResponse;
        const bool success = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(batch, L"PrepareTextPatches", payload, &successResponse) &&
            successResponse.rfind(L"HTP2\tOK\t", 0) == 0;
        const std::wstring revision = DecodedResponseField(successResponse, 2);
        std::wstring executionResponse;
        const bool executed = success && !revision.empty() &&
            InvokeTwoStrings(
                batch,
                L"ExecutePreparedTextPatches",
                payload,
                revision.c_str(),
                &executionResponse) &&
            executionResponse.rfind(L"HCA2\tOK\t", 0) == 0;
        std::wstring inspectionResponse;
        const bool inspectionUsable = executed &&
            InvokeLongString(batch, L"InspectPageV3", 1, &inspectionResponse) &&
            inspectionResponse.rfind(L"HCI1", 0) == 0;
        dispatch->PrepareRequestedFormatPreflightFixture(false, true);
        std::wstring automaticNumberResponse;
        const bool automaticNumber = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(
                batch,
                L"PrepareTextPatches",
                automaticNumberPayload,
                &automaticNumberResponse) &&
            automaticNumberResponse.rfind(L"HTP2\tOK\t", 0) == 0;
        const std::wstring automaticRevision =
            DecodedResponseField(automaticNumberResponse, 2);
        std::wstring automaticExecutionResponse;
        const bool automaticNumberExecuted = automaticNumber &&
            !automaticRevision.empty() &&
            InvokeTwoStrings(
                batch,
                L"ExecutePreparedTextPatches",
                automaticNumberPayload,
                automaticRevision.c_str(),
                &automaticExecutionResponse) &&
            automaticExecutionResponse.rfind(L"HCA2\tOK\t2\t", 0) == 0;
        dispatch->PrepareRequestedFormatPreflightFixture(false, true, true);
        std::wstring staleContentResponse;
        const bool staleContent = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(
                batch,
                L"PrepareTextPatches",
                automaticNumberPayload,
                &staleContentResponse) &&
            staleContentResponse.rfind(
                L"HTP2\tERROR\t0\tSTALE_SELECTION_TEXT\t", 0) == 0;
        dispatch->PrepareRequestedFormatPreflightFixture(true);
        std::wstring mismatchResponse;
        const bool mismatch = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(batch, L"PrepareTextPatches", payload, &mismatchResponse) &&
            mismatchResponse.rfind(L"HTP2\tERROR\t0\tTEXT_FORMAT_READBACK\t", 0) == 0 &&
            DecodedResponseField(mismatchResponse, 4) ==
                L"text.patch.preflight.inverse";
        std::wcout << L"PREPARED_FORMAT_REQUESTED_PROPERTY_SUCCESS "
                   << success << L'\n'
                   << L"PREPARED_FORMAT_REQUESTED_PROPERTY_MISMATCH "
                   << mismatch << L'\n'
                   << L"PREPARED_FORMAT_AUTOMATIC_NUMBER_SUCCESS "
                   << automaticNumber << L'\n'
                   << L"PREPARED_FORMAT_AUTOMATIC_NUMBER_EXECUTED "
                   << automaticNumberExecuted << L'\n'
                   << L"PREPARED_FORMAT_AUTOMATIC_NUMBER_STALE_REJECTED "
                   << staleContent << L'\n'
                   << L"PREPARED_FORMAT_POST_INSPECTION_USABLE "
                   << inspectionUsable << L'\n'
                   << L"PREPARED_FORMAT_REQUESTED_PROPERTY_SUCCESS_RESPONSE "
                   << successResponse << L'\n'
                   << L"PREPARED_FORMAT_REQUESTED_PROPERTY_MISMATCH_RESPONSE "
                   << mismatchResponse << L'\n'
                   << L"PREPARED_FORMAT_AUTOMATIC_NUMBER_RESPONSE "
                   << automaticNumberResponse << L'\n'
                   << L"PREPARED_FORMAT_AUTOMATIC_NUMBER_EXECUTION_RESPONSE "
                   << automaticExecutionResponse << L'\n'
                   << L"PREPARED_FORMAT_AUTOMATIC_NUMBER_STALE_RESPONSE "
                   << staleContentResponse << L'\n'
                   << L"PREPARED_FORMAT_EXECUTION_RESPONSE "
                   << executionResponse << L'\n'
                   << L"PREPARED_FORMAT_INSPECTION_PREFIX "
                   << inspectionResponse.substr(0, 4) << L'\n';
        if (batch != nullptr) {
            batch->Release();
        }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return success && mismatch && automaticNumber &&
            automaticNumberExecuted && staleContent && inspectionUsable &&
            SUCCEEDED(revokeStatus) ? 0 : 43;
    }
    if (textFormatReadbackMode) {
        constexpr wchar_t successPayload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"ACTION\tCharShape\tHCharShape\n"
            L"SET\tTextColor\tI4\t255\nENDACTION\n"
            L"ACTION\tParagraphShape\tHParaShape\n"
            L"SET\tAlignType\tI4\t3\nENDACTION\nEND";
        constexpr wchar_t mismatchPayload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"ACTION\tCharShape\tHCharShape\n"
            L"SET\tTextColor\tI4\t65280\nENDACTION\nEND";
        dispatch->PrepareTextFormatReadbackFixture(false);
        std::wstring successResponse;
        const bool success = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(
                batch,
                L"ExecuteActions",
                successPayload,
                &successResponse) &&
            successResponse.rfind(L"HCA2\tOK\t2\t2\t", 0) == 0 &&
            dispatch->TextFormatReadbackSucceeded();
        dispatch->PrepareTextFormatReadbackFixture(true);
        std::wstring mismatchResponse;
        const bool mismatch = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(
                batch,
                L"ExecuteActions",
                mismatchPayload,
                &mismatchResponse) &&
            mismatchResponse.rfind(L"HCA2\tERROR\tPOSTCONDITION\t", 0) == 0 &&
            DecodedResponseField(mismatchResponse, 3) == L"TextColor" &&
            DecodedResponseField(mismatchResponse, 6) == L"CharShape" &&
            SplitTabs(mismatchResponse).size() >= 9 &&
            SplitTabs(mismatchResponse)[7] == L"1" &&
            dispatch->TextFormatMismatchWasReadBack();
        std::wcout << L"TEXT_FORMAT_READBACK_SUCCESS " << success << L'\n'
                   << L"TEXT_FORMAT_READBACK_MISMATCH " << mismatch << L'\n'
                   << L"TEXT_FORMAT_READBACK_SUCCESS_RESPONSE "
                   << successResponse << L'\n'
                   << L"TEXT_FORMAT_READBACK_MISMATCH_RESPONSE "
                   << mismatchResponse << L'\n';
        if (batch != nullptr) {
            batch->Release();
        }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return success && mismatch && SUCCEEDED(revokeStatus) ? 0 : 39;
    }
    if (formatRangeReadbackMode) {
        constexpr wchar_t successPayload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\nCELL\tA1\n"
            L"RUN\tTableCellBlock\nRUN\tTableCellBlockExtend\n"
            L"RUN\tTableRightCell\nRUN\tTableLowerCell\n"
            L"ACTION\tCellFill\tHCellBorderFill\n"
            L"SET\tFillColor\tI4\t255\nENDACTION\n"
            L"ACTION\tCellBorder\tHCellBorderFill\nENDACTION\n"
            L"ACTION\tTablePropertyDialog\tHShapeObject\nENDACTION\n"
            L"RUN\tTableVAlignCenter\nEND";
        constexpr wchar_t mismatchPayload[] =
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\nCELL\tA1\n"
            L"RUN\tTableCellBlock\nRUN\tTableCellBlockExtend\n"
            L"RUN\tTableRightCell\nRUN\tTableLowerCell\n"
            L"ACTION\tCellFill\tHCellBorderFill\n"
            L"SET\tFillColor\tI4\t65280\nENDACTION\nEND";
        dispatch->PrepareRangeFormatReadbackFixture(false);
        std::wstring successResponse;
        const bool success = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(
                batch,
                L"ExecuteActions",
                successPayload,
                &successResponse) &&
            successResponse.rfind(L"HCA2\tOK\t11\t", 0) == 0 &&
            dispatch->RangeFormatSelectionWasPreserved();
        dispatch->PrepareRangeFormatReadbackFixture(true);
        std::wstring mismatchResponse;
        const bool mismatch = SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(
                batch,
                L"ExecuteActions",
                mismatchPayload,
                &mismatchResponse) &&
            mismatchResponse.rfind(L"HCA2\tERROR\tPOSTCONDITION\t", 0) == 0 &&
            DecodedResponseField(mismatchResponse, 3) == L"FillColor" &&
            DecodedResponseField(mismatchResponse, 6) == L"CellFill" &&
            SplitTabs(mismatchResponse).size() >= 9 &&
            SplitTabs(mismatchResponse)[7] == L"1" &&
            dispatch->RangeFormatMismatchWasReadBack();
        std::wcout << L"FORMAT_RANGE_PRESERVED " << success << L'\n'
                   << L"FORMAT_RANGE_MISMATCH " << mismatch << L'\n'
                   << L"FORMAT_RANGE_SUCCESS_RESPONSE "
                   << successResponse << L'\n'
                   << L"FORMAT_RANGE_MISMATCH_RESPONSE "
                   << mismatchResponse << L'\n';
        if (batch != nullptr) {
            batch->Release();
        }
        const HRESULT revokeStatus = releasePublication();
        FreeLibrary(library);
        CoUninitialize();
        return success && mismatch && SUCCEEDED(revokeStatus) ? 0 : 40;
    }
    std::wstring ping;
    std::wstring badRequest;
    std::wstring unsavedBatchRequest;
    std::wstring snapshot;
    std::wstring page;
    std::wstring pageV3;
    std::wstring pageBatch;
    std::wstring pageSummary;
    std::wstring routingContext;
    std::wstring structure;
    std::wstring scopedStructure;
    std::wstring unscopedStructure;
    std::wstring badActionRequest;
    std::wstring unsavedActionRequest;
    std::wstring badArrayIndexRequest;
    std::wstring validArrayRequest;
    std::wstring sizedPictureRequest;
    std::wstring croppedPictureRequest;
    std::wstring badCropPictureRequest;
    std::wstring formattedCaptionRequest;
    std::wstring capturedTableRequest;
    std::wstring captionTableRequest;
    std::wstring deleteControlRequest;
    std::wstring invalidPasteOrderRequest;
    std::wstring callReturnRequest;
    std::wstring partialMutationRequest;
    std::wstring atomicRollbackRequest;
    std::wstring atomicRollbackRetryRequest;
    std::wstring atomicRollbackFailureRequest;
    std::wstring checkpointRestoreRequest;
    std::wstring checkpointRollbackRequest;
    std::wstring emptyCellTextRequest;
    std::wstring referenceLayoutSuccessRequest;
    std::wstring referenceLayoutAtomicRollbackRequest;
    std::wstring referenceLayoutDroppedEdgesRequest;
    std::wstring selectedControlPatchRequest;
    std::wstring textDeletionPatchRequest;
    std::wstring staleSelectionRequest;
    std::wstring replaceSelectionRequest;
    std::wstring badOfficialApiRequest;
    std::wstring actionOfficialApiRequest;
    std::wstring validationActionOfficialApiRequest;
    std::wstring actionInputOfficialApiRequest;
    std::wstring fallbackActionOfficialApiRequest;
    std::wstring sehActionOfficialApiRequest;
    std::wstring parameterOfficialApiRequest;
    std::wstring fallbackParameterOfficialApiRequest;
    std::wstring normalizedParameterOfficialApiRequest;
    std::wstring reportedMissingParameterOfficialApiRequest;
    std::wstring automationOfficialApiRequest;
    std::wstring eventOfficialApiRequest;
    std::wstring aliasOfficialApiRequest;
    std::wstring itemAliasOfficialApiRequest;
    std::wstring messageBoxOwnerOfficialApiRequest;
    std::wstring writeOnlyPropertyOfficialApiRequest;
    std::wstring hActionGetDefaultOfficialApiRequest;
    std::wstring hActionExecuteOfficialApiRequest;
    std::wstring setIdAliasOfficialApiRequest;
    const bool abandonedCheckpointSmokeFilesRemoved =
        RemoveAbandonedCheckpointSmokeFiles();
    const std::filesystem::path checkpointPath =
        std::filesystem::temp_directory_path() /
        (L"HancomLiveBridgeCheckpointSmoke-" +
         std::to_wstring(GetCurrentProcessId()) + L".bin");
    const bool checkpointFixtureWritten =
        WriteCheckpointFixture(
            checkpointPath,
            "CHECKPOINT_TARGET",
            kCheckpointDocumentSignature);
    const std::wstring encodedCheckpointPath =
        EncodeUtf8Base64(checkpointPath.wstring());
    const std::wstring checkpointRestorePayload =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\nRESTORE_DOCUMENT_FILE\t" +
        encodedCheckpointPath + L"\t4\nEND";
    const std::filesystem::path legacyCheckpointPath =
        std::filesystem::temp_directory_path() /
        (L"HancomLiveBridgeLegacyCheckpointSmoke-" +
         std::to_wstring(GetCurrentProcessId()) + L".bin");
    const bool legacyCheckpointFixtureWritten =
        WriteCheckpointFixture(
            legacyCheckpointPath,
            "CHECKPOINT_TARGET",
            "");
    const std::wstring legacyCheckpointRestorePayload =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\nRESTORE_DOCUMENT_FILE\t" +
        EncodeUtf8Base64(legacyCheckpointPath.wstring()) + L"\t4\nEND";
    // A checkpoint in the other layout: no magic of ours in it, so the bridge
    // reads it back as a document file and restores it with InsertFile. The
    // bytes are never parsed -- the fake engine is what answers for the insert --
    // but they must not start with an encoded-block magic or this would be read
    // as the layout it is here to be distinguished from.
    const std::filesystem::path checkpointDocumentPath =
        std::filesystem::temp_directory_path() /
        (L"HancomLiveBridgeCheckpointDocumentSmoke-" +
         std::to_wstring(GetCurrentProcessId()) + L".hwp");
    // The real route restores over an existing saved user file. Keep that
    // precondition true in the fake too; the same-slot reopen does not invent
    // a missing destination path.
    const std::filesystem::path userDocumentPath =
        std::filesystem::temp_directory_path() /
        (L"HancomLiveBridgeUserDocumentSmoke-" +
         std::to_wstring(GetCurrentProcessId()) + L".hwp");
    const bool userDocumentFixtureWritten =
        WriteDocumentCheckpointFixture(userDocumentPath);
    const bool checkpointDocumentFixtureWritten =
        userDocumentFixtureWritten &&
        WriteDocumentCheckpointFixture(checkpointDocumentPath) &&
        WriteDocumentCheckpointMeta(
            checkpointDocumentPath,
            kCheckpointDocumentSignature);
    const std::wstring encodedCheckpointDocumentPath =
        EncodeUtf8Base64(checkpointDocumentPath.wstring());
    const std::wstring checkpointDocumentRestorePayload =
        L"HCA1\nDOC\t17\t" + EncodeUtf8Base64(userDocumentPath.wstring()) +
        L"\nRESTORE_DOCUMENT_FILE\t" + encodedCheckpointDocumentPath +
        L"\t4\nEND";
    // The read-only block probe. It exists to answer a question nothing has
    // ever measured -- how large a document the encoded-block layout can carry
    // -- so what it must never do is change the document or carry the block
    // itself back in the response.
    const std::filesystem::path blockProbePath =
        std::filesystem::temp_directory_path() /
        (L"HancomLiveBridgeBlockProbeSmoke-" +
         std::to_wstring(GetCurrentProcessId()) + L".b64");
    const std::wstring blockProbePayload =
        L"HCA1\nDOC\t17\t" + EncodeUtf8Base64(userDocumentPath.wstring()) +
        L"\nCAPTURE_DOCUMENT_BLOCK_PROBE\t" +
        EncodeUtf8Base64(blockProbePath.wstring()) + L"\nEND";
    constexpr wchar_t badArrayIndexPayload[] =
        L"HCA1\nDOC\t0\tQzpceC5od3A=\n"
        L"ACTION\tTableCreate\tHTableCreation\n"
        L"ARRAY\tColWidth\t1\n"
        L"ASET\tColWidth\t1\tMM\t16\n"
        L"ENDACTION\nEND";
    constexpr wchar_t validArrayPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"ACTION\tArrayAction\tHArrayFixture\n"
        L"ARRAY\tColWidth\t1\n"
        L"ASET\tColWidth\t0\tI4\t3200\n"
        L"ENDACTION\nEND";
    constexpr wchar_t unsavedBatchPayload[] =
        L"HCB1\nDOC\t17\t \n"
        L"TABLE\t1136771603\n"
        L"TEXT\tA1\tIA==\tdmVyaWZ5\n"
        L"ENDTABLE\nEND";
    constexpr wchar_t unsavedActionPayload[] =
        L"HCA1\nDOC\t17\t \nRUN\tBreakPage\nEND";
    constexpr wchar_t sizedPicturePayload[] =
        L"HCA1\nDOC\t0\tQzpceC5od3A=\n"
        L"MOVE_PAGE\t9\n"
        L"MOVE_POSITION\t0\t21\t0\n"
        L"SELECT_CONTROL\tMTE0MDg3ODQzNg==\n"
        L"INSERT_PICTURE\tQzpceC5wbmc=\t80\t40\nEND";
    constexpr wchar_t croppedPicturePayload[] =
        L"HCA1\nDOC\t0\tQzpceC5od3A=\n"
        L"INSERT_PICTURE\tQzpceC5wbmc=\t80\t40\t0.1\t0\t0.1\t0\nEND";
    // Opposite crop edges that add up to the whole image would leave nothing
    // to show, so the parser has to refuse them before Hancom is touched.
    constexpr wchar_t badCropPicturePayload[] =
        L"HCA1\nDOC\t0\tQzpceC5od3A=\n"
        L"INSERT_PICTURE\tQzpceC5wbmc=\t80\t40\t0.6\t0\t0.4\t0\nEND";
    constexpr wchar_t formattedCaptionPayload[] =
        L"HCA1\nDOC\t0\tQzpceC5od3A=\n"
        L"CAPTION\t9\tdGl0bGU=\tQXJpYWw=\t1200\t0\t0\t3\t160\t0\t0\t0\t1000\t0\t0\t104\t0\nEND";
    constexpr wchar_t capturedTablePayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"COPY_CONTROL\tY2FwdHVyZWQtdGFibGU=\n"
        L"APPLY_COPIED_TABLE_ANCHOR\tY2FwdHVyZWQtdGFibGU=\n"
        L"PASTE_TABLE\n"
        L"CAPTURE_TABLE\nEND";
    constexpr wchar_t captionTablePayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"COPY_CONTROL\tY2FwdHVyZWQtdGFibGU=\n"
        L"APPLY_COPIED_TABLE_ANCHOR\tY2FwdHVyZWQtdGFibGU=\n"
        L"PASTE_TABLE\n"
        L"CAPTURE_TABLE\n"
        L"CAPTION\t9\tdGl0bGU=\nEND";
    constexpr wchar_t deleteControlPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"DELETE_CONTROL\tY2FwdHVyZWQtdGFibGU=\nEND";
    constexpr wchar_t invalidPasteOrderPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"RUN\tBreakPage\n"
        L"PASTE_TABLE\nEND";
    constexpr wchar_t callReturnPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"CALL\tReturnBoolean\nENDCALL\n"
        L"CALL\tReturnInteger\nENDCALL\n"
        L"CALL\tReturnText\nENDCALL\n"
        L"CALL\tReturnVoid\nENDCALL\nEND";
    constexpr wchar_t partialMutationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"RUN\tBreakPage\n"
        L"RUN\tSelectCtrlFront\nEND";
    constexpr wchar_t atomicRollbackPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"POLICY\tATOMIC\t1\n"
        L"MOVE_DOC_END\n"
        L"INSERT_TEXT\tYXRvbWljLXRhaWw=\n"
        L"RUN\tBreakPage\n"
        L"RUN\tSelectCtrlFront\nEND";
    constexpr wchar_t emptyCellTextPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"CAPTURE_TABLE\n"
        L"SET_CELL_TEXT\tA2\t \tZmlsbGVk\t1\nEND";
    struct MultilineCellTextCase {
        const wchar_t* label;
        const wchar_t* payload;
        bool patchText;
        const wchar_t* address;
        const wchar_t* expected;
    };
    constexpr MultilineCellTextCase multilineCellTextCases[] = {
        {
            L"SET_CELL_TEXT/LF",
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\n"
            L"SET_CELL_TEXT\tA2\t \tbGluZTEKbGluZTI=\t1\nEND",
            false,
            L"A2",
            L"line1\nline2",
        },
        {
            L"SET_CELL_TEXT/CRLF",
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\n"
            L"SET_CELL_TEXT\tA2\t \tbGluZTENCmxpbmUy\t1\nEND",
            false,
            L"A2",
            L"line1\nline2",
        },
        {
            L"SET_CELL_TEXT/CR",
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\n"
            L"SET_CELL_TEXT\tA2\t \tbGluZTENbGluZTI=\t1\nEND",
            false,
            L"A2",
            L"line1\nline2",
        },
        {
            L"PATCH_TEXT CELL/LF",
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\n"
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB2\t1\t1\t"
            L"b2xk\tbGluZTEKbGluZTI=\t1\nEND",
            true,
            L"B2",
            L"line1\nline2",
        },
        {
            L"PATCH_TEXT CELL/CRLF",
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\n"
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB2\t1\t1\t"
            L"b2xk\tbGluZTENCmxpbmUy\t1\nEND",
            true,
            L"B2",
            L"line1\nline2",
        },
        {
            L"PATCH_TEXT CELL/CR",
            L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
            L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
            L"CAPTURE_TABLE\n"
            L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB2\t1\t1\t"
            L"b2xk\tbGluZTENbGluZTI=\t1\nEND",
            true,
            L"B2",
            L"line1\nline2",
        },
    };
    constexpr wchar_t allTargetCellTextPreflightPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"SET_CELL_TEXT\tA2\t \tZmlyc3Q=\t1\n"
        L"SET_CELL_TEXT\tB2\tc3RhbGU=\tZGFuZ2Vy\t1\nEND";
    constexpr wchar_t expandCellTextPreflightPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA2\n"
        L"RUN\tTableAppendRow\n"
        L"SET_CELL_TEXT\tA3\tbmV3\n"
        L"SET_CELL_TEXT\tB2\tc3RhbGU=\tZGFuZ2Vy\t1\nEND";
    constexpr wchar_t selectionCompatibleTableTextPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"SET_CELL_TEXT\tA2\tb2xk\tbmV3\t1\nEND";
    constexpr wchar_t topologyReusePayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tA2\t1\t1\t"
        L"b2xk\tbmV3LWE=\t1\n"
        L"PATCH_TEXT\tCELL\tcmVmZXJlbmNlLXRhYmxl\tB2\t1\t1\t"
        L"b2xk\tbmV3LWI=\t1\nEND";
    constexpr wchar_t topologyFormattingPreservationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"ACTION\tStyle\tHStyle\n"
        L"ENDACTION\n"
        L"ACTION\tCharShape\tHCharShape\n"
        L"ENDACTION\n"
        L"ACTION\tParagraphShape\tHParaShape\n"
        L"ENDACTION\n"
        L"ACTION\tCellFill\tHCellBorderFill\n"
        L"ENDACTION\n"
        L"ACTION\tCellBorder\tHCellBorderFill\n"
        L"ENDACTION\n"
        L"RUN\tTableVAlignTop\n"
        L"RUN\tTableVAlignCenter\n"
        L"RUN\tTableVAlignBottom\n"
        L"CELL\tB1\nEND";
    constexpr wchar_t topologyPaddingPreservationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"ACTION\tTablePropertyDialog\tHShapeObject\n"
        L"SET\tHSet/ShapeType\tI4\t3\n"
        L"SET\tHSet/ShapeCellSize\tI4\t0\n"
        L"SET\tShapeTableCell/HasMargin\tI4\t1\n"
        L"SET\tShapeTableCell/MarginLeft\tI4\t100\n"
        L"SET\tShapeTableCell/MarginRight\tI4\t200\n"
        L"SET\tShapeTableCell/MarginTop\tI4\t300\n"
        L"SET\tShapeTableCell/MarginBottom\tI4\t400\n"
        L"ENDACTION\n"
        L"CELL\tB1\nEND";
    constexpr wchar_t topologyCellSizeInvalidationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"ACTION\tTablePropertyDialog\tHShapeObject\n"
        L"SET\tHSet/ShapeType\tI4\t3\n"
        L"SET\tHSet/ShapeCellSize\tI4\t1\n"
        L"SET\tShapeTableCell/Width\tI4\t24000\n"
        L"SET\tShapeTableCell/Height\tI4\t12000\n"
        L"ENDACTION\n"
        L"CELL\tB1\nEND";
    constexpr wchar_t topologyUnknownActionInvalidationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"ACTION\tFutureCellFormat\tHCellBorderFill\n"
        L"ENDACTION\n"
        L"CELL\tB1\nEND";
    constexpr wchar_t topologyInvalidationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"MERGE\tA1\tB1\n"
        L"CELL\tB1\nEND";
    constexpr wchar_t topologyRunInvalidationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"RUN\tTableDeleteRow\n"
        L"CELL\tB1\nEND";
    constexpr wchar_t topologyActionInvalidationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"ACTION\tTableInsertRightColumn\tHTableInsertLine\n"
        L"ENDACTION\n"
        L"CELL\tB1\nEND";
    constexpr wchar_t topologyCallInvalidationPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"SELECT_CONTROL\tcmVmZXJlbmNlLXRhYmxl\n"
        L"CAPTURE_TABLE\n"
        L"CELL\tA1\n"
        L"CALL\tMutateTableTopology\n"
        L"ENDCALL\n"
        L"CELL\tB1\nEND";
    const std::wstring oversizedCellPatchPayload =
        OversizedCellPatchPayload();
    constexpr wchar_t referenceLayoutPayload[] = LR"REF(HCA1
DOC	17	QzpceC5od3A=
MOVE_DOC_END
ACTION	ReferenceLayoutBulk	HTableCreation
SET	Rows	I4	3
SET	Columns	I4	2
SET	BaseStyleId	I4	0
SET	BodyLeft	I4	5804
SET	BodyTop	I4	4387
SET	BodyWidth	I4	47920
SET	BodyHeight	I4	76691
ARRAY	ColumnWidths	2
ARRAY	RowHeights	3
ARRAY	MergeRows	1
ARRAY	MergeColumns	1
ARRAY	MergeRowSpans	1
ARRAY	MergeColumnSpans	1
ARRAY	EdgeOrientations	7
ARRAY	EdgeLines	7
ARRAY	EdgeStarts	7
ARRAY	EdgeEnds	7
ARRAY	EdgeColors	7
ARRAY	StyleFontSizes	2
ARRAY	StyleBold	2
ARRAY	StyleTextColors	2
ARRAY	StyleFillColors	2
ARRAY	StyleAlignments	2
ARRAY	StyleVerticalAlignments	2
ARRAY	StyleWidthRatios	2
ARRAY	StyleLetterSpacings	2
ARRAY	StyleLineSpacingTypes	2
ARRAY	StyleLineSpacings	2
ARRAY	StylePreviousSpacings	2
ARRAY	StyleNextSpacings	2
ARRAY	StylePaddingLeft	2
ARRAY	StylePaddingRight	2
ARRAY	StylePaddingTop	2
ARRAY	StylePaddingBottom	2
ARRAY	RegionTop	2
ARRAY	RegionLeft	2
ARRAY	RegionBottom	2
ARRAY	RegionRight	2
ARRAY	RegionStyleIndexes	2
ARRAY	TextRows	5
ARRAY	TextColumns	5
ARRAY	TextStyleIndexes	5
ARRAY	TextBreakModes	5
ARRAY	EdgeStyles	7
ARRAY	EdgeWidths	7
ARRAY	StyleKeys	2
ARRAY	StyleFontNames	2
ARRAY	TextValues	5
ASET	ColumnWidths	0	I4	19168
ASET	ColumnWidths	1	I4	28752
ASET	RowHeights	0	I4	19173
ASET	RowHeights	1	I4	26842
ASET	RowHeights	2	I4	30676
ASET	MergeRows	0	I4	0
ASET	MergeColumns	0	I4	0
ASET	MergeRowSpans	0	I4	1
ASET	MergeColumnSpans	0	I4	2
ASET	EdgeOrientations	0	I4	0
ASET	EdgeOrientations	1	I4	0
ASET	EdgeOrientations	2	I4	0
ASET	EdgeOrientations	3	I4	0
ASET	EdgeOrientations	4	I4	1
ASET	EdgeOrientations	5	I4	1
ASET	EdgeOrientations	6	I4	1
ASET	EdgeLines	0	I4	0
ASET	EdgeLines	1	I4	1
ASET	EdgeLines	2	I4	2
ASET	EdgeLines	3	I4	3
ASET	EdgeLines	4	I4	0
ASET	EdgeLines	5	I4	1
ASET	EdgeLines	6	I4	2
ASET	EdgeStarts	0	I4	0
ASET	EdgeStarts	1	I4	0
ASET	EdgeStarts	2	I4	0
ASET	EdgeStarts	3	I4	0
ASET	EdgeStarts	4	I4	0
ASET	EdgeStarts	5	I4	1
ASET	EdgeStarts	6	I4	0
ASET	EdgeEnds	0	I4	2
ASET	EdgeEnds	1	I4	2
ASET	EdgeEnds	2	I4	2
ASET	EdgeEnds	3	I4	2
ASET	EdgeEnds	4	I4	3
ASET	EdgeEnds	5	I4	3
ASET	EdgeEnds	6	I4	3
ASET	EdgeColors	0	I4	3943715
ASET	EdgeColors	1	I4	3943715
ASET	EdgeColors	2	I4	3943715
ASET	EdgeColors	3	I4	3943715
ASET	EdgeColors	4	I4	3943715
ASET	EdgeColors	5	I4	3943715
ASET	EdgeColors	6	I4	3943715
ASET	StyleFontSizes	0	I4	900
ASET	StyleFontSizes	1	I4	1100
ASET	StyleBold	0	I4	-1
ASET	StyleBold	1	I4	1
ASET	StyleTextColors	0	I4	1315860
ASET	StyleTextColors	1	I4	16777215
ASET	StyleFillColors	0	I4	-1
ASET	StyleFillColors	1	I4	5125162
ASET	StyleAlignments	0	I4	0
ASET	StyleAlignments	1	I4	1
ASET	StyleVerticalAlignments	0	I4	1
ASET	StyleVerticalAlignments	1	I4	1
ASET	StyleWidthRatios	0	I4	100
ASET	StyleWidthRatios	1	I4	100
ASET	StyleLetterSpacings	0	I4	0
ASET	StyleLetterSpacings	1	I4	0
ASET	StyleLineSpacingTypes	0	I4	0
ASET	StyleLineSpacingTypes	1	I4	0
ASET	StyleLineSpacings	0	I4	100
ASET	StyleLineSpacings	1	I4	100
ASET	StylePreviousSpacings	0	I4	0
ASET	StylePreviousSpacings	1	I4	0
ASET	StyleNextSpacings	0	I4	0
ASET	StyleNextSpacings	1	I4	0
ASET	StylePaddingLeft	0	I4	510
ASET	StylePaddingLeft	1	I4	510
ASET	StylePaddingRight	0	I4	510
ASET	StylePaddingRight	1	I4	510
ASET	StylePaddingTop	0	I4	142
ASET	StylePaddingTop	1	I4	142
ASET	StylePaddingBottom	0	I4	142
ASET	StylePaddingBottom	1	I4	142
ASET	RegionTop	0	I4	0
ASET	RegionTop	1	I4	0
ASET	RegionLeft	0	I4	0
ASET	RegionLeft	1	I4	0
ASET	RegionBottom	0	I4	3
ASET	RegionBottom	1	I4	1
ASET	RegionRight	0	I4	2
ASET	RegionRight	1	I4	2
ASET	RegionStyleIndexes	0	I4	0
ASET	RegionStyleIndexes	1	I4	1
ASET	TextRows	0	I4	0
ASET	TextRows	1	I4	1
ASET	TextRows	2	I4	1
ASET	TextRows	3	I4	2
ASET	TextRows	4	I4	2
ASET	TextColumns	0	I4	0
ASET	TextColumns	1	I4	0
ASET	TextColumns	2	I4	1
ASET	TextColumns	3	I4	0
ASET	TextColumns	4	I4	1
ASET	TextStyleIndexes	0	I4	1
ASET	TextStyleIndexes	1	I4	0
ASET	TextStyleIndexes	2	I4	0
ASET	TextStyleIndexes	3	I4	0
ASET	TextStyleIndexes	4	I4	0
ASET	TextBreakModes	0	I4	0
ASET	TextBreakModes	1	I4	0
ASET	TextBreakModes	2	I4	0
ASET	TextBreakModes	3	I4	0
ASET	TextBreakModes	4	I4	0
ASET	EdgeStyles	0	BSTR	c29saWQ=
ASET	EdgeStyles	1	BSTR	c29saWQ=
ASET	EdgeStyles	2	BSTR	c29saWQ=
ASET	EdgeStyles	3	BSTR	c29saWQ=
ASET	EdgeStyles	4	BSTR	c29saWQ=
ASET	EdgeStyles	5	BSTR	c29saWQ=
ASET	EdgeStyles	6	BSTR	c29saWQ=
ASET	EdgeWidths	0	BSTR	MC4zbW0=
ASET	EdgeWidths	1	BSTR	MC4zbW0=
ASET	EdgeWidths	2	BSTR	MC4zbW0=
ASET	EdgeWidths	3	BSTR	MC4zbW0=
ASET	EdgeWidths	4	BSTR	MC4zbW0=
ASET	EdgeWidths	5	BSTR	MC4zbW0=
ASET	EdgeWidths	6	BSTR	MC4zbW0=
ASET	StyleKeys	0	BSTR	Ym9keQ==
ASET	StyleKeys	1	BSTR	aGVhZGVy
ASET	StyleFontNames	0	BSTR	QXJpYWw=
ASET	StyleFontNames	1	BSTR	QXJpYWw=
ASET	TextValues	0	BSTR	TWVyZ2VkIGhlYWRlcg==
ASET	TextValues	1	BSTR	QTI=
ASET	TextValues	2	BSTR	QjI=
ASET	TextValues	3	BSTR	QTM=
ASET	TextValues	4	BSTR	QjM=
ENDACTION
END)REF";
    const wchar_t* const referenceLayoutAtomicRollbackPayload =
        referenceLayoutPayload;
    constexpr wchar_t staleSelectionPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"EXPECT_SELECTION\t1\t0\t108\t0\t0\t108\t0\n"
        L"REPLACE_SELECTION\td3Jvbmc=\tbmV3\nEND";
    constexpr wchar_t replaceSelectionPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"EXPECT_SELECTION\t1\t0\t108\t0\t0\t108\t0\n"
        L"REPLACE_SELECTION\tb2xk\tbmV3\nEND";
    constexpr wchar_t selectedControlPatchPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"PATCH_TEXT\tCURRENT\t0\t \tZGFuZ2Vy\t0\nEND";
    constexpr wchar_t textDeletionPatchPayload[] =
        L"HCA1\nDOC\t17\tQzpceC5od3A=\n"
        L"PATCH_TEXT\tCURRENT\t1\tb2xk\t \nEND";
    constexpr wchar_t expectedCallResults[] =
        L"QglVbVYwZFhKdVFtOXZiR1ZoYmc9PQkxCkkJVW1WMGRYSnVTVzUwWldkbGNnPT0JLTQyClMJVW1WMGRYSnVWR1Y0ZEE9PQlRem92VkdWdGNDOUNTVTR3TURBeExuQnVadz09ClYJVW1WMGRYSnVWbTlwWkE9PQ==";
    constexpr wchar_t actionOfficialApiPayload[] =
        L"HCV1\nACTION\tCharShapeBold\nEND";
    constexpr wchar_t validationActionOfficialApiPayload[] =
        L"HCV1\nACTION\tCharShapeBold\n"
        L"OPTIONS\tDIALOGS_OBSERVE\tEXECUTE_ONLY\nEND";
    constexpr wchar_t actionInputOfficialApiPayload[] =
        L"HCV1\nACTION\tSetWithoutDefault\n"
        L"SET\tFileName\tBSTR\tC:\\probe\\fixture.dat\nEND";
    constexpr wchar_t fallbackActionOfficialApiPayload[] =
        L"HCV1\nACTION\tAutoSpellRun\nEND";
    constexpr wchar_t sehActionOfficialApiPayload[] =
        L"HCV1\nACTION\tRaiseStructured\nEND";
    constexpr wchar_t parameterOfficialApiPayload[] =
        L"HCV1\nPARAMETER_SET\tCharShape\tCharShape\n"
        L"ITEM\tHeight\tPIT_I4\t-\nEND";
    constexpr wchar_t fallbackParameterOfficialApiPayload[] =
        L"HCV1\nPARAMETER_SET\tChangeRome\tChangeRome\n"
        L"ITEM\tOption\tPIT_UI1\t-\nEND";
    constexpr wchar_t normalizedParameterOfficialApiPayload[] =
        L"HCV1\nPARAMETER_SET\tConvertCase\tConvertCase\n"
        L"ITEM\tType\tPMT_UINT\t-\nEND";
    constexpr wchar_t reportedMissingParameterOfficialApiPayload[] =
        L"HCV1\nPARAMETER_SET\tDocumentInfo\tDocumentInfo\n"
        L"ITEM\tReportedMissing\tPIT_I4\t-\nEND";
    constexpr wchar_t automationOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tIHwpObject\tPageCount\tproperty\nEND\n";
    constexpr wchar_t eventOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tIHwpObjectEvents\tDocumentChange\tevent\nEND\n";
    constexpr wchar_t aliasOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tIHwpObject\tRenameMetatag\tmethod\n"
        L"ARG\tBSTR64\tY29kZXgtcnVudGltZS1wcm9iZQ==\nEND\n";
    constexpr wchar_t itemAliasOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tIDHwpParameterSet\tItemExsit\tmethod\n"
        L"ARG\tBSTR64\tY29kZXgtcnVudGltZS1wcm9iZQ==\nEND\n";
    constexpr wchar_t messageBoxOwnerOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tIXHwpMessageBox\tApplication\tproperty\nEND\n";
    constexpr wchar_t writeOnlyPropertyOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tIXHwpDocument\tPassword\tproperty\nEND\n";
    constexpr wchar_t hActionGetDefaultOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tHAction\tGetDefault\tmethod\n"
        L"ARG\tBSTR64\tSW5zZXJ0VGV4dA==\n"
        L"ARG\tACTION_SET\tInsertText\nEND\n";
    constexpr wchar_t hActionExecuteOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tHAction\tExecute\tmethod\n"
        L"ARG\tBSTR64\tSW5zZXJ0VGV4dA==\n"
        L"ARG\tACTION_SET\tInsertText\nEND\n";
    constexpr wchar_t setIdAliasOfficialApiPayload[] =
        L"HCV1\nAUTOMATION\tIDHwpParameterSet\tGetSetID\tproperty\nEND\n";
    dispatch->PrepareSaveVerifySerializationFixture();
    std::wstring saveVerifyResponse;
    const bool saveVerifyInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"SaveVerify", nullptr, &saveVerifyResponse);
    const bool saveVerifyMatched =
        SaveVerifyMatched(saveVerifyResponse) && dispatch->StoppedLifecycleAfterSave();
    // A document the engine will not serialize costs one full attempt before
    // the save and, once that answer is in, nothing is learned by paying for a
    // second one after it -- the verdict is already decided by the first.
    dispatch->PrepareSaveVerifyOversizeRefusalFixture();
    std::wstring saveVerifyOversizeResponse;
    const bool saveVerifyOversizeInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"SaveVerify", nullptr, &saveVerifyOversizeResponse);
    const size_t saveVerifyOversizeSerializationAttempts =
        dispatch->HwpmlSerializationAttempts();
    const bool saveVerifyOversizeSkippedSecondAttempt =
        saveVerifyOversizeInvoked &&
        saveVerifyOversizeSerializationAttempts == 1;
    dispatch->ClearSaveVerifyOversizeRefusalFixture();
    dispatch->PrepareSaveVerifySectionDiagnosticFixture();
    std::wstring saveVerifySectionDiagnosticsResponse;
    bool saveVerifySectionDiagnosticsInvoked = true;
    bool saveVerifySectionDiagnosticsMatched = true;
    std::uint64_t saveVerifySectionDiagnosticsElapsedMicroseconds = 0;
    constexpr size_t kSectionDiagnosticsIterations = 3;
    for (size_t index = 0; index < kSectionDiagnosticsIterations; ++index) {
        saveVerifySectionDiagnosticsInvoked =
            saveVerifySectionDiagnosticsInvoked &&
            SUCCEEDED(dispatchStatus) &&
            batch != nullptr &&
            InvokeString(
                batch,
                L"SaveVerify",
                nullptr,
                &saveVerifySectionDiagnosticsResponse);
        saveVerifySectionDiagnosticsMatched =
            saveVerifySectionDiagnosticsMatched &&
            SaveVerifySectionDiagnosticsMatched(
                saveVerifySectionDiagnosticsResponse);
        const std::vector<std::wstring> fields =
            SplitTabs(saveVerifySectionDiagnosticsResponse);
        if (fields.size() == 25) {
            saveVerifySectionDiagnosticsElapsedMicroseconds +=
                std::wcstoull(fields[20].c_str(), nullptr, 10);
        }
    }
    dispatch->PrepareLifecycleSuccess();
    std::wstring lifecycleSuccessResponse;
    const bool lifecycleSuccessInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"SaveReopenVerify", nullptr, &lifecycleSuccessResponse);
    const bool lifecycleSuccessMatched =
        LifecycleSuccessMatched(lifecycleSuccessResponse) && dispatch->CompletedLifecycleSequence();
    dispatch->PrepareLifecycleCleanNoOpSave();
    std::wstring lifecycleCleanNoOpResponse;
    const bool lifecycleCleanNoOpInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"SaveReopenVerify", nullptr, &lifecycleCleanNoOpResponse);
    const bool lifecycleCleanNoOpMatched =
        LifecycleCleanNoOpMatched(lifecycleCleanNoOpResponse) &&
        dispatch->CompletedLifecycleSequence();
    dispatch->PrepareLifecycleSaveFailure();
    std::wstring lifecycleSaveGateResponse;
    const bool lifecycleSaveGateInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"SaveReopenVerify", nullptr, &lifecycleSaveGateResponse);
    const bool lifecycleSaveGateMatched =
        LifecycleSaveGateMatched(lifecycleSaveGateResponse) && dispatch->StoppedLifecycleAfterSave();
    dispatch->PrepareLifecycleFalseSaveReturn();
    std::wstring lifecycleSaveReturnGateResponse;
    const bool lifecycleSaveReturnGateInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"SaveReopenVerify", nullptr, &lifecycleSaveReturnGateResponse);
    const bool lifecycleSaveReturnGateMatched =
        LifecycleSaveReturnGateMatched(lifecycleSaveReturnGateResponse) &&
        dispatch->StoppedLifecycleAfterSave();
    dispatch->PrepareLifecycleRecoveryCaptureFailure();
    std::wstring lifecycleRecoveryCaptureGateResponse;
    const bool lifecycleRecoveryCaptureGateInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"SaveReopenVerify",
            nullptr,
            &lifecycleRecoveryCaptureGateResponse);
    const bool lifecycleRecoveryCaptureGateMatched =
        LifecycleRecoveryCaptureGateMatched(
            lifecycleRecoveryCaptureGateResponse) &&
        dispatch->StoppedLifecycleBeforeDestructiveReopen();
    dispatch->PrepareLifecycleOpenFailure();
    std::wstring lifecycleRecoveryResponse;
    const bool lifecycleRecoveryInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"SaveReopenVerify", nullptr, &lifecycleRecoveryResponse);
    const bool lifecycleRecoveryMatched =
        LifecycleRecoveryMatched(lifecycleRecoveryResponse) &&
        dispatch->RecoveredLifecycleSession();
    dispatch->PrepareLifecycleHwpxSuccess();
    std::wstring lifecycleHwpxSuccessResponse;
    const bool lifecycleHwpxSuccessInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"SaveReopenVerify",
            nullptr,
            &lifecycleHwpxSuccessResponse);
    const bool lifecycleHwpxSuccessMatched =
        LifecycleSuccessMatchedForPath(
            lifecycleHwpxSuccessResponse,
            L"Qzpc7ZWc6riAXFguSFdQWA==") &&
        dispatch->CompletedLifecycleSequence();
    dispatch->PrepareLifecycleHwpxOpenFailure();
    std::wstring lifecycleHwpxRecoveryResponse;
    const bool lifecycleHwpxRecoveryInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"SaveReopenVerify",
            nullptr,
            &lifecycleHwpxRecoveryResponse);
    const bool lifecycleHwpxRecoveryMatched =
        LifecycleRecoveryMatchedForPath(
            lifecycleHwpxRecoveryResponse,
            L"Qzpc7ZWc6riAXHguSHdQeA==",
            L"0") &&
        dispatch->RecoveredLifecycleSession() &&
        dispatch->UsedExpectedLifecycleRecoveryFormat();
    dispatch->PrepareLifecycleOpenSuccessFingerprintMismatch();
    std::wstring lifecycleFingerprintRecoveryResponse;
    const bool lifecycleFingerprintRecoveryInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"SaveReopenVerify",
            nullptr,
            &lifecycleFingerprintRecoveryResponse);
    const bool lifecycleFingerprintRecoveryMatched =
        LifecycleRecoveryMatchedForPath(
            lifecycleFingerprintRecoveryResponse,
            L"Qzpc7ZWc6riAXHguaHdw",
            L"1") &&
        dispatch->RecoveredLifecycleFingerprintMismatch() &&
        dispatch->UsedExpectedLifecycleRecoveryFormat();
    dispatch->PrepareLifecycleHwpxOpenSuccessFingerprintMismatch();
    std::wstring lifecycleHwpxFingerprintRecoveryResponse;
    const bool lifecycleHwpxFingerprintRecoveryInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"SaveReopenVerify",
            nullptr,
            &lifecycleHwpxFingerprintRecoveryResponse);
    const bool lifecycleHwpxFingerprintRecoveryMatched =
        LifecycleRecoveryMatchedForPath(
            lifecycleHwpxFingerprintRecoveryResponse,
            L"Qzpc7ZWc6riAXHguSHdQeA==",
            L"1") &&
        dispatch->RecoveredLifecycleFingerprintMismatch() &&
        dispatch->UsedExpectedLifecycleRecoveryFormat();
    dispatch->RestoreLifecycleFixture();
    dispatch->PrepareTransientMessageBoxModeRestoreFailure();
    std::wstring messageBoxModeRestoreRetryResponse;
    const bool messageBoxModeRestoreRetryInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            validArrayPayload,
            &messageBoxModeRestoreRetryResponse);
    const bool messageBoxModeRestoreRetryPassed =
        messageBoxModeRestoreRetryResponse.rfind(
            L"HCA2\tERROR\tMESSAGE_BOX_MODE\t",
            0) == 0 &&
        dispatch->RetriedMessageBoxModeRestore();
    dispatch->PrepareSelectCtrlFrontFailure();
    const bool partialMutationInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"ExecuteActions", partialMutationPayload, &partialMutationRequest);
    dispatch->RestoreSelectCtrlFrontFixture();
    dispatch->PrepareAtomicAppendFailure(false);
    const bool atomicRollbackInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"ExecuteActions", atomicRollbackPayload, &atomicRollbackRequest);
    const bool atomicRollbackRetryInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            atomicRollbackPayload,
            &atomicRollbackRetryRequest);
    const bool atomicRollbackRestored = dispatch->AtomicTailRollbackRestored();
    dispatch->PrepareAtomicAppendFailure(true);
    const bool atomicRollbackFailureInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            atomicRollbackPayload,
            &atomicRollbackFailureRequest);
    const bool atomicRollbackFailureRetainedTail =
        dispatch->AtomicTailRollbackFailureRetainedTail();
    dispatch->RestoreAtomicAppendFixture();
    dispatch->PrepareCheckpointRestoreFixture(false);
    std::wstring legacyCheckpointRestoreRequest;
    const bool legacyCheckpointRestoreInvoked =
        legacyCheckpointFixtureWritten &&
        SUCCEEDED(dispatchStatus) &&
        batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            legacyCheckpointRestorePayload.c_str(),
            &legacyCheckpointRestoreRequest);
    const bool legacyCheckpointRestoreRejected =
        legacyCheckpointRestoreRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_CONTENT\t",
            0) == 0 &&
        DecodedResponseField(legacyCheckpointRestoreRequest, 4).find(
            L"checkpoint content authorization signature is incomplete or "
            L"malformed; the document was not changed") != std::wstring::npos &&
        dispatch->CheckpointRestoreWasNotAttempted();
    dispatch->RestoreCheckpointRestoreFixture();
    const bool legacyCheckpointFixtureDeleted =
        !legacyCheckpointFixtureWritten ||
        DeleteFileW(legacyCheckpointPath.c_str()) != FALSE;

    const auto invokeCheckedCheckpointRestore =
        [&](std::wstring* const response) {
            std::wstring expectedContentSignature;
            return checkpointFixtureWritten &&
                SUCCEEDED(dispatchStatus) &&
                batch != nullptr &&
                InvokeString(
                    batch,
                    L"ContentSignature",
                    nullptr,
                    &expectedContentSignature) &&
                !expectedContentSignature.empty() &&
                InvokeTwoStrings(
                    batch,
                    L"ExecuteActionsChecked",
                    checkpointRestorePayload.c_str(),
                    expectedContentSignature.c_str(),
                    response);
        };
    dispatch->PrepareCheckpointRestoreFixture(false);
    const bool checkpointRestoreInvoked =
        invokeCheckedCheckpointRestore(&checkpointRestoreRequest);
    const bool checkpointRestoreSucceeded =
        checkpointRestoreRequest.rfind(L"HCA2\tOK\t1\t7\t", 0) == 0 &&
        dispatch->CheckpointRestoreSucceeded();
    dispatch->PrepareCheckpointRestoreFixture(true);
    const bool checkpointRollbackInvoked =
        invokeCheckedCheckpointRestore(&checkpointRollbackRequest);
    const bool checkpointRollbackSucceeded =
        checkpointRollbackRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_RESTORE\t",
            0) == 0 &&
        dispatch->CheckpointRollbackSucceeded();
    dispatch->PrepareCheckpointRestoreFailureFixture();
    std::wstring checkpointRollbackFailureRequest;
    const bool checkpointRollbackFailureInvoked =
        invokeCheckedCheckpointRestore(&checkpointRollbackFailureRequest);
    const std::wstring checkpointRollbackFailureMessage =
        DecodedResponseField(checkpointRollbackFailureRequest, 4);
    const std::vector<std::filesystem::path> checkpointRollbackRecoveryCopies =
        CheckpointRollbackCopies(checkpointPath);
    std::error_code checkpointRollbackFileError;
    const bool checkpointRollbackFailureKeptDiskCopy =
        checkpointRollbackFailureRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_ROLLBACK\t",
            0) == 0 &&
        checkpointRollbackRecoveryCopies.size() == 1 &&
        checkpointRollbackFailureMessage.find(
            checkpointRollbackRecoveryCopies.front().wstring()) !=
            std::wstring::npos &&
        std::filesystem::exists(
            checkpointRollbackRecoveryCopies.front(),
            checkpointRollbackFileError) &&
        !checkpointRollbackFileError &&
        std::filesystem::file_size(
            checkpointRollbackRecoveryCopies.front(),
            checkpointRollbackFileError) > 0 &&
        !checkpointRollbackFileError;
    RemoveCheckpointRollbackCopy(checkpointPath);
    dispatch->RestoreCheckpointRestoreFixture();
    const bool checkpointFixtureDeleted =
        !checkpointFixtureWritten ||
        DeleteFileW(checkpointPath.c_str()) != FALSE;
    // Document-file checkpoints are restored by replacing the bytes in the
    // existing task-owned document slot and reopening that slot. InsertFile is
    // intentionally not part of this contract: it can flatten sections or add
    // residue. Assert semantic state and owned-file behavior, not incidental
    // action/call-result counts.
    dispatch->SetDocumentPathFixture(userDocumentPath.wstring());
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    std::wstring checkpointDirectRestoreRequest;
    const bool checkpointDirectRestoreInvoked =
        checkpointDocumentFixtureWritten && SUCCEEDED(dispatchStatus) &&
        batch != nullptr && InvokeString(
            batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
            &checkpointDirectRestoreRequest);
    const std::wstring checkpointDirectRestoreCalls =
        DecodedCallResults(checkpointDirectRestoreRequest);
    const bool checkpointDirectRestoreExact =
        checkpointDirectRestoreRequest.rfind(L"HCA2\tOK\t1\t", 0) == 0 &&
        checkpointDirectRestoreCalls.find(
            L"DocumentRestoreAttempt=document_file_reopen\n") != std::wstring::npos &&
        checkpointDirectRestoreCalls.find(
            L"DocumentCheckpointRestore=content_signature_match\n") != std::wstring::npos &&
        checkpointDirectRestoreCalls.find(
            L"DocumentCheckpointRestore=document_file_reopen\n") != std::wstring::npos &&
        dispatch->CheckpointDirectRestoreExact() &&
        ReadFileBytes(userDocumentPath) == ReadFileBytes(checkpointDocumentPath) &&
        CheckpointRollbackCopies(checkpointDocumentPath).empty();

    const bool checkpointNoClearInputWritten =
        WriteDocumentCheckpointFixture(userDocumentPath) &&
        WriteDocumentCheckpointFixture(checkpointDocumentPath) &&
        WriteDocumentCheckpointMeta(checkpointDocumentPath, kCheckpointDocumentSignature);
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    dispatch->SetCheckpointClearFails();
    std::wstring checkpointNoClearRestoreRequest;
    const bool checkpointNoClearRestoreInvoked = checkpointNoClearInputWritten &&
        InvokeString(batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
                     &checkpointNoClearRestoreRequest);
    const std::wstring checkpointNoClearRestoreCalls =
        DecodedCallResults(checkpointNoClearRestoreRequest);
    const bool checkpointNoClearRestoreExact =
        checkpointNoClearRestoreRequest.rfind(L"HCA2\tOK\t1\t", 0) == 0 &&
        checkpointNoClearRestoreCalls.find(
            L"DocumentRestoreAttempt=document_file_reopen\n") != std::wstring::npos &&
        checkpointNoClearRestoreCalls.find(
            L"DocumentRestoreAttempt=document_file_reopen_no_clear\n") !=
            std::wstring::npos &&
        checkpointNoClearRestoreCalls.find(
            L"DocumentCheckpointRestore=document_file_reopen_no_clear\n") !=
            std::wstring::npos &&
        dispatch->CheckpointNoClearRestoreExact() &&
        ReadFileBytes(userDocumentPath) == ReadFileBytes(checkpointDocumentPath) &&
        CheckpointRollbackCopies(checkpointDocumentPath).empty();

    const bool checkpointNoClearRollbackInputWritten =
        WriteDocumentCheckpointFixture(userDocumentPath) &&
        WriteDocumentCheckpointFixture(checkpointDocumentPath) &&
        WriteDocumentCheckpointMeta(checkpointDocumentPath, kCheckpointDocumentSignature);
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    dispatch->SetCheckpointClearFails();
    dispatch->SetCheckpointDirectTargetMismatch();
    std::wstring checkpointNoClearRollbackRequest;
    const bool checkpointNoClearRollbackInvoked = checkpointNoClearRollbackInputWritten &&
        InvokeString(batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
                     &checkpointNoClearRollbackRequest);
    const bool checkpointNoClearRollbackExact =
        checkpointNoClearRollbackRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_RESTORE\t", 0) == 0 &&
        DecodedResponseField(checkpointNoClearRollbackRequest, 4).find(
            L"the previous document was restored") != std::wstring::npos &&
        dispatch->CheckpointNoClearRollbackExact() &&
        ReadFileBytes(userDocumentPath) == "HWP ROLLBACK COPY" &&
        CheckpointRollbackCopies(checkpointDocumentPath).empty();

    const bool checkpointDirectRollbackInputWritten =
        WriteDocumentCheckpointFixture(userDocumentPath) &&
        WriteDocumentCheckpointFixture(checkpointDocumentPath) &&
        WriteDocumentCheckpointMeta(checkpointDocumentPath, kCheckpointDocumentSignature);
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    dispatch->SetCheckpointDirectTargetMismatch();
    std::wstring checkpointDirectRollbackRequest;
    const bool checkpointDirectRollbackInvoked = checkpointDirectRollbackInputWritten &&
        InvokeString(batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
                     &checkpointDirectRollbackRequest);
    const std::wstring checkpointDirectRollbackCalls =
        DecodedCallResults(checkpointDirectRollbackRequest);
    const bool checkpointDirectRollbackSemanticExact =
        dispatch->CheckpointDirectRollbackExact();
    const bool checkpointDirectRollbackBytesExact =
        ReadFileBytes(userDocumentPath) == "HWP ROLLBACK COPY";
    const bool checkpointDirectRollbackOwnedFilesClean =
        CheckpointRollbackCopies(checkpointDocumentPath).empty();
    const bool checkpointDirectRollbackExact =
        checkpointDirectRollbackRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_RESTORE\t", 0) == 0 &&
        DecodedResponseField(checkpointDirectRollbackRequest, 4).find(
            L"the previous document was restored") != std::wstring::npos &&
        checkpointDirectRollbackCalls.empty() &&
        checkpointDirectRollbackSemanticExact &&
        checkpointDirectRollbackBytesExact &&
        checkpointDirectRollbackOwnedFilesClean;
    const std::wstring checkpointDirectRollbackState =
        dispatch->CheckpointDirectState();

    const bool checkpointUnsignedMetaWritten =
        WriteDocumentCheckpointMeta(checkpointDocumentPath, "");
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    std::wstring checkpointUnsignedRequest;
    const bool checkpointUnsignedInvoked = checkpointUnsignedMetaWritten &&
        InvokeString(batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
                     &checkpointUnsignedRequest);
    const bool checkpointUnsignedRefusedUntouched =
        checkpointUnsignedRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_CONTENT\t", 0) == 0 &&
        DecodedResponseField(checkpointUnsignedRequest, 4).find(
            L"signature is incomplete or malformed; the document was not changed") !=
            std::wstring::npos &&
        dispatch->CheckpointDocumentFileUntouched() &&
        CheckpointRollbackCopies(checkpointDocumentPath).empty();

    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    std::wstring blockProbeRequest;
    const bool blockProbeInvoked = InvokeString(
        batch, L"ExecuteActions", blockProbePayload.c_str(), &blockProbeRequest);
    const std::wstring blockProbeCalls = DecodedCallResults(blockProbeRequest);
    const std::wstring blockProbeBlock = L"CHECKPOINT_ROLLBACK";
    const std::wstring blockProbeSize = std::to_wstring(blockProbeBlock.size());
    const bool blockProbeReported =
        blockProbeRequest.rfind(L"HCA2\tOK\t1\t0\t", 0) == 0 &&
        blockProbeCalls.find(L"DocumentBlockProbe=captured\n") != std::wstring::npos &&
        blockProbeCalls.find(L"DocumentBlockProbeLength=" + blockProbeSize + L"\n") !=
            std::wstring::npos &&
        blockProbeCalls.find(L"DocumentBlockProbeWritten=" + blockProbeSize + L"\n") !=
            std::wstring::npos &&
        std::filesystem::exists(blockProbePath) &&
        std::filesystem::file_size(blockProbePath) == blockProbeBlock.size() &&
        dispatch->CheckpointDocumentFileUntouched();
    std::error_code blockProbeCleanup;
    static_cast<void>(std::filesystem::remove(blockProbePath, blockProbeCleanup));

    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    dispatch->SetCheckpointDocumentBlock(L"");
    std::wstring blockProbeEmptyRequest;
    const bool blockProbeEmptyInvoked = InvokeString(
        batch, L"ExecuteActions", blockProbePayload.c_str(), &blockProbeEmptyRequest);
    const std::wstring blockProbeEmptyCalls = DecodedCallResults(blockProbeEmptyRequest);
    const bool blockProbeEmptyReported =
        blockProbeEmptyRequest.rfind(L"HCA2\tOK\t1\t0\t", 0) == 0 &&
        blockProbeEmptyCalls.find(L"DocumentBlockProbe=allocation_failed\n") !=
            std::wstring::npos &&
        blockProbeEmptyCalls.find(L"DocumentBlockProbeLength=0\n") !=
            std::wstring::npos &&
        !std::filesystem::exists(blockProbePath) &&
        dispatch->CheckpointDocumentFileUntouched();

    const bool checkpointEngineMetaWritten = WriteDocumentCheckpointMeta(
        checkpointDocumentPath, kCheckpointDocumentSignature,
        kCheckpointDocumentSignature, kCheckpointDocumentExpectedPages);
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    dispatch->SetCheckpointEngineUndoRestores();
    std::wstring checkpointEngineUndoRequest;
    const bool checkpointEngineUndoInvoked = checkpointEngineMetaWritten &&
        InvokeString(batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
                     &checkpointEngineUndoRequest);
    const std::wstring checkpointEngineUndoCalls =
        DecodedCallResults(checkpointEngineUndoRequest);
    const bool checkpointEngineUndoExact =
        checkpointEngineUndoRequest.rfind(L"HCA2\tOK\t1\t", 0) == 0 &&
        checkpointEngineUndoCalls.find(
            L"DocumentRestoreAttempt=engine_undo\n") != std::wstring::npos &&
        checkpointEngineUndoCalls.find(
            L"DocumentCheckpointRestoreInsert=engine_undo_verified\n") !=
            std::wstring::npos &&
        dispatch->CheckpointEngineUndoRestoredExactly() &&
        CheckpointRollbackCopies(checkpointDocumentPath).empty();

    const bool checkpointWrongStepMetaWritten = WriteDocumentCheckpointMeta(
        checkpointDocumentPath, kCheckpointDocumentSignature,
        kCheckpointDocumentSignature, kCheckpointDocumentExpectedPages);
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    dispatch->SetCheckpointEngineUndoWrongStep();
    std::wstring checkpointWrongStepRequest;
    const bool checkpointWrongStepInvoked = checkpointWrongStepMetaWritten &&
        InvokeString(batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
                     &checkpointWrongStepRequest);
    const bool checkpointWrongStepReversed =
        checkpointWrongStepRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_ENGINE_HISTORY_DRIFT\t", 0) == 0 &&
        DecodedResponseField(checkpointWrongStepRequest, 4).find(
            L"one Redo restored the exact state this call found") !=
            std::wstring::npos &&
        dispatch->CheckpointEngineWrongStepWasReversed() &&
        CheckpointRollbackCopies(checkpointDocumentPath).empty();

    const bool checkpointDirectFailureInputWritten =
        WriteDocumentCheckpointFixture(userDocumentPath) &&
        WriteDocumentCheckpointFixture(checkpointDocumentPath) &&
        WriteDocumentCheckpointMeta(checkpointDocumentPath, kCheckpointDocumentSignature);
    dispatch->PrepareCheckpointDocumentFileFixture(false, true);
    dispatch->SetCheckpointDirectRollbackFailure();
    std::wstring checkpointDirectRollbackFailureRequest;
    const bool checkpointDirectRollbackFailureInvoked = checkpointDirectFailureInputWritten &&
        InvokeString(batch, L"ExecuteActions", checkpointDocumentRestorePayload.c_str(),
                     &checkpointDirectRollbackFailureRequest);
    const std::vector<std::filesystem::path> checkpointDirectRecoveryCopies =
        CheckpointRollbackCopies(checkpointDocumentPath);
    const std::filesystem::path checkpointDirectRecoveryCopy =
        checkpointDirectRecoveryCopies.size() == 1
            ? checkpointDirectRecoveryCopies.front() : std::filesystem::path();
    const std::wstring checkpointDirectRollbackFailureMessage =
        DecodedResponseField(checkpointDirectRollbackFailureRequest, 4);
    const bool checkpointDirectRollbackFailurePreserved =
        checkpointDirectRollbackFailureRequest.rfind(
            L"HCA2\tERROR\tDOCUMENT_CHECKPOINT_ROLLBACK\t", 0) == 0 &&
        checkpointDirectRollbackFailureMessage.find(
            L"a copy of the document as this call found it was kept at") !=
            std::wstring::npos &&
        checkpointDirectRecoveryCopies.size() == 1 &&
        checkpointDirectRollbackFailureMessage.find(
            checkpointDirectRecoveryCopy.wstring()) != std::wstring::npos &&
        std::filesystem::exists(checkpointDirectRecoveryCopy) &&
        !std::filesystem::exists(std::filesystem::path(
            checkpointDirectRecoveryCopy.wstring() + L".gsgmeta")) &&
        ReadFileBytes(checkpointDirectRecoveryCopy) == "HWP ROLLBACK COPY" &&
        ReadFileBytes(userDocumentPath) == "HWP ROLLBACK COPY" &&
        dispatch->CheckpointDirectRollbackFailed();
    const std::wstring checkpointDirectRollbackFailureState =
        dispatch->CheckpointDirectState();
    const bool checkpointDirectRecoverySidecarAbsent =
        checkpointDirectRecoveryCopies.size() == 1 &&
        !std::filesystem::exists(std::filesystem::path(
            checkpointDirectRecoveryCopy.wstring() + L".gsgmeta"));
    RemoveCheckpointRollbackCopy(checkpointDocumentPath);
    const bool checkpointDirectRecoveryCleaned =
        CheckpointRollbackCopies(checkpointDocumentPath).empty();

    dispatch->RestoreCheckpointDocumentFileFixture();
    dispatch->SetDocumentPathFixture(L"C:\\x.hwp");
    std::error_code checkpointCleanupError;
    const bool checkpointDocumentFixtureDeleted =
        (!checkpointDocumentFixtureWritten ||
         (std::filesystem::remove(checkpointDocumentPath, checkpointCleanupError) &&
          std::filesystem::remove(
              std::filesystem::path(checkpointDocumentPath.wstring() + L".gsgmeta"),
              checkpointCleanupError))) &&
        (!userDocumentFixtureWritten ||
         std::filesystem::remove(userDocumentPath, checkpointCleanupError)) &&
        !std::filesystem::exists(checkpointDocumentPath) &&
        !std::filesystem::exists(std::filesystem::path(
            checkpointDocumentPath.wstring() + L".gsgmeta")) &&
        !std::filesystem::exists(userDocumentPath) &&
        CheckpointRollbackCopies(checkpointDocumentPath).empty();
    dispatch->PrepareEmptyCellTextFixture();
    const bool emptyCellTextFixtureReady =
        dispatch->EmptyCellTextFixtureReady();
    const bool emptyCellTextInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            emptyCellTextPayload,
            &emptyCellTextRequest);
    const bool emptyCellTextSucceeded =
        emptyCellTextRequest.rfind(L"HCA2\tOK\t2\t0\t1\t0\t", 0) == 0;
    const bool emptyCellTextInsertedExactly =
        dispatch->EmptyCellTextInsertedExactly();
    dispatch->RestoreReferenceLayoutFixture();
    std::vector<std::wstring> multilineCellTextResponses;
    std::vector<bool> multilineCellTextCasePassed;
    bool multilineCellTextInvoked = true;
    bool multilineCellTextPassed = true;
    for (const MultilineCellTextCase& test : multilineCellTextCases) {
        dispatch->PrepareMultilineCellTextFixture(test.patchText);
        std::wstring response;
        const bool invoked =
            SUCCEEDED(dispatchStatus) && batch != nullptr &&
            InvokeString(
                batch,
                L"ExecuteActions",
                test.payload,
                &response);
        const bool passed =
            invoked && response.rfind(L"HCA2\tOK\t3\t", 0) == 0 &&
            dispatch->MultilineCellTextWrittenExactly(
                test.address,
                test.expected);
        multilineCellTextInvoked = multilineCellTextInvoked && invoked;
        multilineCellTextPassed = multilineCellTextPassed && passed;
        multilineCellTextResponses.push_back(response);
        multilineCellTextCasePassed.push_back(passed);
        dispatch->RestoreReferenceLayoutFixture();
    }
    dispatch->PrepareAllTargetCellTextPreflightFixture();
    std::wstring allTargetCellTextPreflightResponse;
    const bool allTargetCellTextPreflightInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            allTargetCellTextPreflightPayload,
            &allTargetCellTextPreflightResponse);
    const bool allTargetCellTextPreflightPassed =
        SafeStaleCellPreflightFailed(allTargetCellTextPreflightResponse) &&
        dispatch->AllTargetCellTextPreflightPreserved();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareAllTargetCellTextPreflightFixture();
    std::wstring expandCellTextPreflightResponse;
    const bool expandCellTextPreflightInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            expandCellTextPreflightPayload,
            &expandCellTextPreflightResponse);
    const bool expandCellTextPreflightPassed =
        SafeStaleCellPreflightFailed(expandCellTextPreflightResponse) &&
        dispatch->AllTargetCellTextPreflightPreserved();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareTableTextPreflightSelectionFixture(false, false);
    std::wstring strictCellSelectionPreflightResponse;
    const bool strictCellSelectionPreflightInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            selectionCompatibleTableTextPayload,
            &strictCellSelectionPreflightResponse);
    const bool strictCellSelectionPreflightPassed =
        strictCellSelectionPreflightResponse.rfind(L"HCA2\tOK\t3\t", 0) == 0 &&
        dispatch->TableTextPreflightSelectionBatchSucceeded();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareTableTextPreflightSelectionFixture(true, false);
    std::wstring tableControlSelectionPreflightResponse;
    const bool tableControlSelectionPreflightInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            selectionCompatibleTableTextPayload,
            &tableControlSelectionPreflightResponse);
    const bool tableControlSelectionPreflightPassed =
        tableControlSelectionPreflightResponse.rfind(L"HCA2\tOK\t3\t", 0) == 0 &&
        dispatch->TableTextPreflightSelectionBatchSucceeded();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareTableTextPreflightSelectionFixture(false, true);
    std::wstring selectionRestoreFailureResponse;
    const bool selectionRestoreFailureInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            selectionCompatibleTableTextPayload,
            &selectionRestoreFailureResponse);
    const bool selectionRestoreFailurePassed =
        SafeSelectionRestoreFailed(selectionRestoreFailureResponse) &&
        dispatch->TableTextPreflightRestoreFailurePreservedDocument();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareTopologyReuseFixture();
    std::wstring topologyReuseResponse;
    const bool topologyReuseInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyReusePayload,
            &topologyReuseResponse);
    const bool topologyReusePassed =
        topologyReuseResponse.rfind(L"HCA2\tOK\t4\t", 0) == 0 &&
        dispatch->TopologyWasReusedAcrossTextBatch();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareFormattingTopologyPolicyFixture();
    std::wstring topologyFormattingPreservationResponse;
    const bool topologyFormattingPreservationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyFormattingPreservationPayload,
            &topologyFormattingPreservationResponse);
    const bool topologyFormattingPreservationPassed =
        topologyFormattingPreservationResponse.rfind(L"HCA2\tOK\t12\t", 0) == 0 &&
        dispatch->TopologyWasPreservedAfterCellFormatting();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareFormattingTopologyPolicyFixture();
    std::wstring topologyPaddingPreservationResponse;
    const bool topologyPaddingPreservationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyPaddingPreservationPayload,
            &topologyPaddingPreservationResponse);
    const bool topologyPaddingPreservationPassed =
        topologyPaddingPreservationResponse.rfind(L"HCA2\tOK\t5\t", 0) == 0 &&
        dispatch->TopologyWasPreservedAfterCellPadding();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareFormattingTopologyPolicyFixture();
    std::wstring topologyCellSizeInvalidationResponse;
    const bool topologyCellSizeInvalidationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyCellSizeInvalidationPayload,
            &topologyCellSizeInvalidationResponse);
    const bool topologyCellSizeInvalidationPassed =
        topologyCellSizeInvalidationResponse.rfind(L"HCA2\tOK\t5\t", 0) == 0 &&
        dispatch->TopologyWasInvalidatedAfterCellSizeChange();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareFormattingTopologyPolicyFixture();
    std::wstring topologyUnknownActionInvalidationResponse;
    const bool topologyUnknownActionInvalidationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyUnknownActionInvalidationPayload,
            &topologyUnknownActionInvalidationResponse);
    const bool topologyUnknownActionInvalidationPassed =
        topologyUnknownActionInvalidationResponse.rfind(L"HCA2\tOK\t5\t", 0) == 0 &&
        dispatch->TopologyWasInvalidatedAfterUnknownAction();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareTopologyInvalidationFixture();
    std::wstring topologyInvalidationResponse;
    const bool topologyInvalidationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyInvalidationPayload,
            &topologyInvalidationResponse);
    const bool topologyInvalidationPassed =
        CellTopologyInvalidationFailedSafely(topologyInvalidationResponse) &&
        dispatch->TopologyWasInvalidatedAfterMerge();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareRunTopologyInvalidationFixture();
    std::wstring topologyRunInvalidationResponse;
    const bool topologyRunInvalidationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyRunInvalidationPayload,
            &topologyRunInvalidationResponse);
    const bool topologyRunInvalidationPassed =
        topologyRunInvalidationResponse.rfind(L"HCA2\tOK\t5\t", 0) == 0 &&
        dispatch->TopologyWasInvalidatedAfterRunDeleteRow();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareActionTopologyInvalidationFixture();
    std::wstring topologyActionInvalidationResponse;
    const bool topologyActionInvalidationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyActionInvalidationPayload,
            &topologyActionInvalidationResponse);
    const bool topologyActionInvalidationPassed =
        topologyActionInvalidationResponse.rfind(L"HCA2\tOK\t5\t", 0) == 0 &&
        dispatch->TopologyWasInvalidatedAfterActionInsertColumn();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareCallTopologyInvalidationFixture();
    std::wstring topologyCallInvalidationResponse;
    const bool topologyCallInvalidationInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            topologyCallInvalidationPayload,
            &topologyCallInvalidationResponse);
    const bool topologyCallInvalidationPassed =
        CellTopologyInvalidationFailedSafely(topologyCallInvalidationResponse) &&
        dispatch->TopologyWasInvalidatedAfterCall();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareOversizedCellPatchFixture();
    std::wstring oversizedCellPatchResponse;
    const bool oversizedCellPatchInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            oversizedCellPatchPayload.c_str(),
            &oversizedCellPatchResponse);
    const bool oversizedCellPatchRejected =
        OversizedCellPatchRejectedSafely(oversizedCellPatchResponse) &&
        dispatch->OversizedCellPatchPreservedDocument();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareReferenceLayoutFixture(false);
    const bool referenceLayoutSuccessInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            referenceLayoutPayload,
            &referenceLayoutSuccessRequest);
    const bool referenceLayoutCreatedExactly =
        dispatch->ReferenceLayoutCreatedExactly();
    const bool referenceLayoutBorderReadbackCovered =
        dispatch->ReferenceBorderReadbackCoveredRequestedEdges();
    dispatch->PrepareReferenceLayoutFixture(true);
    const bool referenceLayoutAtomicRollbackInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            referenceLayoutAtomicRollbackPayload,
            &referenceLayoutAtomicRollbackRequest);
    const bool referenceLayoutAtomicRollbackRestored =
        dispatch->ReferenceLayoutRollbackRestored();
    dispatch->PrepareReferenceLayoutDroppedEdgesFixture();
    const bool referenceLayoutDroppedEdgesInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            referenceLayoutPayload,
            &referenceLayoutDroppedEdgesRequest);
    const bool referenceLayoutDroppedEdgesRejected =
        ReferenceLayoutDroppedEdgesRejected(
            referenceLayoutDroppedEdgesRequest) &&
        dispatch->ReferenceLayoutDroppedEdgesObserved();
    dispatch->RestoreReferenceLayoutFixture();
    dispatch->PrepareSelectedControlTextPatch();
    const bool selectedControlPatchInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            selectedControlPatchPayload,
            &selectedControlPatchRequest);
    const bool selectedControlPatchRejected =
        selectedControlPatchRequest.rfind(
            L"HCA2\tERROR\tNON_TEXT_SELECTION\t",
            0) == 0 &&
        dispatch->InsertTextExecutions() == 0;
    const size_t selectedControlPatchInsertions = dispatch->InsertTextExecutions();
    dispatch->RestoreTextPatchSelectionFixture();
    dispatch->PrepareTextDeletionPatch();
    const bool textDeletionPatchInvoked = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(
            batch,
            L"ExecuteActions",
            textDeletionPatchPayload,
            &textDeletionPatchRequest);
    const bool textDeletionPatchVerified =
        textDeletionPatchRequest.rfind(L"HCA2\tOK\t1\t", 0) == 0 &&
        dispatch->DeletedSelectedText();
    dispatch->RestoreTextDeletionPatch();
    dispatch->PrepareScopedInspectionFixture();
    const bool scopedStructureInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"InspectStructure", L"31", &scopedStructure);
    const bool scopedStructureSucceeded =
        scopedStructure.rfind(L"HDS1\n", 0) == 0 &&
        dispatch->UsedBoundedBackwardInspection();
    dispatch->PrepareScopedInspectionFixture();
    const bool unscopedStructureInvoked =
        SUCCEEDED(dispatchStatus) && batch != nullptr &&
        InvokeString(batch, L"InspectStructure", L"-31", &unscopedStructure);
    const bool unscopedStructureSucceeded =
        unscopedStructure.rfind(L"HDS1\n", 0) == 0 &&
        dispatch->PreservedUnscopedForwardInspection();
    dispatch->RestoreScopedInspectionFixture();
    const bool graphDispatchAbi = SUCCEEDED(dispatchStatus) && batch != nullptr &&
        ReadGraphDispatchAbi(batch);
    if (!cellTopologyOwnerIndex || !tableCellFormatSampling ||
        !paragraphTextNormalization ||
        FAILED(dispatchStatus) || !ReadProtocolVersion(batch) ||
        !graphDispatchAbi ||
        !dynamicDocumentActivationWorked ||
        !saveVerifyInvoked || !saveVerifyMatched ||
        !saveVerifyOversizeInvoked ||
        !saveVerifyOversizeSkippedSecondAttempt ||
        !saveVerifySectionDiagnosticsInvoked ||
        !saveVerifySectionDiagnosticsMatched ||
        !lifecycleSuccessInvoked || !lifecycleSuccessMatched ||
        !lifecycleCleanNoOpInvoked || !lifecycleCleanNoOpMatched ||
        !lifecycleSaveGateInvoked || !lifecycleSaveGateMatched ||
        !lifecycleSaveReturnGateInvoked || !lifecycleSaveReturnGateMatched ||
        !lifecycleRecoveryCaptureGateInvoked ||
        !lifecycleRecoveryCaptureGateMatched ||
        !lifecycleRecoveryInvoked || !lifecycleRecoveryMatched ||
        !lifecycleHwpxSuccessInvoked || !lifecycleHwpxSuccessMatched ||
        !lifecycleHwpxRecoveryInvoked || !lifecycleHwpxRecoveryMatched ||
        !lifecycleFingerprintRecoveryInvoked ||
        !lifecycleFingerprintRecoveryMatched ||
        !lifecycleHwpxFingerprintRecoveryInvoked ||
        !lifecycleHwpxFingerprintRecoveryMatched ||
        !partialMutationInvoked || partialMutationRequest.rfind(
            L"HCA2\tERROR\tACTION_FAILED\tU2VsZWN0Q3RybEZyb250\t"
            L"U2VsZWN0Q3RybEZyb250IHJldHVybmVkIGZhbHNl\t1\t"
            L"U2VsZWN0Q3RybEZyb250\t1\t0\t",
            0) != 0 ||
        !atomicRollbackInvoked || !AtomicRollbackSucceeded(atomicRollbackRequest) ||
        !atomicRollbackRetryInvoked || !AtomicRollbackSucceeded(atomicRollbackRetryRequest) ||
        !atomicRollbackRestored ||
        !atomicRollbackFailureInvoked || !AtomicRollbackFailurePreservedOriginal(
            atomicRollbackRequest,
            atomicRollbackFailureRequest) ||
        !atomicRollbackFailureRetainedTail ||
        !legacyCheckpointFixtureWritten ||
        !legacyCheckpointRestoreInvoked ||
        !legacyCheckpointRestoreRejected ||
        !legacyCheckpointFixtureDeleted ||
        !checkpointFixtureWritten ||
        encodedCheckpointPath.empty() ||
        !checkpointRestoreInvoked ||
        !checkpointRestoreSucceeded ||
        !checkpointRollbackInvoked ||
        !checkpointRollbackSucceeded ||
        !checkpointFixtureDeleted ||
        !emptyCellTextFixtureReady ||
        !emptyCellTextInvoked ||
        !emptyCellTextSucceeded ||
        !emptyCellTextInsertedExactly ||
        !multilineCellTextInvoked ||
        !multilineCellTextPassed ||
        !allTargetCellTextPreflightInvoked ||
        !allTargetCellTextPreflightPassed ||
        !expandCellTextPreflightInvoked ||
        !expandCellTextPreflightPassed ||
        !strictCellSelectionPreflightInvoked ||
        !strictCellSelectionPreflightPassed ||
        !tableControlSelectionPreflightInvoked ||
        !tableControlSelectionPreflightPassed ||
        !selectionRestoreFailureInvoked ||
        !selectionRestoreFailurePassed ||
        !topologyReuseInvoked ||
        !topologyReusePassed ||
        !topologyFormattingPreservationInvoked ||
        !topologyFormattingPreservationPassed ||
        !topologyPaddingPreservationInvoked ||
        !topologyPaddingPreservationPassed ||
        !topologyCellSizeInvalidationInvoked ||
        !topologyCellSizeInvalidationPassed ||
        !topologyUnknownActionInvalidationInvoked ||
        !topologyUnknownActionInvalidationPassed ||
        !topologyInvalidationInvoked ||
        !topologyInvalidationPassed ||
        !topologyRunInvalidationInvoked ||
        !topologyRunInvalidationPassed ||
        !topologyActionInvalidationInvoked ||
        !topologyActionInvalidationPassed ||
        !topologyCallInvalidationInvoked ||
        !topologyCallInvalidationPassed ||
        !oversizedCellPatchInvoked ||
        !oversizedCellPatchRejected ||
        !messageBoxModeRestoreRetryInvoked ||
        !messageBoxModeRestoreRetryPassed ||
        !referenceLayoutSuccessInvoked ||
        !ReferenceLayoutSucceeded(referenceLayoutSuccessRequest) ||
        !referenceLayoutCreatedExactly ||
        !referenceLayoutBorderReadbackCovered ||
        !referenceLayoutAtomicRollbackInvoked ||
        !ReferenceLayoutAtomicRollbackSucceeded(referenceLayoutAtomicRollbackRequest) ||
        !referenceLayoutAtomicRollbackRestored ||
        !referenceLayoutDroppedEdgesInvoked ||
        !referenceLayoutDroppedEdgesRejected ||
        !selectedControlPatchInvoked || !selectedControlPatchRejected ||
        !textDeletionPatchInvoked || !textDeletionPatchVerified ||
        !scopedStructureInvoked || !scopedStructureSucceeded ||
        !unscopedStructureInvoked || !unscopedStructureSucceeded ||
        !HasDispatchMember(batch, L"ExecuteProtocolBundle") ||
        !InvokeString(batch, L"Ping", nullptr, &ping) || ping != L"HCB14\tPONG\t14" ||
        !InvokeString(batch, L"Execute", L"not-a-request", &badRequest) ||
        badRequest.rfind(L"HCB1\tERROR\tBAD_REQUEST\t", 0) != 0 ||
        !InvokeString(batch, L"Execute", unsavedBatchPayload, &unsavedBatchRequest) ||
        unsavedBatchRequest.rfind(L"HCB1\tERROR\tSTALE_DOCUMENT\t", 0) != 0 ||
        !InvokeString(batch, L"Snapshot", nullptr, &snapshot) ||
        snapshot.rfind(L"HCI1\tERROR\t", 0) != 0 ||
        !InvokeString(batch, L"InspectPage", L"1", &page) ||
        page.rfind(L"HCI1\tERROR\t", 0) != 0 ||
        !InvokeString(batch, L"InspectPageV3", L"1", &pageV3) ||
        pageV3.rfind(L"HCI1\tERROR\t", 0) != 0 ||
        !InvokeString(batch, L"InspectPagesV3", L"1", &pageBatch) ||
        pageBatch.rfind(L"HCI1\tERROR\t", 0) != 0 ||
        !InvokeString(batch, L"InspectPageSummary", L"1", &pageSummary) ||
        pageSummary.rfind(L"HCI1\tERROR\t", 0) != 0 ||
        !InvokeString(batch, L"InspectRoutingContext", L"0", &routingContext) ||
        routingContext.rfind(L"HCI1\tERROR\t", 0) != 0 ||
        !InvokeString(batch, L"InspectStructure", L"1", &structure) ||
        structure.rfind(L"HCI1\tERROR\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", L"not-a-request", &badActionRequest) ||
        badActionRequest.rfind(L"HCA1\tERROR\tBAD_REQUEST\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", unsavedActionPayload, &unsavedActionRequest) ||
        unsavedActionRequest.rfind(L"HCA2\tERROR\tSTALE_DOCUMENT\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", badArrayIndexPayload, &badArrayIndexRequest) ||
        badArrayIndexRequest.rfind(L"HCA1\tERROR\tBAD_REQUEST\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", validArrayPayload, &validArrayRequest) ||
        validArrayRequest.rfind(L"HCA2\tOK\t1\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", sizedPicturePayload, &sizedPictureRequest) ||
        sizedPictureRequest.rfind(L"HCA2\tERROR\tSTALE_DOCUMENT\t", 0) != 0 ||
        !InvokeString(
            batch,
            L"ExecuteActions",
            croppedPicturePayload,
            &croppedPictureRequest) ||
        croppedPictureRequest.rfind(L"HCA2\tERROR\tSTALE_DOCUMENT\t", 0) != 0 ||
        !InvokeString(
            batch,
            L"ExecuteActions",
            badCropPicturePayload,
            &badCropPictureRequest) ||
        badCropPictureRequest.rfind(L"HCA1\tERROR\tBAD_REQUEST\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", formattedCaptionPayload, &formattedCaptionRequest) ||
        formattedCaptionRequest.rfind(L"HCA2\tERROR\tSTALE_DOCUMENT\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", capturedTablePayload, &capturedTableRequest) ||
        capturedTableRequest.rfind(L"HCA2\tOK\t4\t", 0) != 0 ||
        capturedTableRequest.find(L"\tY2FwdHVyZWQtdGFibGU=") == std::wstring::npos ||
        !InvokeString(batch, L"ExecuteActions", deleteControlPayload, &deleteControlRequest) ||
        deleteControlRequest.rfind(L"HCA2\tOK\t1\t1\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", invalidPasteOrderPayload, &invalidPasteOrderRequest) ||
        invalidPasteOrderRequest.rfind(
            L"HCA2\tERROR\tNO_TABLE_ANCHOR_FORMAT\t", 0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", callReturnPayload, &callReturnRequest) ||
        callReturnRequest.rfind(L"HCA2\tOK\t4\t0\t0\t0\t", 0) != 0 ||
        callReturnRequest.find(std::wstring(L"\t\t") + expectedCallResults) == std::wstring::npos ||
        !InvokeString(
            batch,
            L"ExecuteActions",
            staleSelectionPayload,
            &staleSelectionRequest) ||
        staleSelectionRequest.rfind(
            L"HCA2\tERROR\tSTALE_SELECTION_TEXT\t"
            L"c2VsZWN0aW9u\t"
            L"c2VsZWN0ZWQgdGV4dCBjaGFuZ2VkIGFmdGVyIHRoZSByZXF1ZXN0IHdhcyBwcmVwYXJlZA==\t"
            L"0\tUkVQTEFDRV9TRUxFQ1RJT04=\t0\t1\t",
            0) != 0 ||
        !dispatch->InsertedText().empty() ||
        !InvokeString(
            batch,
            L"ExecuteActions",
            replaceSelectionPayload,
            &replaceSelectionRequest) ||
        replaceSelectionRequest.rfind(L"HCA2\tOK\t1\t0\t1\t0\t", 0) != 0 ||
        dispatch->InsertedText() != L"new" ||
        !InvokeString(batch, L"ProbeOfficialApi", L"not-a-request", &badOfficialApiRequest) ||
        badOfficialApiRequest.rfind(L"HCV1\tERROR\tBAD_REQUEST\t", 0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
        actionOfficialApiPayload,
        &actionOfficialApiRequest) ||
        actionOfficialApiRequest.rfind(L"HCV1\tACTION\tCharShapeBold\t", 0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            validationActionOfficialApiPayload,
            &validationActionOfficialApiRequest) ||
        validationActionOfficialApiRequest.rfind(
            L"HCV1\tACTION\tCharShapeBold\t0\t0\t0\t0\t0\t1\t1\t0\t1\t"
            L"-2147418113\t-2\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            actionInputOfficialApiPayload,
            &actionInputOfficialApiRequest) ||
        actionInputOfficialApiRequest.rfind(
            L"HCV1\tACTION\tSetWithoutDefault\t0\t0\t0\t0\t"
            L"-2147467259\t-2\t0\t0\t1\t-2147418113\t-2\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            fallbackActionOfficialApiPayload,
            &fallbackActionOfficialApiRequest) ||
        fallbackActionOfficialApiRequest.rfind(
            L"HCV1\tACTION\tAutoSpellRun\t0\t-2147352571\t"
            L"-2147418113\t-2147418113\t-2147418113\t-2\t0\t"
            L"-2147418113\t-2\t0\t1\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            sehActionOfficialApiPayload,
            &sehActionOfficialApiRequest) ||
        sehActionOfficialApiRequest.rfind(
            L"HCV1\tACTION\tRaiseStructured\t0\t0\t0\t0\t0\t1\t0\t"
            L"-268430796\t-2\t0\t1\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            parameterOfficialApiPayload,
            &parameterOfficialApiRequest) ||
        parameterOfficialApiRequest.rfind(
            L"HCV1\tPARAMETER_SET\tCharShape\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            fallbackParameterOfficialApiPayload,
            &fallbackParameterOfficialApiRequest) ||
        fallbackParameterOfficialApiRequest.rfind(
            L"HCV1\tPARAMETER_SET\tChangeRome\t-2147352571\t0\t1\t1\t1\t-1\t1\t1\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            normalizedParameterOfficialApiPayload,
            &normalizedParameterOfficialApiRequest) ||
        normalizedParameterOfficialApiRequest.rfind(
            L"HCV1\tPARAMETER_SET\tConvertCase\t0\t0\t0\t0\t0\t1\t1\t1\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            reportedMissingParameterOfficialApiPayload,
            &reportedMissingParameterOfficialApiRequest) ||
        reportedMissingParameterOfficialApiRequest.find(
            L"\nITEM\tReportedMissing\t0\t0\t0\t0\t0\t1") == std::wstring::npos ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            automationOfficialApiPayload,
            &automationOfficialApiRequest) ||
        automationOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tIHwpObject\tPageCount\tproperty\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            eventOfficialApiPayload,
            &eventOfficialApiRequest) ||
        eventOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tIHwpObjectEvents\tDocumentChange\tevent\t"
            L"0\t0\t0\t-2147467263\t3\t11\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            aliasOfficialApiPayload,
            &aliasOfficialApiRequest) ||
        aliasOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tIHwpObject\tRenameMetatag\tmethod\t0\t0\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            itemAliasOfficialApiPayload,
            &itemAliasOfficialApiRequest) ||
        itemAliasOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tIDHwpParameterSet\tItemExsit\tmethod\t0\t0\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            messageBoxOwnerOfficialApiPayload,
            &messageBoxOwnerOfficialApiRequest) ||
        messageBoxOwnerOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tIXHwpMessageBox\tApplication\tproperty\t0\t0\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            writeOnlyPropertyOfficialApiPayload,
            &writeOnlyPropertyOfficialApiRequest) ||
        writeOnlyPropertyOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tIXHwpDocument\tPassword\tproperty\t0\t0\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            hActionGetDefaultOfficialApiPayload,
            &hActionGetDefaultOfficialApiRequest) ||
        hActionGetDefaultOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tHAction\tGetDefault\tmethod\t0\t0\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            hActionExecuteOfficialApiPayload,
            &hActionExecuteOfficialApiRequest) ||
        hActionExecuteOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tHAction\tExecute\tmethod\t0\t0\t0\t",
            0) != 0 ||
        !InvokeString(
            batch,
            L"ProbeOfficialApi",
            setIdAliasOfficialApiPayload,
            &setIdAliasOfficialApiRequest) ||
        setIdAliasOfficialApiRequest.rfind(
            L"HCV1\tAUTOMATION\tIDHwpParameterSet\tGetSetID\tproperty\t0\t0\t0\t",
            0) != 0 ||
        !InvokeString(batch, L"ExecuteActions", captionTablePayload, &captionTableRequest) ||
        captionTableRequest.rfind(L"HCA2\tOK\t5\t4\t1\t0\t", 0) != 0 ||
        dispatch->RanBreakPage() ||
        !dispatch->UsedAutoYesMessageBoxMode() ||
        !dispatch->MessageBoxModeRestored() ||
        !dispatch->UsedNativeTableBlockCopy() ||
        !dispatch->UsedNativeTableBlockPaste() ||
        dispatch->UsedClipboardTableTransfer() ||
        !dispatch->PasteUsedSourceAnchorFormat() ||
        !dispatch->PreservedAttachedCaptionNumber() ||
        !streamingScanWorked || streamedCellText != L"cell-text" ||
        !dispatch->UsedStreamingTextScan() ||
        // Last on purpose. Terms in this condition are not all pure reads --
        // the caption batch above runs inside it -- so a term that fires early
        // stops the ones after it from ever running, and their flags then print
        // as zero. A new check placed in the middle therefore reports itself as
        // nine unrelated failures. These go after everything that does work.
        !abandonedCheckpointSmokeFilesRemoved ||
        !checkpointDocumentFixtureWritten ||
        encodedCheckpointDocumentPath.empty() ||
        !checkpointRollbackFailureInvoked ||
        !checkpointRollbackFailureKeptDiskCopy ||
        !checkpointDirectRestoreInvoked ||
        !checkpointDirectRestoreExact ||
        !checkpointNoClearRestoreInvoked ||
        !checkpointNoClearRestoreExact ||
        !checkpointNoClearRollbackInvoked ||
        !checkpointNoClearRollbackExact ||
        !checkpointDirectRollbackInvoked ||
        !checkpointDirectRollbackExact ||
        !checkpointDirectRollbackFailureInvoked ||
        !checkpointDirectRollbackFailurePreserved ||
        !checkpointDirectRecoveryCleaned ||
        !checkpointUnsignedInvoked ||
        !checkpointUnsignedRefusedUntouched ||
        !checkpointEngineUndoInvoked ||
        !checkpointEngineUndoExact ||
        !checkpointWrongStepInvoked ||
        !checkpointWrongStepReversed ||
        !blockProbeInvoked ||
        !blockProbeReported ||
        !blockProbeEmptyInvoked ||
        !blockProbeEmptyReported ||
        !checkpointDocumentFixtureDeleted) {
        std::wcerr
            << L"batch automation contract failed"
            << L"\n  lifecycle-success=" << lifecycleSuccessResponse
            << L"\n  save-verify=" << saveVerifyResponse
            << L"\n  save-verify-matched=" << saveVerifyMatched
            << L"\n  save-verify-oversize=" << saveVerifyOversizeResponse
            << L"\n  save-verify-oversize-serialization-attempts="
            << saveVerifyOversizeSerializationAttempts
            << L"\n  save-verify-section-diagnostics="
            << saveVerifySectionDiagnosticsResponse
            << L"\n  save-verify-section-diagnostics-matched="
            << saveVerifySectionDiagnosticsMatched
            << L"\n  lifecycle-success-sequence=" << dispatch->CompletedLifecycleSequence()
            << L"\n  lifecycle-clean-noop=" << lifecycleCleanNoOpResponse
            << L"\n  lifecycle-clean-noop-matched=" << lifecycleCleanNoOpMatched
            << L"\n  lifecycle-save-gate=" << lifecycleSaveGateResponse
            << L"\n  lifecycle-save-stopped=" << dispatch->StoppedLifecycleAfterSave()
            << L"\n  lifecycle-save-return-gate=" << lifecycleSaveReturnGateResponse
            << L"\n  lifecycle-recovery-capture-gate="
            << lifecycleRecoveryCaptureGateResponse
            << L"\n  lifecycle-recovery-capture-gate-matched="
            << lifecycleRecoveryCaptureGateMatched
            << L"\n  lifecycle-recovery=" << lifecycleRecoveryResponse
            << L"\n  lifecycle-recovery-matched=" << lifecycleRecoveryMatched
            << L"\n  lifecycle-hwpx-success=" << lifecycleHwpxSuccessResponse
            << L"\n  lifecycle-hwpx-success-matched="
            << lifecycleHwpxSuccessMatched
            << L"\n  lifecycle-hwpx-recovery=" << lifecycleHwpxRecoveryResponse
            << L"\n  lifecycle-hwpx-recovery-matched="
            << lifecycleHwpxRecoveryMatched
            << L"\n  lifecycle-fingerprint-recovery="
            << lifecycleFingerprintRecoveryResponse
            << L"\n  lifecycle-fingerprint-recovery-matched="
            << lifecycleFingerprintRecoveryMatched
            << L"\n  lifecycle-hwpx-fingerprint-recovery="
            << lifecycleHwpxFingerprintRecoveryResponse
            << L"\n  lifecycle-hwpx-fingerprint-recovery-matched="
            << lifecycleHwpxFingerprintRecoveryMatched
            << L"\n  partial-mutation=" << partialMutationRequest
            << L"\n  atomic-rollback=" << atomicRollbackRequest
            << L"\n  atomic-rollback-retry=" << atomicRollbackRetryRequest
            << L"\n  atomic-rollback-restored=" << atomicRollbackRestored
            << L"\n  atomic-rollback-failure=" << atomicRollbackFailureRequest
            << L"\n  atomic-rollback-failure-retained-tail="
            << atomicRollbackFailureRetainedTail
            << L"\n  empty-cell-text=" << emptyCellTextRequest
            << L"\n  empty-cell-text-fixture-ready=" << emptyCellTextFixtureReady
            << L"\n  empty-cell-text-succeeded=" << emptyCellTextSucceeded
            << L"\n  empty-cell-text-inserted-exactly="
            << emptyCellTextInsertedExactly
            << L"\n  multiline-cell-text-invoked="
            << multilineCellTextInvoked
            << L"\n  multiline-cell-text-passed="
            << multilineCellTextPassed
            << L"\n  all-target-cell-text-preflight="
            << allTargetCellTextPreflightResponse
            << L"\n  all-target-cell-text-preflight-passed="
            << allTargetCellTextPreflightPassed
            << L"\n  expand-cell-text-preflight="
            << expandCellTextPreflightResponse
            << L"\n  expand-cell-text-preflight-passed="
            << expandCellTextPreflightPassed
            << L"\n  strict-cell-selection-preflight="
            << strictCellSelectionPreflightResponse
            << L"\n  strict-cell-selection-preflight-passed="
            << strictCellSelectionPreflightPassed
            << L"\n  table-control-selection-preflight="
            << tableControlSelectionPreflightResponse
            << L"\n  table-control-selection-preflight-passed="
            << tableControlSelectionPreflightPassed
            << L"\n  selection-restore-failure="
            << selectionRestoreFailureResponse
            << L"\n  selection-restore-failure-passed="
            << selectionRestoreFailurePassed
            << L"\n  topology-reuse=" << topologyReuseResponse
            << L"\n  topology-reuse-passed=" << topologyReusePassed
            << L"\n  topology-formatting-preservation="
            << topologyFormattingPreservationResponse
            << L"\n  topology-formatting-preservation-passed="
            << topologyFormattingPreservationPassed
            << L"\n  topology-padding-preservation="
            << topologyPaddingPreservationResponse
            << L"\n  topology-padding-preservation-passed="
            << topologyPaddingPreservationPassed
            << L"\n  topology-cell-size-invalidation="
            << topologyCellSizeInvalidationResponse
            << L"\n  topology-cell-size-invalidation-passed="
            << topologyCellSizeInvalidationPassed
            << L"\n  topology-unknown-action-invalidation="
            << topologyUnknownActionInvalidationResponse
            << L"\n  topology-unknown-action-invalidation-passed="
            << topologyUnknownActionInvalidationPassed
            << L"\n  topology-invalidation=" << topologyInvalidationResponse
            << L"\n  topology-invalidation-passed=" << topologyInvalidationPassed
            << L"\n  topology-run-invalidation="
            << topologyRunInvalidationResponse
            << L"\n  topology-run-invalidation-passed="
            << topologyRunInvalidationPassed
            << L"\n  topology-action-invalidation="
            << topologyActionInvalidationResponse
            << L"\n  topology-action-invalidation-passed="
            << topologyActionInvalidationPassed
            << L"\n  topology-call-invalidation="
            << topologyCallInvalidationResponse
            << L"\n  topology-call-invalidation-passed="
            << topologyCallInvalidationPassed
            << L"\n  oversized-cell-patch=" << oversizedCellPatchResponse
            << L"\n  oversized-cell-patch-rejected="
            << oversizedCellPatchRejected
            << L"\n  reference-layout-success="
            << referenceLayoutSuccessRequest
            << L"\n  reference-layout-created-exactly="
            << referenceLayoutCreatedExactly
            << L"\n  reference-layout-border-readback-covered="
            << referenceLayoutBorderReadbackCovered
            << L"\n  reference-layout-atomic-rollback="
            << referenceLayoutAtomicRollbackRequest
            << L"\n  reference-layout-atomic-rollback-restored="
            << referenceLayoutAtomicRollbackRestored
            << L"\n  reference-layout-dropped-edges="
            << referenceLayoutDroppedEdgesRequest
            << L"\n  reference-layout-dropped-edges-rejected="
            << referenceLayoutDroppedEdgesRejected
            << L"\n  cell-topology-owner-index="
            << cellTopologyOwnerIndex
            << L"\n  table-cell-format-sampling="
            << tableCellFormatSampling
            << L"\n  graph-dispatch-abi="
            << graphDispatchAbi
            << L"\n  paragraph-text-normalization="
            << paragraphTextNormalization
            << L"\n  selected-control-patch=" << selectedControlPatchRequest
            << L"\n  selected-control-patch-rejected=" << selectedControlPatchRejected
            << L"\n  selected-control-patch-insertions=" << selectedControlPatchInsertions
            << L"\n  text-deletion-patch=" << textDeletionPatchRequest
            << L"\n  text-deletion-patch-verified=" << textDeletionPatchVerified
            << L"\n  scoped-structure=" << scopedStructure
            << L"\n  scoped-structure-succeeded=" << scopedStructureSucceeded
            << L"\n  unscoped-structure=" << unscopedStructure
            << L"\n  unscoped-structure-succeeded=" << unscopedStructureSucceeded
            << L"\n  snapshot=" << snapshot
            << L"\n  page=" << page
            << L"\n  page-v3=" << pageV3
            << L"\n  structure=" << structure
            << L"\n  bad-action=" << badActionRequest
            << L"\n  bad-array=" << badArrayIndexRequest
            << L"\n  valid-array=" << validArrayRequest
            << L"\n  call-return=" << callReturnRequest
            << L"\n  stale-selection=" << staleSelectionRequest
            << L"\n  replace-selection=" << replaceSelectionRequest
            << L"\n  bad-official-api=" << badOfficialApiRequest
            << L"\n  action-official-api=" << actionOfficialApiRequest
            << L"\n  validation-action-official-api="
            << validationActionOfficialApiRequest
            << L"\n  action-input-official-api=" << actionInputOfficialApiRequest
            << L"\n  fallback-action=" << fallbackActionOfficialApiRequest
            << L"\n  seh-action=" << sehActionOfficialApiRequest
            << L"\n  event=" << eventOfficialApiRequest
            << L"\n  fallback-parameter=" << fallbackParameterOfficialApiRequest
            << L"\n  normalized-parameter=" << normalizedParameterOfficialApiRequest
            << L"\n  alias=" << aliasOfficialApiRequest
            << L"\n  item-alias=" << itemAliasOfficialApiRequest
            << L"\n  message-box-owner=" << messageBoxOwnerOfficialApiRequest
            << L"\n  write-only-property=" << writeOnlyPropertyOfficialApiRequest
            << L"\n  picture=" << sizedPictureRequest
            << L"\n  caption=" << formattedCaptionRequest
            << L"\n  table=" << capturedTableRequest
            << L"\n  delete-control=" << deleteControlRequest
            << L"\n  invalid-paste-order=" << invalidPasteOrderRequest
            << L"\n  caption-table=" << captionTableRequest
            << L"\n  break-page-before-preflight=" << dispatch->RanBreakPage()
            << L"\n  native-block-copy=" << dispatch->UsedNativeTableBlockCopy()
            << L"\n  native-block-paste=" << dispatch->UsedNativeTableBlockPaste()
            << L"\n  clipboard-transfer=" << dispatch->UsedClipboardTableTransfer()
            << L"\n  source-anchor-format-before-paste="
            << dispatch->PasteUsedSourceAnchorFormat()
            << L"\n  attached-caption-number-preserved="
            << dispatch->PreservedAttachedCaptionNumber()
            << L"\n  streaming-cell-text=" << streamedCellText
            << L"\n  streaming-scan=" << dispatch->UsedStreamingTextScan()
            << L"\n  abandoned-checkpoint-smoke-files-removed="
            << abandonedCheckpointSmokeFilesRemoved
            << L"\n  checkpoint-document-user-fixture-written="
            << userDocumentFixtureWritten
            << L"\n  checkpoint-document-fixture-written="
            << checkpointDocumentFixtureWritten
            << L"\n  checkpoint-direct-restore-invoked="
            << checkpointDirectRestoreInvoked
            << L"\n  checkpoint-direct-restore-exact="
            << checkpointDirectRestoreExact
            << L"\n  checkpoint-direct-restore-response="
            << checkpointDirectRestoreRequest
            << L"\n  checkpoint-no-clear-restore-invoked="
            << checkpointNoClearRestoreInvoked
            << L"\n  checkpoint-no-clear-restore-exact="
            << checkpointNoClearRestoreExact
            << L"\n  checkpoint-no-clear-restore-response="
            << checkpointNoClearRestoreRequest
            << L"\n  checkpoint-no-clear-rollback-invoked="
            << checkpointNoClearRollbackInvoked
            << L"\n  checkpoint-no-clear-rollback-exact="
            << checkpointNoClearRollbackExact
            << L"\n  checkpoint-no-clear-rollback-response="
            << checkpointNoClearRollbackRequest
            << L"\n  checkpoint-direct-rollback-invoked="
            << checkpointDirectRollbackInvoked
            << L"\n  checkpoint-direct-rollback-exact="
            << checkpointDirectRollbackExact
            << L"\n  checkpoint-direct-rollback-response="
            << checkpointDirectRollbackRequest
            << L"\n  checkpoint-direct-rollback-semantic="
            << checkpointDirectRollbackSemanticExact
            << L"\n  checkpoint-direct-rollback-bytes="
            << checkpointDirectRollbackBytesExact
            << L"\n  checkpoint-direct-rollback-owned-clean="
            << checkpointDirectRollbackOwnedFilesClean
            << L"\n  checkpoint-direct-rollback-state="
            << checkpointDirectRollbackState
            << L"\n  checkpoint-direct-rollback-failure-invoked="
            << checkpointDirectRollbackFailureInvoked
            << L"\n  checkpoint-direct-rollback-failure-preserved="
            << checkpointDirectRollbackFailurePreserved
            << L"\n  checkpoint-direct-rollback-failure-response="
            << checkpointDirectRollbackFailureRequest
            << L"\n  checkpoint-direct-rollback-failure-state="
            << checkpointDirectRollbackFailureState
            << L"\n  checkpoint-direct-recovery-sidecar-absent="
            << checkpointDirectRecoverySidecarAbsent
            << L"\n  checkpoint-direct-recovery-cleaned="
            << checkpointDirectRecoveryCleaned
            << L"\n  checkpoint-unsigned-refused-untouched="
            << checkpointUnsignedRefusedUntouched
            << L"\n  checkpoint-unsigned-response=" << checkpointUnsignedRequest
            << L"\n  checkpoint-engine-undo-exact=" << checkpointEngineUndoExact
            << L"\n  checkpoint-engine-undo-response=" << checkpointEngineUndoRequest
            << L"\n  checkpoint-wrong-step-reversed=" << checkpointWrongStepReversed
            << L"\n  checkpoint-wrong-step-response=" << checkpointWrongStepRequest
            << L"\n  block-probe-reported=" << blockProbeReported
            << L"\n  block-probe-response=" << blockProbeRequest
            << L"\n  block-probe-empty-reported=" << blockProbeEmptyReported
            << L"\n  checkpoint-owned-files-deleted="
            << checkpointDocumentFixtureDeleted << L'\n';
        for (size_t index = 0;
             index < multilineCellTextResponses.size();
             ++index) {
            std::wcerr
                << L"  multiline-case=" << multilineCellTextCases[index].label
                << L" passed=" << multilineCellTextCasePassed[index]
                << L" response=" << multilineCellTextResponses[index] << L'\n';
        }
        if (batch != nullptr) {
            batch->Release();
        }
        static_cast<void>(releasePublication());
        FreeLibrary(library);
        CoUninitialize();
        return 8;
    }
    batch->Release();
    CComPtr<FakeDispatch> secondaryDocumentOwner;
    secondaryDocumentOwner.Attach(
        new FakeDispatch(primaryWindowHandle, secondaryDocumentId));
    FakeDispatch* const secondaryDocumentDispatch = secondaryDocumentOwner;
    const int secondaryDocumentPublished =
        module->DoAction(kOnLoad, secondaryDocumentDispatch);
    const bool secondaryDocumentPublicationDoesNotOwnHwp =
        secondaryDocumentDispatch->ReferenceCount() == 2;
    const std::wstring secondaryDocumentScope =
        primaryScope + L"." + std::to_wstring(secondaryDocumentId);
    IUnknown* const retainedPrimaryDocumentRaw = GetPublishedObject(
        L"HancomLiveBridge." + primaryDocumentScope);
    IUnknown* const retainedPrimaryDocumentBatch = GetPublishedObject(
        L"HancomLiveBatch." + primaryDocumentScope);
    IUnknown* const secondaryDocumentRaw = GetPublishedObject(
        L"HancomLiveBridge." + secondaryDocumentScope);
    IUnknown* const secondaryDocumentBatch = GetPublishedObject(
        L"HancomLiveBatch." + secondaryDocumentScope);
    const bool documentRegistryWorked =
        secondaryDocumentPublished != FALSE &&
        secondaryDocumentPublicationDoesNotOwnHwp &&
        retainedPrimaryDocumentRaw != nullptr &&
        retainedPrimaryDocumentBatch != nullptr &&
        secondaryDocumentRaw != nullptr &&
        secondaryDocumentBatch != nullptr;
    if (retainedPrimaryDocumentRaw != nullptr) {
        retainedPrimaryDocumentRaw->Release();
    }
    if (retainedPrimaryDocumentBatch != nullptr) {
        retainedPrimaryDocumentBatch->Release();
    }
    if (secondaryDocumentRaw != nullptr) {
        secondaryDocumentRaw->Release();
    }
    if (secondaryDocumentBatch != nullptr) {
        secondaryDocumentBatch->Release();
    }
    if (!documentRegistryWorked) {
        std::wcerr << L"document-scoped ROT registry failed\n";
        static_cast<void>(releasePublication());
        FreeLibrary(library);
        CoUninitialize();
        return 20;
    }
    CComPtr<FakeDispatch> secondaryOwner;
    secondaryOwner.Attach(
        new FakeDispatch(secondaryWindowHandle, otherWindowDocumentId));
    FakeDispatch* const secondaryDispatch = secondaryOwner;
    const int secondaryPublished = module->DoAction(kOnLoad, secondaryDispatch);
    const bool secondaryPublicationDoesNotOwnHwp =
        secondaryDispatch->ReferenceCount() == 2;
    const std::wstring secondaryScope =
        processId + L"." + std::to_wstring(secondaryWindowHandle);
    const std::wstring otherWindowDocumentScope =
        secondaryScope + L"." + std::to_wstring(otherWindowDocumentId);
    IUnknown* const retainedPrimaryRaw = GetPublishedObject(
        L"HancomLiveBridge." + primaryScope);
    IUnknown* const retainedPrimaryBatch = GetPublishedObject(
        L"HancomLiveBatch." + primaryScope);
    IUnknown* const secondaryRaw = GetPublishedObject(
        L"HancomLiveBridge." + secondaryScope);
    IUnknown* const secondaryBatch = GetPublishedObject(
        L"HancomLiveBatch." + secondaryScope);
    IUnknown* const otherWindowDocumentRaw = GetPublishedObject(
        L"HancomLiveBridge." + otherWindowDocumentScope);
    IUnknown* const otherWindowDocumentBatch = GetPublishedObject(
        L"HancomLiveBatch." + otherWindowDocumentScope);
    const bool windowRegistryWorked =
        secondaryPublished != FALSE &&
        secondaryPublicationDoesNotOwnHwp &&
        retainedPrimaryRaw != nullptr &&
        retainedPrimaryBatch != nullptr &&
        secondaryRaw != nullptr &&
        secondaryBatch != nullptr &&
        otherWindowDocumentRaw != nullptr &&
        otherWindowDocumentBatch != nullptr;
    if (retainedPrimaryRaw != nullptr) {
        retainedPrimaryRaw->Release();
    }
    if (retainedPrimaryBatch != nullptr) {
        retainedPrimaryBatch->Release();
    }
    if (secondaryRaw != nullptr) {
        secondaryRaw->Release();
    }
    if (secondaryBatch != nullptr) {
        secondaryBatch->Release();
    }
    if (otherWindowDocumentRaw != nullptr) {
        otherWindowDocumentRaw->Release();
    }
    if (otherWindowDocumentBatch != nullptr) {
        otherWindowDocumentBatch->Release();
    }
    if (!windowRegistryWorked) {
        std::wcerr << L"window-scoped ROT registry failed\n";
        static_cast<void>(releasePublication());
        FreeLibrary(library);
        CoUninitialize();
        return 21;
    }
    if (argumentCount == 3 && std::wcscmp(arguments[2], L"--hold") == 0) {
        std::wcout << L"READY " << GetCurrentProcessId() << L'\n' << std::flush;
        const ULONGLONG deadline = GetTickCount64() + 15'000;
        while (GetTickCount64() < deadline) {
            static_cast<void>(MsgWaitForMultipleObjects(
                0,
                nullptr,
                FALSE,
                50,
                QS_ALLINPUT));
            MSG message{};
            while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    }
    const HRESULT revokeStatus = releasePublication();
    const bool revokePreservedHostOwnership =
        dispatch->ReferenceCount() == 1 &&
        secondaryDocumentDispatch->ReferenceCount() == 1 &&
        secondaryDispatch->ReferenceCount() == 1;
    if (FAILED(revokeStatus) ||
        !revokePreservedHostOwnership ||
        GetPublishedObject(L"HancomLiveBridge." + processId) != nullptr ||
        GetPublishedObject(L"HancomLiveBatch." + processId) != nullptr ||
        GetPublishedObject(L"HancomLiveBridge." + primaryScope) != nullptr ||
        GetPublishedObject(L"HancomLiveBatch." + primaryScope) != nullptr ||
        GetPublishedObject(
            L"HancomLiveBridge." + primaryDocumentScope) != nullptr ||
        GetPublishedObject(
            L"HancomLiveBatch." + primaryDocumentScope) != nullptr ||
        GetPublishedObject(
            L"HancomLiveBridge." + secondaryDocumentScope) != nullptr ||
        GetPublishedObject(
            L"HancomLiveBatch." + secondaryDocumentScope) != nullptr ||
        GetPublishedObject(L"HancomLiveBridge." + secondaryScope) != nullptr ||
        GetPublishedObject(L"HancomLiveBatch." + secondaryScope) != nullptr ||
        GetPublishedObject(
            L"HancomLiveBridge." + otherWindowDocumentScope) != nullptr ||
        GetPublishedObject(
            L"HancomLiveBatch." + otherWindowDocumentScope) != nullptr) {
        std::wcerr << L"ROT revoke failed\n";
        FreeLibrary(library);
        CoUninitialize();
        return 9;
    }

    for (size_t index = 0;
         index < multilineCellTextResponses.size();
         ++index) {
        std::wcout
            << L"MULTILINE_CELL_TEXT " << multilineCellTextCases[index].label
            << L" passed=" << multilineCellTextCasePassed[index]
            << L" response=" << multilineCellTextResponses[index] << L'\n';
    }
    std::wcout
               << L"ALL_TARGET_CELL_TEXT_PREFLIGHT "
               << allTargetCellTextPreflightResponse << L'\n'
               << L"STRICT_CELL_SELECTION_PREFLIGHT "
               << strictCellSelectionPreflightResponse << L'\n'
               << L"TABLE_CONTROL_SELECTION_PREFLIGHT "
               << tableControlSelectionPreflightResponse << L'\n'
               << L"SELECTION_RESTORE_FAILURE "
               << selectionRestoreFailureResponse << L'\n'
               << L"TABLE_TOPOLOGY_REUSE " << topologyReusePassed << L'\n'
               << L"TABLE_TOPOLOGY_FORMATTING_PRESERVATION "
               << topologyFormattingPreservationPassed << L'\n'
               << L"TABLE_TOPOLOGY_PADDING_PRESERVATION "
               << topologyPaddingPreservationPassed << L'\n'
               << L"TABLE_TOPOLOGY_CELL_SIZE_INVALIDATION "
               << topologyCellSizeInvalidationPassed << L'\n'
               << L"TABLE_TOPOLOGY_UNKNOWN_ACTION_INVALIDATION "
               << topologyUnknownActionInvalidationPassed << L'\n'
               << L"TABLE_TOPOLOGY_INVALIDATION "
               << topologyInvalidationPassed << L'\n'
               << L"TABLE_TOPOLOGY_RUN_INVALIDATION "
               << topologyRunInvalidationPassed << L'\n'
               << L"TABLE_TOPOLOGY_ACTION_INVALIDATION "
               << topologyActionInvalidationPassed << L'\n'
               << L"TABLE_TOPOLOGY_CALL_INVALIDATION "
               << topologyCallInvalidationPassed << L'\n'
               << L"OVERSIZED_CELL_PATCH_REJECTED "
               << oversizedCellPatchRejected << L'\n'
               << L"DOCUMENT_SECTION_DIAGNOSTICS "
               << saveVerifySectionDiagnosticsMatched
               << L" normalized_wchars=8388636"
               << L" iterations=" << kSectionDiagnosticsIterations
               << L" total_us="
               << saveVerifySectionDiagnosticsElapsedMicroseconds
               << L" average_us="
               << (saveVerifySectionDiagnosticsElapsedMicroseconds /
                   kSectionDiagnosticsIterations)
               << L'\n'
               << L"LIFECYCLE_SUCCESS " << lifecycleSuccessResponse << L'\n'
               << L"LIFECYCLE_CLEAN_NOOP " << lifecycleCleanNoOpResponse << L'\n'
               << L"LIFECYCLE_SAVE_GATE " << lifecycleSaveGateResponse << L'\n'
               << L"LIFECYCLE_SAVE_RETURN_GATE " << lifecycleSaveReturnGateResponse << L'\n'
               << L"LIFECYCLE_RECOVERY_CAPTURE_GATE "
               << lifecycleRecoveryCaptureGateResponse << L'\n'
               << L"LIFECYCLE_RECOVERY " << lifecycleRecoveryResponse << L'\n'
               << L"LIFECYCLE_HWPX_SUCCESS " << lifecycleHwpxSuccessResponse << L'\n'
               << L"LIFECYCLE_HWPX_RECOVERY " << lifecycleHwpxRecoveryResponse << L'\n'
               << L"LIFECYCLE_FINGERPRINT_RECOVERY "
               << lifecycleFingerprintRecoveryResponse << L'\n'
               << L"LIFECYCLE_HWPX_FINGERPRINT_RECOVERY "
               << lifecycleHwpxFingerprintRecoveryResponse << L'\n'
               << L"ATOMIC_ROLLBACK " << atomicRollbackRequest << L'\n'
               << L"ATOMIC_ROLLBACK_RETRY " << atomicRollbackRetryRequest << L'\n'
               << L"ATOMIC_ROLLBACK_TAIL_RESTORED " << atomicRollbackRestored << L'\n'
               << L"ATOMIC_ROLLBACK_FAILURE " << atomicRollbackFailureRequest << L'\n'
               << L"ATOMIC_ROLLBACK_FAILURE_TAIL_RETAINED "
               << atomicRollbackFailureRetainedTail << L'\n'
               << L"CHECKPOINT_RESTORE " << checkpointRestoreRequest << L'\n'
               << L"CHECKPOINT_ROLLBACK " << checkpointRollbackRequest << L'\n'
               << L"CHECKPOINT_ROLLBACK_RESTORED "
               << checkpointRollbackSucceeded << L'\n'
               << L"ABANDONED_CHECKPOINT_SMOKE_FILES_REMOVED "
               << abandonedCheckpointSmokeFilesRemoved << L'\n'
               << L"CHECKPOINT_DOCUMENT_DIRECT_REOPEN "
               << checkpointDirectRestoreRequest << L'\n'
               << L"CHECKPOINT_DOCUMENT_DIRECT_REOPEN_EXACT "
               << checkpointDirectRestoreExact << L'\n'
               << L"CHECKPOINT_DOCUMENT_DIRECT_ROLLBACK "
               << checkpointDirectRollbackRequest << L'\n'
               << L"CHECKPOINT_DOCUMENT_DIRECT_ROLLBACK_EXACT "
               << checkpointDirectRollbackExact << L'\n'
               << L"CHECKPOINT_DOCUMENT_ROLLBACK_FAILURE "
               << checkpointDirectRollbackFailureRequest << L'\n'
               << L"CHECKPOINT_DOCUMENT_ROLLBACK_COPY_KEPT "
               << checkpointDirectRollbackFailurePreserved << L'\n'
               << L"CHECKPOINT_DOCUMENT_ENGINE_UNDO_EXACT "
               << checkpointEngineUndoExact << L'\n'
               << L"CHECKPOINT_DOCUMENT_WRONG_STEP_REVERSED "
               << checkpointWrongStepReversed << L'\n'
               << L"CHECKPOINT_DOCUMENT_OWNED_FILES_DELETED "
               << checkpointDocumentFixtureDeleted << L'\n'
               << L"EMPTY_CELL_TEXT " << emptyCellTextRequest << L'\n'
               << L"EMPTY_CELL_TEXT_POSITION actual=650:0:0 stale=0:0:0\n"
               << L"EMPTY_CELL_TEXT_INSERTED_EXACTLY "
               << emptyCellTextInsertedExactly << L'\n'
               << L"REFERENCE_LAYOUT_SUCCESS "
               << referenceLayoutSuccessRequest << L'\n'
               << L"REFERENCE_LAYOUT_CREATED_EXACTLY "
               << referenceLayoutCreatedExactly << L'\n'
               << L"REFERENCE_LAYOUT_BORDER_READBACK_COVERED "
               << referenceLayoutBorderReadbackCovered << L'\n'
               << L"REFERENCE_LAYOUT_ATOMIC_ROLLBACK "
               << referenceLayoutAtomicRollbackRequest << L'\n'
               << L"REFERENCE_LAYOUT_ATOMIC_ROLLBACK_TAIL_RESTORED "
               << referenceLayoutAtomicRollbackRestored << L'\n'
               << L"REFERENCE_LAYOUT_DROPPED_EDGES "
               << referenceLayoutDroppedEdgesRequest << L'\n'
               << L"DOCUMENT_GRAPH_SCHEMA " << documentGraphSchema << L'\n'
               << L"CELL_TOPOLOGY_OWNER_INDEX "
               << cellTopologyOwnerIndex << L'\n'
               << L"TABLE_CELL_FORMAT_SAMPLING "
               << tableCellFormatSampling << L'\n'
               << L"SELECTED_CONTROL_PATCH " << selectedControlPatchRequest << L'\n'
               << L"TEXT_DELETION_PATCH " << textDeletionPatchRequest << L'\n'
               << L"SCOPED_STRUCTURE " << scopedStructureSucceeded << L'\n'
               << L"UNSCOPED_STRUCTURE " << unscopedStructureSucceeded << L'\n'
               << L"PASS UserAction ABI, inspection, lifecycle, action batch, "
                  L"window/document-scoped ROT publication, and revoke\n";
    FreeLibrary(library);
    CoUninitialize();
    return 0;
}
