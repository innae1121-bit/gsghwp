#include "DocumentGraphCodec.h"
#include "DocumentGraphCodecInternal.h"
#include "DocumentGraphLayout.h"
#include "DocumentGraphProperties.h"
#include <Windows.h>
#include <algorithm>
#include <atomic>
#include <bcrypt.h>
#include <cmath>
#include <cstring>
#include <functional>
#include <limits>
#include <map>
#include <tuple>
#include <unordered_map>

namespace hancom::graph::codec {
namespace {
void U8(Bytes *o, std::uint8_t v) { o->push_back(v); }
void U16(Bytes *o, std::uint16_t v) {
  U8(o, static_cast<std::uint8_t>(v));
  U8(o, static_cast<std::uint8_t>(v >> 8));
}
void U32(Bytes *o, std::uint32_t v) {
  for (unsigned s = 0; s < 32; s += 8)
    U8(o, static_cast<std::uint8_t>(v >> s));
}
void U64(Bytes *o, std::uint64_t v) {
  for (unsigned s = 0; s < 64; s += 8)
    U8(o, static_cast<std::uint8_t>(v >> s));
}
void I32(Bytes *o, std::int32_t v) { U32(o, static_cast<std::uint32_t>(v)); }
void Add(Bytes *o, ByteView v) {
  if (v.size)
    o->insert(o->end(), v.data, v.data + static_cast<std::size_t>(v.size));
}
void Add(Bytes *o, const Bytes &v) { Add(o, View(v)); }
std::uint16_t R16(const std::uint8_t *p) {
  return static_cast<std::uint16_t>(p[0] |
                                    static_cast<std::uint16_t>(p[1] << 8));
}
std::uint32_t R32(const std::uint8_t *p) {
  std::uint32_t v = 0;
  for (unsigned i = 0; i < 4; ++i)
    v |= static_cast<std::uint32_t>(p[i]) << (i * 8);
  return v;
}
std::uint64_t R64(const std::uint8_t *p) {
  std::uint64_t v = 0;
  for (unsigned i = 0; i < 8; ++i)
    v |= static_cast<std::uint64_t>(p[i]) << (i * 8);
  return v;
}
bool Fits(std::uint64_t p, std::uint64_t n, std::uint64_t z) {
  return p <= z && n <= z - p;
}
void Frame(Bytes *o, const Bytes &v) {
  U64(o, static_cast<std::uint64_t>(v.size()));
  Add(o, v);
}
Bytes Item(Bytes b) {
  Bytes o;
  Frame(&o, b);
  return o;
}
Bytes Seq(std::vector<Bytes> v, bool sort) {
  if (sort)
    std::sort(v.begin(), v.end());
  Bytes o;
  U64(&o, static_cast<std::uint64_t>(v.size()));
  for (const auto &i : v)
    Frame(&o, i);
  return o;
}
Bytes PackedItems(const std::vector<Bytes> &items) {
  Bytes out;
  U64(&out, static_cast<std::uint64_t>(items.size()));
  for (const auto &item : items)
    Add(&out, item);
  return out;
}
bool PathLess(const CanonicalPath &a, const CanonicalPath &b) {
  const std::size_t common = (std::min)(a.size(), b.size());
  for (std::size_t index = 0; index < common; ++index) {
    if (a[index].kind != b[index].kind)
      return static_cast<std::uint16_t>(a[index].kind) <
             static_cast<std::uint16_t>(b[index].kind);
    if (a[index].siblingOrdinal != b[index].siblingOrdinal)
      return a[index].siblingOrdinal < b[index].siblingOrdinal;
  }
  return a.size() < b.size();
}
bool ReferenceLess(const ReferenceInput &a, const ReferenceInput &b) {
  if (a.edge != b.edge)
    return static_cast<std::uint16_t>(a.edge) <
           static_cast<std::uint16_t>(b.edge);
  if (a.targetKind != b.targetKind)
    return static_cast<std::uint16_t>(a.targetKind) <
           static_cast<std::uint16_t>(b.targetKind);
  if (a.ordinalTarget != b.ordinalTarget)
    return a.ordinalTarget < b.ordinalTarget;
  if (a.ordinalTarget)
    return a.targetOrdinal < b.targetOrdinal;
  return PathLess(a.targetPath, b.targetPath);
}
bool Known(FieldTag t, const FieldTag *k, std::size_t n) {
  for (std::size_t i = 0; i < n; ++i)
    if (k[i] == t)
      return true;
  return false;
}
Error CheckArrayView(const ScalarTag scalar, const std::uint64_t elementCount,
                     const ByteView value) {
  if (value.size < 16)
    return Error::BadArray;
  const auto *p = value.data;
  const auto raw = R16(p);
  if (raw > 17 || raw != static_cast<std::uint16_t>(scalar) ||
      R16(p + 2) != 0 || R32(p + 4) != 0 || R64(p + 8) != elementCount)
    return Error::BadArray;
  std::uint64_t at = 16;
  for (std::uint64_t i = 0; i < elementCount; ++i) {
    if (!Fits(at, 8, value.size))
      return Error::BadArray;
    const auto n = R64(p + at);
    at += 8;
    if (!Fits(at, n, value.size))
      return Error::BadArray;
    const auto e =
        ValidateCanonicalScalar(static_cast<ScalarTag>(raw), {p + at, n});
    if (e != Error::None)
      return e;
    at += n;
  }
  return at == value.size ? Error::None : Error::BadArray;
}
Error CheckArray(const Field &f) {
  return CheckArrayView(f.scalar, f.elementCount, View(f.value));
}
Error ValidateFieldView(const std::uint16_t flags, const ScalarTag scalar,
                        const std::uint64_t elementCount,
                        const ByteView value) {
  if ((flags & kFieldFlagArray) != 0)
    return CheckArrayView(scalar, elementCount, value);
  if (elementCount != 1)
    return Error::BadArray;
  const Error scalarError = ValidateCanonicalScalar(scalar, value);
  if (scalarError == Error::None)
    return Error::None;
  if (value.size >= kObservationEnvelopeFixedBytesV1 &&
      ValidateObservation(value, scalar) == Error::None)
    return Error::None;
  return scalarError;
}
} // namespace
ByteView View(const Bytes &b) noexcept {
  return {b.data(), static_cast<std::uint64_t>(b.size())};
}
Bytes Uint64(std::uint64_t v) {
  Bytes o;
  U64(&o, v);
  return o;
}
Bytes Sint64(std::int64_t v) { return Uint64(static_cast<std::uint64_t>(v)); }
Bytes Bool(bool v) { return {static_cast<std::uint8_t>(v ? 1 : 0)}; }
Bytes Uint32(std::uint32_t v) {
  Bytes o;
  U32(&o, v);
  return o;
}
Bytes Sint32(std::int32_t v) { return Uint32(static_cast<std::uint32_t>(v)); }
Bytes Uint16(std::uint16_t v) {
  Bytes o;
  U16(&o, v);
  return o;
}
Bytes Uint8(std::uint8_t v) { return {v}; }
Bytes Float64(double v, Error *e) {
  if (!std::isfinite(v)) {
    if (e)
      *e = Error::NonFiniteFloat;
    return {};
  }
  if (v == 0.0)
    v = 0.0;
  std::uint64_t bits = 0;
  std::memcpy(&bits, &v, 8);
  if (e)
    *e = Error::None;
  return Uint64(bits);
}
Bytes Utf16(const std::uint16_t *p, std::uint64_t n) {
  Bytes o;
  U64(&o, n);
  for (std::uint64_t i = 0; i < n; ++i)
    U16(&o, p[i]);
  return o;
}
Bytes BytesValue(ByteView v) {
  Bytes o;
  U64(&o, v.size);
  Add(&o, v);
  return o;
}
Bytes Enum(std::int64_t raw, const std::uint16_t *s, std::uint64_t n) {
  Bytes o = Sint64(raw);
  U8(&o, s ? 1U : 0U);
  for (unsigned i = 0; i < 7; ++i)
    U8(&o, 0);
  if (s)
    Add(&o, Utf16(s, n));
  return o;
}
Bytes BlobSliceValue(const ContentId &id, std::uint64_t off, std::uint64_t len,
                     const Sha256 &d) {
  Bytes o(id.bytes.begin(), id.bytes.end());
  U64(&o, off);
  U64(&o, len);
  o.insert(o.end(), d.bytes.begin(), d.bytes.end());
  return o;
}
Bytes ArrayValue(ScalarTag t, std::uint16_t flags,
                 const std::vector<Bytes> &v) {
  Bytes o;
  U16(&o, static_cast<std::uint16_t>(t));
  U16(&o, flags);
  U32(&o, 0);
  U64(&o, static_cast<std::uint64_t>(v.size()));
  for (const auto &i : v)
    Frame(&o, i);
  return o;
}
Bytes Observation(ObservationState s, std::int32_t hr, const std::uint16_t *d,
                  std::uint64_t dn, ByteView value, Error *e) {
  const bool p = s == ObservationState::Value;
  if (static_cast<std::uint8_t>(s) >
          static_cast<std::uint8_t>(ObservationState::ProjectionOmitted) ||
      (dn != 0 && d == nullptr) || (p && hr != 0) || (!p && value.size)) {
    if (e)
      *e = Error::BadObservation;
    return {};
  }
  Bytes o;
  U8(&o, static_cast<std::uint8_t>(s));
  U8(&o, p ? 1U : 0U);
  U16(&o, 0);
  I32(&o, hr);
  U64(&o, dn);
  U64(&o, p ? value.size : 0);
  for (std::uint64_t i = 0; i < dn; ++i)
    U16(&o, d[i]);
  if (p)
    Add(&o, value);
  if (e)
    *e = Error::None;
  return o;
}
Error ValidateCanonicalScalar(ScalarTag t, ByteView v) noexcept {
  const auto fixed = FixedScalarBytes(t);
  if (fixed) {
    if (v.size != fixed)
      return Error::BadScalar;
    if (t == ScalarTag::Bool && v.data[0] > 1)
      return Error::BadScalar;
    if (t == ScalarTag::Float64) {
      double d = 0;
      std::memcpy(&d, v.data, 8);
      if (!std::isfinite(d))
        return Error::NonFiniteFloat;
      if (d == 0.0 && R64(v.data) != 0)
        return Error::BadScalar;
    }
    return Error::None;
  }
  if (t == ScalarTag::UTF16 || t == ScalarTag::Bytes) {
    if (v.size < 8)
      return Error::BadScalar;
    const auto n = R64(v.data);
    const std::uint64_t m = t == ScalarTag::UTF16 ? 2U : 1U;
    if (n > (std::numeric_limits<std::uint64_t>::max)() / m ||
        n * m != v.size - 8)
      return Error::BadScalar;
    return Error::None;
  }
  if (t == ScalarTag::Enum) {
    if (v.size < 16 || v.data[8] > 1)
      return Error::BadScalar;
    for (std::uint64_t i = 9; i < 16; ++i)
      if (v.data[i])
        return Error::ReservedNonzero;
    if (!v.data[8])
      return v.size == 16 ? Error::None : Error::BadScalar;
    return ValidateCanonicalScalar(ScalarTag::UTF16,
                                   {v.data + 16, v.size - 16});
  }
  if (t == ScalarTag::BlobSlice)
    return v.size == 64 ? Error::None : Error::BadScalar;
  if (t == ScalarTag::Struct)
    return Error::None;
  return Error::BadScalar;
}
Error ValidateObservation(ByteView v, ScalarTag scalar) noexcept {
  if (v.size < 24)
    return Error::BadObservation;
  const auto state = v.data[0], present = v.data[1];
  if (state > 6 || present > 1 || R16(v.data + 2) != 0)
    return Error::BadObservation;
  const auto detail = R64(v.data + 8), value = R64(v.data + 16);
  if (detail > (std::numeric_limits<std::uint64_t>::max)() / 2 ||
      !Fits(24, detail * 2, v.size) || !Fits(24 + detail * 2, value, v.size) ||
      24 + detail * 2 + value != v.size)
    return Error::BadObservation;
  const auto hr = R32(v.data + 4);
  if (state == 0) {
    if (present != 1 || hr != 0)
      return Error::BadObservation;
    return ValidateCanonicalScalar(scalar, {v.data + 24 + detail * 2, value});
  }
  return present == 0 && value == 0 ? Error::None : Error::BadObservation;
}
namespace {
Error ValidateFieldBytes(const Field &field) noexcept {
  if ((field.flags & kFieldFlagArray) != 0)
    return CheckArray(field);
  if (field.elementCount != 1)
    return Error::BadArray;
  const Error scalar = ValidateCanonicalScalar(field.scalar, View(field.value));
  if (scalar == Error::None)
    return Error::None;
  if (field.value.size() >= kObservationEnvelopeFixedBytesV1 &&
      ValidateObservation(View(field.value), field.scalar) == Error::None)
    return Error::None;
  return scalar;
}
} // namespace
Error EncodeField(const Field &f, Bytes *o) noexcept {
  if (!o || !f.tag || (f.flags & ~3U))
    return Error::BadFlags;
  if (f.elementCount != 1 && (f.flags & kFieldFlagArray) == 0)
    return Error::BadArray;
  const auto e = ValidateFieldBytes(f);
  if (e != Error::None)
    return e;
  U16(o, f.tag);
  U16(o, f.flags);
  U16(o, static_cast<std::uint16_t>(f.scalar));
  U16(o, 0);
  U64(o, f.elementCount);
  U64(o, static_cast<std::uint64_t>(f.value.size()));
  Add(o, f.value);
  return Error::None;
}
Error EncodeLogicalRecord(const LogicalRecord &r, Bytes *o) noexcept {
  if (!o || (r.flags & ~kFieldFlagRequired) || r.fields.size() > UINT16_MAX)
    return Error::BadFlags;
  Bytes p;
  FieldTag prior = 0;
  for (std::size_t i = 0; i < r.fields.size(); ++i) {
    if (i && r.fields[i].tag == prior)
      return Error::DuplicateField;
    if (i && r.fields[i].tag < prior)
      return Error::OutOfOrderField;
    const auto e = EncodeField(r.fields[i], &p);
    if (e != Error::None)
      return e;
    prior = r.fields[i].tag;
  }
  U16(o, static_cast<std::uint16_t>(r.kind));
  U16(o, 1);
  U16(o, r.flags);
  U16(o, static_cast<std::uint16_t>(r.fields.size()));
  U64(o, r.id);
  U64(o, static_cast<std::uint64_t>(p.size()));
  Add(o, p);
  return Error::None;
}
Error ValidateLogicalRecord(ByteView b, const FieldTag *k, std::size_t kn,
                            RecordId id, bool exact) noexcept {
  if (b.size < 24)
    return Error::Truncated;
  if (R16(b.data) > 8)
    return Error::BadFlags;
  if (R16(b.data + 2) != 1)
    return Error::BadRecordVersion;
  if (R16(b.data + 4) != kFieldFlagRequired)
    return Error::BadFlags;
  if (exact && R64(b.data + 8) != id)
    return Error::RecordIdMismatch;
  if (R64(b.data + 16) != b.size - 24)
    return Error::Truncated;
  const auto count = R16(b.data + 6);
  std::uint64_t at = 24;
  FieldTag prior = 0;
  for (std::uint16_t i = 0; i < count; ++i) {
    if (!Fits(at, 24, b.size))
      return Error::Truncated;
    const auto *h = b.data + at;
    const auto t = R16(h);
    const auto flags = R16(h + 2);
    const auto raw = R16(h + 4);
    if (!t)
      return Error::BadFlags;
    if (R16(h + 6))
      return Error::ReservedNonzero;
    if (flags & ~3U)
      return Error::BadFlags;
    if (i && t == prior)
      return Error::DuplicateField;
    if (i && t < prior)
      return Error::OutOfOrderField;
    if (!Known(t, k, kn) && (flags & 1U))
      return Error::UnknownRequiredField;
    if (raw > 17)
      return Error::BadScalar;
    const auto n = R64(h + 16);
    at += 24;
    if (!Fits(at, n, b.size))
      return Error::Truncated;
    const auto elementCount = R64(h + 8);
    if ((flags & kFieldFlagArray) == 0 && elementCount != 1)
      return Error::BadArray;
    const auto e = ValidateFieldView(
        flags, static_cast<ScalarTag>(raw), elementCount,
        {b.data + at, n});
    if (e != Error::None)
      return e;
    at += n;
    prior = t;
  }
  return at == b.size ? Error::None : Error::Truncated;
}
namespace {
struct SharedSha256Provider final {
  INIT_ONCE once = INIT_ONCE_STATIC_INIT;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  DWORD objectBytes = 0;
};
SharedSha256Provider gSha256Provider{};

BOOL CALLBACK InitializeSha256Provider(PINIT_ONCE, PVOID parameter,
                                       PVOID*) noexcept {
  auto* const provider = static_cast<SharedSha256Provider*>(parameter);
  DWORD received = 0;
  if (BCryptOpenAlgorithmProvider(
          &provider->algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0 ||
      BCryptGetProperty(
          provider->algorithm, BCRYPT_OBJECT_LENGTH,
          reinterpret_cast<PUCHAR>(&provider->objectBytes),
          sizeof(provider->objectBytes), &received, 0) < 0) {
    if (provider->algorithm != nullptr)
      BCryptCloseAlgorithmProvider(provider->algorithm, 0);
    provider->algorithm = nullptr;
    provider->objectBytes = 0;
  }
  return TRUE;
}

Sha256 HashParts(const ByteView first, const ByteView second) noexcept {
  Sha256 digest{};
  if (!InitOnceExecuteOnce(
          &gSha256Provider.once, InitializeSha256Provider,
          &gSha256Provider, nullptr) ||
      gSha256Provider.algorithm == nullptr ||
      gSha256Provider.objectBytes == 0)
    return digest;
  try {
    thread_local Bytes object;
    object.resize(gSha256Provider.objectBytes);
    BCRYPT_HASH_HANDLE hash = nullptr;
    if (BCryptCreateHash(
            gSha256Provider.algorithm, &hash, object.data(),
            gSha256Provider.objectBytes, nullptr, 0, 0) < 0)
      return digest;
    NTSTATUS status = 0;
    for (const ByteView part : {first, second}) {
      std::uint64_t at = 0;
      while (at < part.size && status >= 0) {
        const ULONG chunk = static_cast<ULONG>(
            (std::min<std::uint64_t>)(part.size - at, MAXDWORD));
        status = BCryptHashData(
            hash,
            const_cast<PUCHAR>(part.data + static_cast<std::size_t>(at)),
            chunk, 0);
        at += chunk;
      }
    }
    if (status >= 0)
      status = BCryptFinishHash(
          hash, digest.bytes.data(),
          static_cast<ULONG>(digest.bytes.size()), 0);
    BCryptDestroyHash(hash);
    if (status < 0) digest = {};
    return digest;
  } catch (...) {
    return {};
  }
}
} // namespace

Sha256 Hash(ByteView b) noexcept { return HashParts({}, b); }
Sha256 DomainHash(ByteView domain, ByteView b) noexcept {
  return HashParts(domain, b);
}
bool Equal(const Sha256 &a, const Sha256 &b) noexcept {
  return a.bytes == b.bytes;
}
Bytes EncodePath(const CanonicalPath &p) {
  Bytes o;
  U64(&o, static_cast<std::uint64_t>(p.size()));
  for (const auto &s : p) {
    U16(&o, static_cast<std::uint16_t>(s.kind));
    U64(&o, s.siblingOrdinal);
  }
  return o;
}
Bytes SemanticPropertyBytes(const SemanticPropertyInput &p) {
  Bytes b;
  U32(&b, p.key);
  U8(&b, static_cast<std::uint8_t>(p.origin));
  U8(&b, 0);
  U8(&b, 0);
  U8(&b, 0);
  U64(&b, static_cast<std::uint64_t>(p.canonicalValue.size()));
  Add(&b, p.canonicalValue);
  return Item(b);
}
Sha256 SemanticPropertyDigest(const SemanticPropertyInput &p) {
  const auto b = SemanticPropertyBytes(p);
  return DomainHash("HWPGRAPH\0PROPERTY\0SEMANTIC\0V1", View(b));
}
Sha256 DefinitionPropertyAggregate(std::vector<SemanticPropertyInput> p) {
  std::sort(p.begin(), p.end(),
            [](const auto &a, const auto &b) { return a.key < b.key; });
  Bytes v;
  U64(&v, static_cast<std::uint64_t>(p.size()));
  for (const auto &i : p) {
    const auto d = SemanticPropertyDigest(i);
    U64(&v, 32);
    v.insert(v.end(), d.bytes.begin(), d.bytes.end());
  }
  return DomainHash("HWPGRAPH\0DEFINITIONPROPERTIES\0V1", View(v));
}
Bytes SemanticPayloadElement(const SemanticPayloadInput &p) {
  Bytes b;
  U16(&b, p.fieldTag);
  U16(&b, static_cast<std::uint16_t>(p.scalar));
  U8(&b, 0);
  U8(&b, 1);
  U16(&b, 0);
  U64(&b, static_cast<std::uint64_t>(p.transformedValue.size()));
  Add(&b, p.transformedValue);
  return Item(b);
}
Bytes ReferenceDescriptor(const ReferenceInput &r) {
  Bytes b;
  U16(&b, static_cast<std::uint16_t>(r.edge));
  U16(&b, static_cast<std::uint16_t>(r.targetKind));
  if (r.ordinalTarget)
    U64(&b, r.targetOrdinal);
  else
    Add(&b, EncodePath(r.targetPath));
  return b;
}
Sha256 DefinitionFingerprint(DefinitionKind k, const SemanticPayloadInput *n,
                             const Sha256 &a, std::vector<ReferenceInput> r) {
  std::sort(r.begin(), r.end(), ReferenceLess);
  std::vector<Bytes> refs;
  for (const auto &i : r)
    refs.push_back(ReferenceDescriptor(i));
  Bytes v;
  U16(&v, static_cast<std::uint16_t>(k));
  if (n)
    Add(&v, SemanticPayloadElement(*n));
  v.insert(v.end(), a.bytes.begin(), a.bytes.end());
  Add(&v, Seq(refs, false));
  return DomainHash("HWPGRAPH\0DEFINITION\0V1", View(v));
}
Sha256 BinaryFingerprint(std::uint64_t n, const Sha256 &d) {
  Bytes v;
  U64(&v, n);
  v.insert(v.end(), d.bytes.begin(), d.bytes.end());
  return DomainHash("HWPGRAPH\0BINARY\0V1", View(v));
}
Sha256 NodeFingerprint(NodeFingerprintInput i) {
  std::sort(
      i.payloads.begin(), i.payloads.end(),
      [](const auto &a, const auto &b) { return a.fieldTag < b.fieldTag; });
  std::sort(i.properties.begin(), i.properties.end(),
            [](const auto &a, const auto &b) { return a.key < b.key; });
  std::vector<Bytes> p, q, c, r;
  for (const auto &x : i.payloads)
    p.push_back(SemanticPayloadElement(x));
  for (const auto &x : i.properties) {
    const auto d = SemanticPropertyDigest(x);
    q.emplace_back(d.bytes.begin(), d.bytes.end());
  }
  for (const auto &x : i.children)
    c.emplace_back(x.bytes.begin(), x.bytes.end());
  std::sort(i.references.begin(), i.references.end(), ReferenceLess);
  for (const auto &x : i.references)
    r.push_back(ReferenceDescriptor(x));
  Bytes v = EncodePath(i.path);
  U16(&v, static_cast<std::uint16_t>(i.kind));
  U64(&v, i.siblingOrdinal);
  Add(&v, PackedItems(p));
  Add(&v, Seq(q, false));
  Add(&v, Seq(c, false));
  Add(&v, Seq(r, false));
  return DomainHash("HWPGRAPH\0NODE\0V1", View(v));
}
Sha256 ObservedSemanticRoot(const Sha256 &d) {
  return DomainHash("HWPGRAPH\0SEMANTIC\0V1", {d.bytes.data(), 32});
}
Bytes CapturePropertyBytes(const CaptureObservationInput &i) {
  Bytes b;
  U32(&b, i.propertyKey);
  U8(&b, static_cast<std::uint8_t>(i.state));
  U8(&b, static_cast<std::uint8_t>(i.origin));
  const bool p = i.state == ObservationState::Value;
  U8(&b, p ? 1U : 0U);
  U8(&b, 0);
  I32(&b, i.hresult);
  U64(&b, static_cast<std::uint64_t>(i.detailUtf16.size()));
  Add(&b, i.detailUtf16);
  U64(&b, p ? static_cast<std::uint64_t>(i.canonicalValue.size()) : 0);
  if (p)
    Add(&b, i.canonicalValue);
  return Item(b);
}
Sha256 CapturePropertyDigest(const CaptureObservationInput &i) {
  const auto bytes = CapturePropertyBytes(i);
  return DomainHash("HWPGRAPH\0PROPERTY\0CAPTURE\0V1", View(bytes));
}
Bytes AvailableValueBytes(const CaptureObservationInput &i) {
  Bytes b = EncodePath(i.ownerPath);
  U16(&b, static_cast<std::uint16_t>(i.ownerRecordKind));
  U16(&b, i.ownerFieldTag);
  U8(&b, i.propertyKeyPresent ? 1U : 0U);
  if (i.propertyKeyPresent) {
    U32(&b, i.propertyKey);
    U8(&b, static_cast<std::uint8_t>(i.origin));
  }
  const auto d = Hash(View(i.canonicalValue));
  b.insert(b.end(), d.bytes.begin(), d.bytes.end());
  U64(&b, static_cast<std::uint64_t>(i.canonicalValue.size()));
  Add(&b, i.canonicalValue);
  return Item(b);
}
Bytes CoverageBytes(const CoverageInput &i) {
  Bytes b;
  U8(&b, i.ownerPresent ? 1U : 0U);
  if (i.ownerPresent)
    Add(&b, EncodePath(i.ownerPath));
  U16(&b, static_cast<std::uint16_t>(i.ownerRecordKind));
  U16(&b, i.ownerFieldTag);
  U8(&b, i.profile);
  U8(&b, i.propertyKeyPresent ? 1U : 0U);
  if (i.propertyKeyPresent)
    U32(&b, i.propertyKey);
  U8(&b, static_cast<std::uint8_t>(i.state));
  U64(&b, static_cast<std::uint64_t>(i.detailUtf16.size()));
  Add(&b, i.detailUtf16);
  return Item(b);
}
Bytes UnavailableBytes(const CaptureObservationInput &i) {
  Bytes b = EncodePath(i.ownerPath);
  U16(&b, static_cast<std::uint16_t>(i.ownerRecordKind));
  U16(&b, i.ownerFieldTag);
  U8(&b, i.propertyKeyPresent ? 1U : 0U);
  if (i.propertyKeyPresent)
    U32(&b, i.propertyKey);
  U8(&b, static_cast<std::uint8_t>(i.state));
  I32(&b, i.hresult);
  U64(&b, static_cast<std::uint64_t>(i.detailUtf16.size()));
  Add(&b, i.detailUtf16);
  return Item(b);
}
Bytes DiagnosticBytes(const DiagnosticInput &i) {
  Bytes b;
  U8(&b, i.ownerPresent ? 1U : 0U);
  if (i.ownerPresent)
    Add(&b, EncodePath(i.ownerPath));
  U32(&b, static_cast<std::uint32_t>(i.code));
  U8(&b, static_cast<std::uint8_t>(i.severity));
  U8(&b, i.propertyKeyPresent ? 1U : 0U);
  if (i.propertyKeyPresent)
    U32(&b, i.propertyKey);
  I32(&b, i.hresult);
  U64(&b, static_cast<std::uint64_t>(i.detailUtf16.size()));
  Add(&b, i.detailUtf16);
  return Item(b);
}
Sha256 LayoutRoot(LayoutRootInput input) {
  std::sort(input.layoutFacts.begin(), input.layoutFacts.end(),
            [](const LayoutFactInput &a, const LayoutFactInput &b) {
              if (PathLess(a.ownerPath, b.ownerPath))
                return true;
              if (PathLess(b.ownerPath, a.ownerPath))
                return false;
              return a.propertyKey < b.propertyKey;
            });
  std::vector<Bytes> facts;
  facts.reserve(input.layoutFacts.size());
  for (const auto &fact : input.layoutFacts) {
    Bytes bytes = EncodePath(fact.ownerPath);
    U32(&bytes, fact.propertyKey);
    Add(&bytes, fact.canonicalValue);
    facts.push_back(Item(std::move(bytes)));
  }
  Bytes value(input.observedSemanticRoot.bytes.begin(),
              input.observedSemanticRoot.bytes.end());
  Add(&value, PackedItems(facts));
  Frame(&value, input.layoutEnvironment);
  return DomainHash("HWPGRAPH\0LAYOUT\0V1", View(value));
}
Error CaptureRoot(CaptureRootInput input, Sha256 *root) noexcept {
  if (root == nullptr)
    return Error::BadFlags;
  *root = {};
  if ((input.layoutPresent && !input.semanticCertified) ||
      (input.semanticCertified &&
       input.integrity != CaptureIntegrity::Complete) ||
      !HasOnlyKnownProfiles(input.closedProfileBits))
    return Error::IllegalLayoutState;
  for (const auto &value : input.layoutValues)
    if (value.state != ObservationState::Value)
      return Error::IllegalStreamState;
  for (const auto &value : input.captureOnlyValues)
    if (value.state != ObservationState::Value)
      return Error::IllegalStreamState;
  for (const auto &value : input.unavailable)
    if (value.state == ObservationState::Value ||
        value.state == ObservationState::ProjectionOmitted)
      return Error::IllegalStreamState;
  for (const auto &coverage : input.coverage)
    if (coverage.state == CoverageState::ProjectionOmitted)
      return Error::IllegalStreamState;
  const auto observationLess = [](const CaptureObservationInput &a,
                                  const CaptureObservationInput &b) {
    if (PathLess(a.ownerPath, b.ownerPath))
      return true;
    if (PathLess(b.ownerPath, a.ownerPath))
      return false;
    if (a.ownerRecordKind != b.ownerRecordKind)
      return static_cast<std::uint16_t>(a.ownerRecordKind) <
             static_cast<std::uint16_t>(b.ownerRecordKind);
    if (a.ownerFieldTag != b.ownerFieldTag)
      return a.ownerFieldTag < b.ownerFieldTag;
    if (a.propertyKeyPresent != b.propertyKeyPresent)
      return a.propertyKeyPresent < b.propertyKeyPresent;
    if (a.propertyKey != b.propertyKey)
      return a.propertyKey < b.propertyKey;
    if (a.state != b.state)
      return static_cast<std::uint8_t>(a.state) <
             static_cast<std::uint8_t>(b.state);
    if (a.hresult != b.hresult)
      return a.hresult < b.hresult;
    return a.detailUtf16 < b.detailUtf16;
  };
  const auto availableLess = [&](const CaptureObservationInput &a,
                                 const CaptureObservationInput &b) {
    if (observationLess(a, b))
      return true;
    if (observationLess(b, a))
      return false;
    const Sha256 ad = Hash(View(a.canonicalValue));
    const Sha256 bd = Hash(View(b.canonicalValue));
    return ad.bytes < bd.bytes;
  };
  const auto coverageLess = [](const CoverageInput &a, const CoverageInput &b) {
    if (a.ownerPresent != b.ownerPresent)
      return a.ownerPresent < b.ownerPresent;
    if (PathLess(a.ownerPath, b.ownerPath))
      return true;
    if (PathLess(b.ownerPath, a.ownerPath))
      return false;
    if (a.ownerRecordKind != b.ownerRecordKind)
      return static_cast<std::uint16_t>(a.ownerRecordKind) <
             static_cast<std::uint16_t>(b.ownerRecordKind);
    if (a.ownerFieldTag != b.ownerFieldTag)
      return a.ownerFieldTag < b.ownerFieldTag;
    if (a.profile != b.profile)
      return a.profile < b.profile;
    if (a.propertyKeyPresent != b.propertyKeyPresent)
      return a.propertyKeyPresent < b.propertyKeyPresent;
    if (a.propertyKey != b.propertyKey)
      return a.propertyKey < b.propertyKey;
    if (a.state != b.state)
      return static_cast<std::uint8_t>(a.state) <
             static_cast<std::uint8_t>(b.state);
    return a.detailUtf16 < b.detailUtf16;
  };
  const auto diagnosticLess = [](const DiagnosticInput &a,
                                 const DiagnosticInput &b) {
    if (a.ownerPresent != b.ownerPresent)
      return a.ownerPresent < b.ownerPresent;
    if (PathLess(a.ownerPath, b.ownerPath))
      return true;
    if (PathLess(b.ownerPath, a.ownerPath))
      return false;
    if (a.code != b.code)
      return static_cast<std::uint32_t>(a.code) <
             static_cast<std::uint32_t>(b.code);
    if (a.severity != b.severity)
      return static_cast<std::uint8_t>(a.severity) <
             static_cast<std::uint8_t>(b.severity);
    if (a.propertyKeyPresent != b.propertyKeyPresent)
      return a.propertyKeyPresent < b.propertyKeyPresent;
    if (a.propertyKey != b.propertyKey)
      return a.propertyKey < b.propertyKey;
    if (a.hresult != b.hresult)
      return a.hresult < b.hresult;
    return a.detailUtf16 < b.detailUtf16;
  };
  std::sort(input.layoutValues.begin(), input.layoutValues.end(),
            availableLess);
  std::sort(input.captureOnlyValues.begin(), input.captureOnlyValues.end(),
            availableLess);
  std::sort(input.coverage.begin(), input.coverage.end(), coverageLess);
  std::sort(input.unavailable.begin(), input.unavailable.end(),
            observationLess);
  std::sort(input.diagnostics.begin(), input.diagnostics.end(), diagnosticLess);
  std::vector<Bytes> layout, capture, coverage, unavailable, diagnostics;
  for (const auto &item : input.layoutValues)
    layout.push_back(AvailableValueBytes(item));
  for (const auto &item : input.captureOnlyValues)
    capture.push_back(AvailableValueBytes(item));
  for (const auto &item : input.coverage)
    coverage.push_back(CoverageBytes(item));
  for (const auto &item : input.unavailable)
    unavailable.push_back(UnavailableBytes(item));
  for (const auto &item : input.diagnostics)
    diagnostics.push_back(DiagnosticBytes(item));
  Bytes value;
  U16(&value, 1);
  U64(&value, input.closedProfileBits);
  U8(&value, static_cast<std::uint8_t>(input.integrity));
  U8(&value, input.semanticCertified ? 1U : 0U);
  value.insert(value.end(), input.observedSemanticRoot.bytes.begin(),
               input.observedSemanticRoot.bytes.end());
  U8(&value, input.layoutPresent ? 1U : 0U);
  if (input.layoutPresent)
    value.insert(value.end(), input.layoutRoot.bytes.begin(),
                 input.layoutRoot.bytes.end());
  else
    value.insert(value.end(), 32, 0);
  Add(&value, PackedItems(layout));
  Add(&value, PackedItems(capture));
  Add(&value, PackedItems(coverage));
  Add(&value, PackedItems(unavailable));
  Add(&value, PackedItems(diagnostics));
  *root = DomainHash("HWPGRAPH\0CAPTURE\0V1", View(value));
  return Error::None;
}
Sha256 CaptureRoot(CaptureRootInput input) {
  Sha256 root{};
  static_cast<void>(CaptureRoot(std::move(input), &root));
  return root;
}

namespace {
struct ParsedField final {
  FieldTag tag = 0;
  std::uint16_t flags = 0;
  ScalarTag scalar = ScalarTag::Bytes;
  std::uint64_t count = 0;
  ByteView value{};
};
struct ParsedFields final {
  std::array<ParsedField, 128> fields{};
  std::uint16_t count = 0;
};
thread_local std::uint64_t gFieldDecodeWalks = 0;

Error ParseFields(ByteView bytes, std::uint16_t declaredCount,
                  ParsedFields *parsed) noexcept {
  ++gFieldDecodeWalks;
  if (parsed == nullptr || declaredCount > parsed->fields.size())
    return Error::BadNestedStruct;
  parsed->count = declaredCount;
  std::uint64_t at = 0;
  FieldTag prior = 0;
  for (std::uint16_t index = 0; index < declaredCount; ++index) {
    if (!Fits(at, 24, bytes.size))
      return Error::Truncated;
    const auto *header = bytes.data + at;
    const FieldTag tag = R16(header);
    const std::uint16_t flags = R16(header + 2);
    const std::uint16_t scalar = R16(header + 4);
    const std::uint64_t count = R64(header + 8);
    const std::uint64_t length = R64(header + 16);
    if (tag == 0 || R16(header + 6) != 0 || (flags & ~3U) != 0 || scalar > 17)
      return Error::BadFlags;
    if (index != 0 && tag == prior)
      return Error::DuplicateField;
    if (index != 0 && tag < prior)
      return Error::OutOfOrderField;
    at += 24;
    if (!Fits(at, length, bytes.size))
      return Error::Truncated;
    ParsedField &field = parsed->fields[index];
    field = {tag,
             flags,
             static_cast<ScalarTag>(scalar),
             count,
             {bytes.data + at, length}};
    if ((flags & kFieldFlagArray) == 0 && count != 1)
      return Error::BadArray;
    const Error error = ValidateFieldView(
        flags, field.scalar, count, {bytes.data + at, length});
    if (error != Error::None)
      return error;
    at += length;
    prior = tag;
  }
  return at == bytes.size ? Error::None : Error::Truncated;
}
const ParsedField *Find(const ParsedFields &fields, FieldTag tag) noexcept {
  for (std::uint16_t index = 0; index < fields.count; ++index)
    if (fields.fields[index].tag == tag)
      return &fields.fields[index];
  return nullptr;
}
Error ValidateRuleSet(const ParsedFields &fields, RecordKind kind) noexcept {
  for (std::uint16_t index = 0; index < fields.count; ++index) {
    const ParsedField &field = fields.fields[index];
    const RecordFieldRule *matched = nullptr;
    for (const auto &rule : kRecordFieldRules)
      if (rule.record == kind && rule.tag == field.tag) {
        matched = &rule;
        break;
      }
    if (matched == nullptr) {
      if ((field.flags & kFieldFlagRequired) != 0)
        return Error::UnknownRequiredField;
      continue;
    }
    const bool required = matched->requirement == Requirement::Required;
    if (field.scalar != matched->scalar)
      return Error::BadScalar;
    if (((field.flags & kFieldFlagRequired) != 0) != required ||
        (field.flags & kFieldFlagArray) != 0)
      return Error::BadFlags;
  }
  for (const auto &rule : kRecordFieldRules)
    if (rule.record == kind && rule.requirement == Requirement::Required &&
        Find(fields, rule.tag) == nullptr)
      return Error::MissingRequiredField;
  return Error::None;
}
Error ParseNested(ByteView bytes, ParsedFields *fields) noexcept {
  std::uint64_t at = 0;
  std::uint16_t count = 0;
  while (at != bytes.size) {
    if (!Fits(at, 24, bytes.size) || count == UINT16_MAX)
      return Error::BadNestedStruct;
    const std::uint64_t length = R64(bytes.data + at + 16);
    at += 24;
    if (!Fits(at, length, bytes.size))
      return Error::BadNestedStruct;
    at += length;
    ++count;
  }
  return ParseFields(bytes, count, fields);
}
bool ScalarMatches(const ParsedField &field, ScalarTag scalar,
                   bool array) noexcept {
  return field.scalar == scalar &&
         ((field.flags & kFieldFlagArray) != 0) == array;
}
Error ValidateNativeLocator(ByteView bytes) noexcept {
  if (bytes.size < 8 ||
      R16(bytes.data) > static_cast<std::uint16_t>(LocatorTag::Cell))
    return Error::BadNestedStruct;
  for (std::uint64_t index = 2; index < 8; ++index)
    if (bytes.data[index] != 0)
      return Error::ReservedNonzero;
  const auto tag = static_cast<LocatorTag>(R16(bytes.data));
  ByteView payload{bytes.data + 8, bytes.size - 8};
  if (tag == LocatorTag::None)
    return payload.size == 0 ? Error::None : Error::BadNestedStruct;
  if (tag == LocatorTag::Story)
    return payload.size == 8 ? Error::None : Error::BadNestedStruct;
  if (tag == LocatorTag::Paragraph)
    return payload.size == 16 ? Error::None : Error::BadNestedStruct;
  if (tag == LocatorTag::Run)
    return payload.size == 32 ? Error::None : Error::BadNestedStruct;
  if (tag == LocatorTag::Cell) {
    if (payload.size < 32)
      return Error::BadNestedStruct;
    const std::uint64_t units = R64(payload.data + 16);
    return units <= ((std::numeric_limits<std::uint64_t>::max)() / 2) &&
                   16 + 8 + units * 2 + 8 == payload.size
               ? Error::None
               : Error::BadNestedStruct;
  }
  if (payload.size < 8)
    return Error::BadNestedStruct;
  const std::uint64_t ctrlUnits = R64(payload.data);
  if (ctrlUnits > ((std::numeric_limits<std::uint64_t>::max)() / 2) ||
      !Fits(8, ctrlUnits * 2 + 1, payload.size))
    return Error::BadNestedStruct;
  std::uint64_t at = 8 + ctrlUnits * 2;
  const std::uint8_t present = payload.data[at++];
  if (present > 1)
    return Error::BadNestedStruct;
  if (present != 0) {
    if (!Fits(at, 8, payload.size))
      return Error::BadNestedStruct;
    const std::uint64_t units = R64(payload.data + at);
    at += 8;
    if (units > ((std::numeric_limits<std::uint64_t>::max)() / 2) ||
        !Fits(at, units * 2, payload.size))
      return Error::BadNestedStruct;
    at += units * 2;
  }
  return payload.size - at == 32 ? Error::None : Error::BadNestedStruct;
}
Error ValidateStructuredValue(NodeKind kind, FieldTag tag,
                              ByteView value) noexcept {
  if ((kind == NodeKind::Section && (tag == 101 || tag == 102)) ||
      ((kind == NodeKind::Paragraph && (tag == 100 || tag == 101)) ||
       ((kind == NodeKind::GenericControl || kind == NodeKind::Table ||
         kind == NodeKind::Image) &&
        (tag == 103 || tag == 104))))
    return value.size == 24 ? Error::None : Error::BadNestedStruct;
  if ((kind == NodeKind::CharacterRun && tag == 100) ||
      (kind == NodeKind::SpecialCharacter && tag == 100) ||
      (kind == NodeKind::GeneratedText && tag == 100))
    return value.size == 32 ? Error::None : Error::BadNestedStruct;
  if ((kind == NodeKind::TableCell && tag == 108) ||
      ((kind == NodeKind::Image) && (tag == 107 || tag == 108)))
    return value.size == 16 ? Error::None : Error::BadNestedStruct;
  if (kind == NodeKind::Image && tag == 109)
    return value.size == 32 ? Error::None : Error::BadNestedStruct;
  if (kind == NodeKind::Image && tag == 110)
    return value.size == 56 ? Error::None : Error::BadNestedStruct;
  return Error::None;
}
Error ValidateNode(const ParsedFields &outer, StreamKind stream) noexcept {
  const ParsedField *commonField = Find(outer, 1),
                    *payloadField = Find(outer, 2);
  if (commonField == nullptr || payloadField == nullptr ||
      commonField->scalar != ScalarTag::Struct ||
      payloadField->scalar != ScalarTag::Struct)
    return Error::MissingRequiredField;
  ParsedFields common{}, payload{};
  Error error = ParseNested(commonField->value, &common);
  if (error != Error::None)
    return error;
  error = ParseNested(payloadField->value, &payload);
  if (error != Error::None)
    return error;
  const ParsedField *kindField = Find(common, 2);
  if (kindField == nullptr || kindField->scalar != ScalarTag::Uint16 ||
      kindField->value.size != 2 || R16(kindField->value.data) > 12)
    return Error::BadNestedStruct;
  const NodeKind kind = static_cast<NodeKind>(R16(kindField->value.data));
  constexpr FieldTag requiredCommon[]{1, 2, 4, 5, 6, 7, 8, 10, 11};
  for (FieldTag tag : requiredCommon)
    if (Find(common, tag) == nullptr)
      return Error::MissingRequiredField;
  if (kind != NodeKind::Document && Find(common, 3) == nullptr)
    return Error::MissingRequiredField;
  if (kind == NodeKind::Document && Find(common, 3) != nullptr)
    return Error::BadNestedStruct;
  for (std::uint16_t index = 0; index < common.count; ++index) {
    const ParsedField &field = common.fields[index];
    const NodeFieldRule *rule = nullptr;
    for (const auto &candidate : kCommonNodeFieldRules)
      if (candidate.tag == field.tag) {
        rule = &candidate;
        break;
      }
    if (rule == nullptr)
      return field.tag == kReservedCommonFieldTagV1
                 ? Error::BadNestedStruct
                 : Error::UnknownRequiredField;
    if (!ScalarMatches(field, rule->scalar, rule->array))
      return Error::BadScalar;
    if (((field.flags & kFieldFlagRequired) != 0) !=
        (rule->requirement == Requirement::Required))
      return Error::BadFlags;
    if (field.tag == 7) {
      error = ValidateObservation(field.value, ScalarTag::Struct);
      if (error != Error::None)
        return error;
      if (field.value.data[0] ==
              static_cast<std::uint8_t>(ObservationState::ProjectionOmitted) &&
          stream == StreamKind::Capture)
        return Error::IllegalStreamState;
      if (field.value.data[0] ==
          static_cast<std::uint8_t>(ObservationState::Value)) {
        const std::uint64_t detail = R64(field.value.data + 8);
        error = ValidateNativeLocator(
            {field.value.data + 24 + detail * 2, R64(field.value.data + 16)});
        if (error != Error::None)
          return error;
      }
    }
  }
  for (std::uint16_t index = 0; index < payload.count; ++index) {
    const ParsedField &field = payload.fields[index];
    if (kind == NodeKind::Definition && field.tag == 100 &&
        (field.value.size != 2 ||
         R16(field.value.data) >
             static_cast<std::uint16_t>(DefinitionKind::ColumnDef)))
      return Error::BadScalar;
    const NodeFieldRule *rule = nullptr;
    for (const auto &candidate : kNodePayloadFieldRules)
      if (candidate.node == kind && candidate.tag == field.tag) {
        rule = &candidate;
        break;
      }
    if (rule == nullptr &&
        (kind == NodeKind::Table || kind == NodeKind::Image) &&
        field.tag >= 100 && field.tag <= 106)
      for (const auto &candidate : kNodePayloadFieldRules)
        if (candidate.node == NodeKind::GenericControl &&
            candidate.tag == field.tag) {
          rule = &candidate;
          break;
        }
    if (rule == nullptr)
      return Error::UnknownRequiredField;
    if (!ScalarMatches(field, rule->scalar, rule->array))
      return Error::BadScalar;
    if (((field.flags & kFieldFlagRequired) != 0) !=
        (rule->requirement == Requirement::Required))
      return Error::BadFlags;
    ByteView structured = field.value;
    if (rule->observation) {
      error = ValidateObservation(field.value, rule->scalar);
      if (error != Error::None)
        return error;
      const auto state = static_cast<ObservationState>(field.value.data[0]);
      if (state == ObservationState::ProjectionOmitted &&
          stream == StreamKind::Capture)
        return Error::IllegalStreamState;
      if (state != ObservationState::Value)
        continue;
      const std::uint64_t detail = R64(field.value.data + 8);
      structured = {field.value.data + 24 + detail * 2,
                    R64(field.value.data + 16)};
    }
    if (rule->scalar == ScalarTag::Struct) {
      error = ValidateStructuredValue(kind, field.tag, structured);
      if (error != Error::None)
        return error;
    }
  }
  for (const auto &rule : kNodePayloadFieldRules) {
    const bool applies =
        rule.node == kind ||
        ((kind == NodeKind::Table || kind == NodeKind::Image) &&
         rule.node == NodeKind::GenericControl && rule.tag <= 106);
    if (applies && rule.requirement == Requirement::Required &&
        Find(payload, rule.tag) == nullptr)
      return Error::MissingRequiredField;
  }
  return Error::None;
}
Error ValidateProperty(const ParsedFields &fields, StreamKind stream) noexcept {
  const ParsedField *keyField = Find(fields, 3),
                    *observationField = Find(fields, 4),
                    *originField = Find(fields, 5);
  if (keyField == nullptr || observationField == nullptr ||
      originField == nullptr || keyField->value.size != 4 ||
      originField->value.size != 1 ||
      originField->value.data[0] >
          static_cast<std::uint8_t>(PropertyOrigin::Unavailable))
    return Error::BadNestedStruct;
  const PropertyRule *rule = FindPropertyRule(R32(keyField->value.data));
  if (rule == nullptr)
    return Error::BadScalar;
  const ScalarTag observationScalar =
      rule->valueShape == PropertyValueShape::Scalar ? rule->scalar
                                                     : ScalarTag::Struct;
  Error error = ValidateObservation(observationField->value, observationScalar);
  if (error != Error::None)
    return error;
  const auto state =
      static_cast<ObservationState>(observationField->value.data[0]);
  if (state == ObservationState::ProjectionOmitted &&
      stream == StreamKind::Capture)
    return Error::IllegalStreamState;
  if (state != ObservationState::Value ||
      rule->valueShape == PropertyValueShape::Scalar)
    return Error::None;
  const std::uint64_t detail = R64(observationField->value.data + 8);
  ByteView arrayBytes{observationField->value.data + 24 + detail * 2,
                      R64(observationField->value.data + 16)};
  if (arrayBytes.size < 16)
    return Error::BadArray;
  const std::uint64_t elementCount = R64(arrayBytes.data + 8);
  error = CheckArrayView(rule->scalar, elementCount, arrayBytes);
  if (error != Error::None)
    return error;
  if (rule->valueShape != PropertyValueShape::Uint16Array) {
    std::uint64_t at = 16;
    for (std::uint64_t item = 0; item < elementCount; ++item) {
      const std::uint64_t length = R64(arrayBytes.data + at);
      at += 8;
      const NestedSchema nestedSchema =
          rule->valueShape == PropertyValueShape::NumberingLevelArray
              ? NestedSchema::NumberingLevel
          : rule->valueShape == PropertyValueShape::TabItemArray
              ? NestedSchema::TabItem
              : NestedSchema::ColumnWidth;
      error =
          ValidateNestedStructure(nestedSchema, {arrayBytes.data + at, length});
      if (error != Error::None)
        return error;
      at += length;
    }
  }
  return Error::None;
}
bool IsNodeCoverageField(const NodeKind kind, const FieldTag tag) noexcept {
  if (tag == 0 || tag == kReservedCommonFieldTagV1)
    return false;
  for (const auto &rule : kCommonNodeFieldRules)
    if (rule.tag == tag)
      return true;
  for (const auto &rule : kNodePayloadFieldRules)
    if ((rule.node == kind ||
         ((kind == NodeKind::Table || kind == NodeKind::Image) &&
          rule.node == NodeKind::GenericControl && rule.tag <= 106)) &&
        rule.tag == tag)
      return true;
  return false;
}
Error ValidateCoverageCoordinate(const ParsedFields &fields) noexcept {
  const bool ownerPresent = Find(fields, 1) != nullptr;
  const std::uint8_t profile = Find(fields, 2)->value.data[0];
  const bool propertyPresent = Find(fields, 4) != nullptr;
  const auto ownerKind = static_cast<RecordKind>(
      R16(Find(fields, 6)->value.data));
  const FieldTag ownerField = R16(Find(fields, 7)->value.data);
  for (const CoverageCoordinateRule& rule : kCoverageCoordinateRules)
    if (IsCoverageCoordinate(
            rule.kind, ownerPresent, profile, ownerKind, ownerField,
            propertyPresent))
      return Error::None;
  return Error::IllegalStreamState;
}
Error ValidateSchemaRecord(ByteView record, StreamKind stream) noexcept {
  const RecordKind kind = static_cast<RecordKind>(R16(record.data));
  ParsedFields fields{};
  Error error = ParseFields({record.data + 24, record.size - 24},
                            R16(record.data + 6), &fields);
  if (error != Error::None)
    return error;
  error = ValidateRuleSet(fields, kind);
  if (error != Error::None)
    return error;
  if (kind == RecordKind::Manifest) {
    const ParsedField *version = Find(fields, 1), *integrity = Find(fields, 2),
                      *streamKind = Find(fields, 6);
    const std::uint64_t profiles = R64(Find(fields, 3)->value.data);
    if (version->value.size != kGraphVersionBytesV1 ||
        R16(version->value.data) != kCodecVersion ||
        R64(version->value.data + 2) != profiles ||
        version->value.data[10] > 1 || version->value.data[11] > 1 ||
        version->value.data[12] != 0 || version->value.data[13] != 0 ||
        version->value.data[14] != 0 || version->value.data[15] != 0 ||
        (version->value.data[11] != 0 && version->value.data[10] == 0) ||
        integrity->value.data[0] >
            static_cast<std::uint8_t>(CaptureIntegrity::StateRestoreFailure) ||
        streamKind->value.data[0] != static_cast<std::uint8_t>(stream) ||
        !HasOnlyKnownProfiles(profiles) ||
        CloseProfileBits(profiles) != profiles)
      return Error::IllegalStreamState;
  } else if (kind == RecordKind::Node) {
    error = ValidateNode(fields, stream);
  } else if (kind == RecordKind::Property) {
    error = ValidateProperty(fields, stream);
  } else if (kind == RecordKind::Coverage) {
    const ParsedField *state = Find(fields, 3);
    const ParsedField *key = Find(fields, 4);
    if ((key != nullptr && FindPropertyRule(R32(key->value.data)) == nullptr) ||
        state->value.data[0] >
            static_cast<std::uint8_t>(CoverageState::ProjectionOmitted) ||
        (state->value.data[0] ==
             static_cast<std::uint8_t>(CoverageState::ProjectionOmitted) &&
         stream == StreamKind::Capture))
      error = Error::IllegalStreamState;
    if (error == Error::None)
      error = ValidateCoverageCoordinate(fields);
  } else if (kind == RecordKind::Diagnostic) {
    const ParsedField *key = Find(fields, 4);
    if (key != nullptr && FindPropertyRule(R32(key->value.data)) == nullptr)
      error = Error::IllegalStreamState;
  } else if (kind == RecordKind::Tombstone) {
    const auto reason = static_cast<TombstoneReason>(
        R16(Find(fields, 3)->value.data));
    if (R64(Find(fields, 2)->value.data) == 0 ||
        reason > TombstoneReason::ReopenNewLineage)
      error = Error::IllegalStreamState;
  } else if (kind == RecordKind::Remap) {
    const auto disposition = static_cast<RemapDisposition>(
        Find(fields, 3)->value.data[0]);
    const auto reason = static_cast<RemapReason>(
        R16(Find(fields, 4)->value.data));
    const bool targetPresent = Find(fields, 2) != nullptr;
    if (disposition > RemapDisposition::Ambiguous ||
        reason > RemapReason::Delete ||
        ((disposition == RemapDisposition::Retained ||
          disposition == RemapDisposition::New) && !targetPresent) ||
        disposition == RemapDisposition::Tombstoned && targetPresent)
      error = Error::IllegalStreamState;
  }
  return error;
}
bool ReadStream(const RecordStream &stream, std::uint64_t offset,
                std::uint8_t *buffer, std::uint32_t length) noexcept {
  std::uint32_t done = 0;
  while (done != length) {
    std::uint32_t actual = 0;
    if (!stream.read(stream.context, offset + done, buffer + done,
                     length - done, &actual) ||
        actual == 0 || actual > length - done)
      return false;
    done += actual;
  }
  return true;
}
} // namespace

Error ValidateNestedStructure(NestedSchema schema, ByteView bytes) noexcept {
  if (schema == NestedSchema::Position)
    return bytes.size == 24 ? Error::None : Error::BadNestedStruct;
  if (schema == NestedSchema::ParagraphRange)
    return bytes.size == 32 ? Error::None : Error::BadNestedStruct;
  if (schema == NestedSchema::Size)
    return bytes.size == 16 ? Error::None : Error::BadNestedStruct;
  if (schema == NestedSchema::Crop)
    return bytes.size == 32 ? Error::None : Error::BadNestedStruct;
  if (schema == NestedSchema::BlobRef)
    return bytes.size == 56 ? Error::None : Error::BadNestedStruct;
  if (schema == NestedSchema::NativeLocator)
    return ValidateNativeLocator(bytes);
  ParsedFields fields{};
  Error error = ParseNested(bytes, &fields);
  if (error != Error::None)
    return error;
  const std::array<ScalarTag, 6> numbering{
      ScalarTag::Uint64, ScalarTag::Sint64,   ScalarTag::UTF16,
      ScalarTag::Enum,   ScalarTag::RawURC32, ScalarTag::Uint64};
  const std::array<ScalarTag, 3> tabs{ScalarTag::RawURC32, ScalarTag::Enum,
                                      ScalarTag::Enum};
  const std::array<ScalarTag, 3> columns{ScalarTag::Uint64, ScalarTag::RawURC32,
                                         ScalarTag::RawURC32};
  const ScalarTag *expected = nullptr;
  std::size_t required = 0, allowed = 0;
  if (schema == NestedSchema::NumberingLevel) {
    expected = numbering.data();
    required = 5;
    allowed = 6;
  } else if (schema == NestedSchema::TabItem) {
    expected = tabs.data();
    required = allowed = tabs.size();
  } else if (schema == NestedSchema::ColumnWidth) {
    expected = columns.data();
    required = allowed = columns.size();
  } else {
    return Error::BadNestedStruct;
  }
  for (std::size_t tag = 1; tag <= required; ++tag)
    if (Find(fields, static_cast<FieldTag>(tag)) == nullptr)
      return Error::MissingRequiredField;
  for (std::uint16_t index = 0; index < fields.count; ++index) {
    const ParsedField &field = fields.fields[index];
    if (field.tag > allowed)
      return (field.flags & kFieldFlagRequired) != 0
                 ? Error::UnknownRequiredField
                 : Error::None;
    if (field.scalar != expected[field.tag - 1] ||
        (field.flags & kFieldFlagArray) != 0)
      return Error::BadScalar;
    if (((field.flags & kFieldFlagRequired) != 0) != (field.tag <= required))
      return Error::BadFlags;
  }
  return Error::None;
}

namespace {
struct UuidKeyHash final {
  std::size_t operator()(
      const std::array<std::uint8_t, 16> &key) const noexcept {
    std::size_t value = sizeof(std::size_t) == 8
                            ? static_cast<std::size_t>(1469598103934665603ULL)
                            : static_cast<std::size_t>(2166136261U);
    const std::size_t prime = sizeof(std::size_t) == 8
                                  ? static_cast<std::size_t>(1099511628211ULL)
                                  : static_cast<std::size_t>(16777619U);
    for (const std::uint8_t byte : key) {
      value ^= byte;
      value *= prime;
    }
    return value;
  }
};

struct ParsedFieldRange final {
  std::size_t first = 0;
  std::uint16_t count = 0;
};
struct ParsedFieldSpan final {
  const ParsedField* fields = nullptr;
  std::uint16_t count = 0;
};
const ParsedField* Find(const ParsedFieldSpan fields,
                        const FieldTag tag) noexcept {
  for (std::uint16_t index = 0; index < fields.count; ++index)
    if (fields.fields[index].tag == tag)
      return &fields.fields[index];
  return nullptr;
}
struct ParsedRecordWalkerRecord final {
  std::uint64_t offset = 0;
  std::uint64_t next = 0;
  Bytes owned{};
  ByteView bytes{};
  ParsedFieldRange outer{};
  ParsedFieldRange common{};
  ParsedFieldRange payload{};
};
std::atomic<std::uint64_t> gParsedRecordWalks{0};
std::atomic<std::uint64_t> gLegacyValidatorWalks{0};
std::atomic<std::uint64_t> gSourceBytesRead{0};
std::atomic<std::uint64_t> gRetainedRecordCopies{0};
std::atomic<std::uint64_t> gBlobHashOperations{0};
std::atomic<std::uint64_t> gBlobHashBufferBytes{0};
thread_local std::uint64_t gDescriptorRecordBuilds = 0;
thread_local std::uint64_t gDescriptorRecordReuses = 0;
thread_local std::uint64_t gDescriptorFields = 0;
thread_local std::uint64_t gDescriptorArenaGrowths = 0;
thread_local std::uint64_t gCanonicalWalkQpc = 0;
thread_local std::uint64_t gAuthorityPassQpc = 0;
thread_local std::uint64_t gSemanticPassQpc = 0;
std::atomic<std::uint64_t> gValidatedCanonicalPostWalks{0};
std::atomic<std::uint64_t> gIncrementalAccumulatorFinalizations{0};
thread_local bool gCanonicalDescriptorReuseEnabled = true;

void AppendParsedFields(const ParsedFields& source,
                        std::vector<ParsedField>* const arena,
                        ParsedFieldRange* const range) {
  range->first = arena->size();
  range->count = source.count;
  const std::size_t priorCapacity = arena->capacity();
  arena->insert(arena->end(), source.fields.begin(),
                source.fields.begin() + source.count);
  if (arena->capacity() != priorCapacity)
    ++gDescriptorArenaGrowths;
  gDescriptorFields += source.count;
}
ParsedFieldSpan DescriptorSpan(const std::vector<ParsedField>& arena,
                               const ParsedFieldRange range) noexcept {
  return {range.count == 0 ? nullptr : arena.data() + range.first,
          range.count};
}

Error WalkParsedRecordStream(
    const RecordStream &stream, std::uint64_t *recordCount,
    std::vector<ParsedRecordWalkerRecord> *parsed,
    std::vector<ParsedField>* descriptorArena = nullptr) noexcept {
  if (recordCount == nullptr || stream.read == nullptr)
    return Error::BadFlags;
  *recordCount = 0;
  std::uint64_t offset = 0;
  RecordKind previous = RecordKind::Manifest;
  std::uint8_t nodePhase = 0;
  std::uint8_t captureNodePhase = 0;
  bool sawDocument = false, finalPhase = false;
  std::uint64_t declaredRecords = 0;
  using NodeIdKey = std::array<std::uint8_t, 16>;
  struct NodeOrderInfo final {
    NodeKind kind = NodeKind::Document;
    StoryKind story = StoryKind::Other;
    DefinitionKind definition = DefinitionKind::Style;
    bool parentPresent = false;
    NodeIdKey parent{};
    std::uint64_t containsCount = 0;
    std::uint64_t anchorsCount = 0;
    std::uint64_t characterShapeRefCount = 0;
    bool characterShapePropertySeen = false;
    std::uint64_t characterShapeCoverageCount = 0;
    std::uint64_t characterShapeCompleteCoverageCount = 0;
    std::uint64_t characterShapeNotExposedCoverageCount = 0;
    std::uint64_t characterShapeReadFailedCoverageCount = 0;
    std::uint64_t characterShapeContradictoryCoverageCount = 0;
  };
  struct SiblingKey final {
    NodeIdKey parent{};
    NodeKind kind = NodeKind::Document;
    bool operator<(const SiblingKey &other) const noexcept {
      return parent < other.parent ||
             (!(other.parent < parent) && kind < other.kind);
    }
  };
  std::unordered_map<NodeIdKey, NodeOrderInfo, UuidKeyHash> nodes;
  NodeIdKey currentNode{};
  bool currentNodePresent = false;
  std::map<SiblingKey, std::uint64_t> lastSibling;
  std::unordered_map<NodeIdKey, std::uint64_t, UuidKeyHash>
      priorEdgeOrdinal;
  std::unordered_map<NodeIdKey, EdgeKind, UuidKeyHash> priorEdgeKind;
  bool havePropertyKey = false, haveCoverageKey = false,
       haveDiagnosticKey = false;
  std::pair<FieldTag, PropertyKeyId> priorPropertyKey{};
  Bytes priorCoverageKey{}, priorDiagnosticKey{};
  struct PendingEdge final {
    EdgeKind kind = EdgeKind::Contains;
    NodeIdKey source{};
    NodeIdKey target{};
  };
  std::vector<PendingEdge> pendingEdges;
  bool haveDefinitionOrder = false, haveBinaryOrder = false;
  DefinitionKind priorDefinitionKind = DefinitionKind::Style;
  bool priorDefinitionNativePresent = false;
  std::int64_t priorDefinitionNative = 0;
  Sha256 priorDefinitionAggregate{};
  std::map<std::pair<DefinitionKind, std::int64_t>, Sha256>
      exposedDefinitionAggregates;
  struct ReceiptRemap final {
    bool targetPresent = false;
    NodeIdKey target{};
    RemapDisposition disposition = RemapDisposition::Retained;
    RemapReason reason = RemapReason::IdentityRetained;
  };
  std::unordered_map<NodeIdKey, TombstoneReason, UuidKeyHash> tombstones;
  std::unordered_map<NodeIdKey, std::vector<ReceiptRemap>, UuidKeyHash>
      remaps;
  std::unordered_map<NodeIdKey, NodeIdKey, UuidKeyHash> claimedRemapTargets;
  bool haveTombstoneOrder = false, haveRemapOrder = false;
  std::tuple<NodeIdKey, std::uint64_t, TombstoneReason> priorTombstone{};
  std::tuple<NodeIdKey, RemapDisposition, bool, NodeIdKey, RemapReason>
      priorRemap{};
  Sha256 priorBinaryDigest{};
  std::uint64_t priorBinaryLength = 0;
  while (true) {
    // Producer-backed streams: sample completion before the length so a
    // true completion always pairs with the final length. Sampling the
    // length first can pair a stale length with a fresh completion and end
    // the walk before the producer's tail records were consumed.
    const bool producerComplete = stream.currentLength != nullptr &&
        stream.complete != nullptr && stream.complete(stream.context);
    const std::uint64_t available = stream.currentLength != nullptr
        ? stream.currentLength(stream.context) : stream.length;
    if (offset == available &&
        (stream.currentLength == nullptr || producerComplete))
      break;
    std::array<std::uint8_t, 24> header{};
    if (stream.currentLength == nullptr &&
        available - offset < header.size())
      return Error::Truncated;
    if (!ReadStream(stream, offset, header.data(),
                    static_cast<std::uint32_t>(header.size()))) {
      const std::uint64_t finalLength = stream.currentLength != nullptr
          ? stream.currentLength(stream.context) : stream.length;
      if (stream.currentLength != nullptr && stream.complete != nullptr &&
          stream.complete(stream.context) && offset == finalLength)
        break;
      return Error::Truncated;
    }
    const std::uint64_t payload = R64(header.data() + 16);
    const std::uint64_t recordAvailable = stream.currentLength != nullptr
        ? stream.currentLength(stream.context) : stream.length;
    if (payload > MAXDWORD - header.size() || offset > recordAvailable ||
        recordAvailable - offset < header.size() ||
        payload > recordAvailable - offset - header.size())
      return Error::LengthOverflow;
    const std::uint32_t recordBytes =
        static_cast<std::uint32_t>(payload + header.size());
    ParsedRecordWalkerRecord retained{};
    retained.offset = offset;
    retained.next = offset + recordBytes;
    if (stream.resident.data != nullptr &&
        stream.resident.size == stream.length) {
      retained.bytes = {stream.resident.data + offset, recordBytes};
    } else if (stream.viewRecord != nullptr &&
               stream.viewRecord(stream.context, offset, &retained.bytes) &&
               retained.bytes.data != nullptr &&
               retained.bytes.size == recordBytes) {
      // The segmented owner keeps this exact record alive for the walk.
    } else {
      gRetainedRecordCopies.fetch_add(1, std::memory_order_relaxed);
      retained.owned.resize(recordBytes);
      std::copy(header.begin(), header.end(), retained.owned.begin());
      if (payload != 0 &&
          !ReadStream(stream, offset + header.size(),
                      retained.owned.data() + header.size(),
                      static_cast<std::uint32_t>(payload)))
        return Error::Truncated;
      retained.bytes = View(retained.owned);
    }
    const ByteView record = retained.bytes;
    gSourceBytesRead.fetch_add(recordBytes, std::memory_order_relaxed);
    constexpr FieldTag known[]{1, 2, 3, 4, 5, 6, 7};
    Error error = ValidateLogicalRecord(
        record, known, sizeof(known) / sizeof(known[0]), *recordCount);
    if (error != Error::None)
      return error;
    const RecordKind kind = static_cast<RecordKind>(R16(record.data));
    error = ValidateSchemaRecord(record, stream.kind);
    if (error != Error::None)
      return error;
    if (*recordCount == 0) {
      if (kind != RecordKind::Manifest)
        return Error::BadRecordOrder;
      ParsedFields manifestFields{};
      ParseFields({record.data + 24, record.size - 24},
                  R16(record.data + 6), &manifestFields);
      declaredRecords = R64(Find(manifestFields, 4)->value.data);
      if (parsed != nullptr &&
          declaredRecords <= static_cast<std::uint64_t>(
              (std::numeric_limits<std::size_t>::max)()))
        parsed->reserve(static_cast<std::size_t>(declaredRecords));
      if (descriptorArena != nullptr && declaredRecords <=
              static_cast<std::uint64_t>(
                  (std::numeric_limits<std::size_t>::max)() / 4))
        descriptorArena->reserve(static_cast<std::size_t>(declaredRecords) * 4);
    } else if (kind == RecordKind::Manifest) {
      return Error::BadRecordOrder;
    } else if (kind == RecordKind::Node) {
      if (finalPhase)
        return Error::BadRecordOrder;
      nodePhase = 0;
      havePropertyKey = false;
      haveCoverageKey = false;
      haveDiagnosticKey = false;
      ParsedFields outer{}, common{}, payloadFields{};
      ParseFields({record.data + 24, record.size - 24},
                  R16(record.data + 6), &outer);
      ParseNested(Find(outer, 1)->value, &common);
      ParseNested(Find(outer, 2)->value, &payloadFields);
      const NodeKind nodeKind =
          static_cast<NodeKind>(R16(Find(common, 2)->value.data));
      if (stream.kind == StreamKind::Capture) {
        const std::uint8_t wanted = nodeKind == NodeKind::Document     ? 0
                                    : nodeKind == NodeKind::Definition ? 1
                                    : nodeKind == NodeKind::BinaryData ? 2
                                                                       : 3;
        if ((sawDocument && wanted == 0) || wanted < captureNodePhase)
          return Error::BadRecordOrder;
        captureNodePhase = wanted;
      }
      StoryKind storyKind = StoryKind::Other;
      if (nodeKind == NodeKind::Story)
        storyKind =
            static_cast<StoryKind>(R16(Find(payloadFields, 100)->value.data));
      DefinitionKind definitionKind = DefinitionKind::Style;
      if (nodeKind == NodeKind::Definition) {
        definitionKind = static_cast<DefinitionKind>(
            R16(Find(payloadFields, 100)->value.data));
        const ByteView observation = Find(payloadFields, 101)->value;
        const auto nativeState = static_cast<ObservationState>(
            observation.data[0]);
        if (nativeState == ObservationState::NotRequested ||
            nativeState == ObservationState::ProjectionOmitted)
          return Error::BadRecordOrder;
        const bool nativePresent =
            nativeState == ObservationState::Value;
        std::int64_t native = 0;
        if (nativePresent) {
          const std::uint64_t detailUnits = R64(observation.data + 8);
          native = static_cast<std::int64_t>(
              R64(observation.data + 24 + detailUnits * 2));
        }
        Sha256 aggregate{};
        std::copy_n(Find(payloadFields, 103)->value.data,
                    aggregate.bytes.size(), aggregate.bytes.begin());
        if (nativePresent) {
          const auto identity =
              std::make_pair(definitionKind, native);
          const auto priorAggregate = exposedDefinitionAggregates.find(identity);
          if (priorAggregate != exposedDefinitionAggregates.end() &&
              priorAggregate->second.bytes != aggregate.bytes)
            return Error::BadRecordOrder;
          exposedDefinitionAggregates[identity] = aggregate;
        }
        if (haveDefinitionOrder &&
            !DefinitionOrderLess(
                priorDefinitionKind, priorDefinitionNativePresent,
                priorDefinitionNative, priorDefinitionAggregate,
                definitionKind, nativePresent, native, aggregate))
          return Error::BadRecordOrder;
        haveDefinitionOrder = true;
        priorDefinitionKind = definitionKind;
        priorDefinitionNativePresent = nativePresent;
        priorDefinitionNative = native;
        priorDefinitionAggregate = aggregate;
      } else if (nodeKind == NodeKind::BinaryData) {
        Sha256 digest{};
        std::copy_n(Find(payloadFields, 102)->value.data, digest.bytes.size(),
                    digest.bytes.begin());
        const std::uint64_t length = R64(Find(payloadFields, 101)->value.data);
        if (haveBinaryOrder && (digest.bytes < priorBinaryDigest.bytes ||
                                (digest.bytes == priorBinaryDigest.bytes &&
                                 length <= priorBinaryLength)))
          return Error::BadRecordOrder;
        haveBinaryOrder = true;
        priorBinaryDigest = digest;
        priorBinaryLength = length;
      }
      NodeIdKey nodeId{};
      std::copy_n(Find(common, 1)->value.data, nodeId.size(), nodeId.begin());
      if (nodes.find(nodeId) != nodes.end())
        return Error::BadRecordOrder;
      if (!sawDocument) {
        if (stream.kind == StreamKind::Capture &&
            nodeKind != NodeKind::Document)
          return Error::BadRecordOrder;
        sawDocument = true;
      }
      NodeOrderInfo info{nodeKind, storyKind, definitionKind};
      if (const ParsedField *parentField = Find(common, 3)) {
        info.parentPresent = true;
        std::copy_n(parentField->value.data, info.parent.size(),
                    info.parent.begin());
        const auto parent = nodes.find(info.parent);
        if (stream.kind == StreamKind::Capture &&
            (parent == nodes.end() ||
             !IsLegalPrimaryParent(nodeKind, parent->second.kind, storyKind,
                                   parent->second.story)))
          return Error::BadRecordOrder;
        const std::uint64_t sibling = R64(Find(common, 4)->value.data);
        const SiblingKey key{info.parent, nodeKind};
        const auto prior = lastSibling.find(key);
        if (prior != lastSibling.end() && sibling <= prior->second)
          return Error::BadRecordOrder;
        lastSibling[key] = sibling;
      }
      nodes.emplace(nodeId, info);
      currentNode = nodeId;
      currentNodePresent = true;
    } else if (kind == RecordKind::Edge) {
      if (!sawDocument || finalPhase || nodePhase > 4)
        return Error::BadRecordOrder;
      nodePhase = 4;
      ParsedFields edgeFields{};
      ParseFields({record.data + 24, record.size - 24},
                  R16(record.data + 6), &edgeFields);
      const EdgeKind edge =
          static_cast<EdgeKind>(R16(Find(edgeFields, 1)->value.data));
      NodeIdKey source{}, target{};
      std::copy_n(Find(edgeFields, 2)->value.data, source.size(),
                  source.begin());
      std::copy_n(Find(edgeFields, 3)->value.data, target.size(),
                  target.begin());
      const std::uint64_t ordinal = R64(Find(edgeFields, 4)->value.data);
      if (!currentNodePresent || source != currentNode)
        return Error::BadRecordOrder;
      if (stream.kind == StreamKind::Capture)
        pendingEdges.push_back({edge, source, target});
      const auto priorKind = priorEdgeKind.find(source);
      if (priorKind != priorEdgeKind.end() &&
          (edge < priorKind->second ||
           (edge == priorKind->second && ordinal <= priorEdgeOrdinal[source])))
        return Error::BadRecordOrder;
      priorEdgeKind[source] = edge;
      priorEdgeOrdinal[source] = ordinal;
    } else if (kind == RecordKind::Tombstone || kind == RecordKind::Remap) {
      if (stream.kind == StreamKind::QueryView || !sawDocument ||
          (kind == RecordKind::Tombstone && previous == RecordKind::Remap))
        return Error::BadRecordOrder;
      ParsedFields receiptFields{};
      ParseFields({record.data + 24, record.size - 24},
                  R16(record.data + 6), &receiptFields);
      NodeIdKey source{};
      std::copy_n(Find(receiptFields, 1)->value.data, source.size(),
                  source.begin());
      if ((source[6] & 0xf0U) != 0x40U ||
          (source[8] & 0xc0U) != 0x80U)
        return Error::IllegalStreamState;
      if (kind == RecordKind::Tombstone) {
        const std::uint64_t revision =
            R64(Find(receiptFields, 2)->value.data);
        const auto reason = static_cast<TombstoneReason>(
            R16(Find(receiptFields, 3)->value.data));
        const auto key = std::make_tuple(source, revision, reason);
        if ((haveTombstoneOrder && key <= priorTombstone) ||
            nodes.find(source) != nodes.end() ||
            !tombstones.emplace(source, reason).second)
          return Error::BadRecordOrder;
        priorTombstone = key;
        haveTombstoneOrder = true;
      } else {
        const ParsedField *targetField = Find(receiptFields, 2);
        ReceiptRemap remap;
        remap.targetPresent = targetField != nullptr;
        if (targetField != nullptr)
          std::copy_n(targetField->value.data, remap.target.size(),
                      remap.target.begin());
        remap.disposition = static_cast<RemapDisposition>(
            Find(receiptFields, 3)->value.data[0]);
        remap.reason = static_cast<RemapReason>(
            R16(Find(receiptFields, 4)->value.data));
        const auto key = std::make_tuple(
            source, remap.disposition, remap.targetPresent, remap.target,
            remap.reason);
        if (haveRemapOrder && key <= priorRemap)
          return Error::BadRecordOrder;
        remaps[source].push_back(remap);
        if (remap.targetPresent &&
            (nodes.find(remap.target) == nodes.end() ||
             !claimedRemapTargets.emplace(remap.target, source).second))
          return Error::IllegalStreamState;
        const bool self = remap.targetPresent && source == remap.target;
        bool validSemantics = false;
        switch (remap.disposition) {
        case RemapDisposition::Retained:
          validSemantics = self &&
              (remap.reason == RemapReason::IdentityRetained ||
               remap.reason == RemapReason::Split ||
               remap.reason == RemapReason::Coalesce ||
               remap.reason == RemapReason::Move ||
               remap.reason == RemapReason::Merge ||
               remap.reason == RemapReason::TableCellSplit ||
               remap.reason == RemapReason::SaveReopenEqual ||
               remap.reason == RemapReason::ExternalUnique);
          break;
        case RemapDisposition::New:
          validSemantics = remap.targetPresent &&
              ((self &&
                (remap.reason == RemapReason::Split ||
                 remap.reason == RemapReason::ExternalUnique ||
                 remap.reason == RemapReason::Ambiguous)) ||
               (!self &&
                ((remap.reason == RemapReason::ClientInsert &&
                  nodes.find(source) == nodes.end()) ||
                 ((remap.reason == RemapReason::Clone ||
                   remap.reason == RemapReason::TableCellSplit) &&
                  nodes.find(source) != nodes.end()))));
          break;
        case RemapDisposition::Tombstoned:
          validSemantics = !remap.targetPresent &&
              (remap.reason == RemapReason::Delete ||
               remap.reason == RemapReason::Coalesce ||
               remap.reason == RemapReason::Merge);
          break;
        case RemapDisposition::Ambiguous:
          validSemantics = !remap.targetPresent &&
              remap.reason == RemapReason::Ambiguous;
          break;
        }
        if (!validSemantics)
          return Error::IllegalStreamState;
        priorRemap = key;
        haveRemapOrder = true;
      }
      finalPhase = true;
    } else {
      const std::uint8_t phase = kind == RecordKind::Property     ? 1
                                 : kind == RecordKind::Coverage   ? 2
                                 : kind == RecordKind::Diagnostic ? 3
                                 : kind == RecordKind::AssetChunk ? 5
                                                                  : 255;
      if (!sawDocument || !currentNodePresent || finalPhase ||
          phase == 255 || phase < nodePhase)
        return Error::BadRecordOrder;
      ParsedFields fields{};
      ParseFields({record.data + 24, record.size - 24},
                  R16(record.data + 6), &fields);
      NodeOrderInfo &ownerInfo = nodes.find(currentNode)->second;
      if (kind == RecordKind::Property) {
        NodeIdKey recordOwner{};
        std::copy_n(Find(fields, 1)->value.data, recordOwner.size(),
                    recordOwner.begin());
        if (recordOwner != currentNode)
          return Error::BadRecordOrder;
        const std::pair<FieldTag, PropertyKeyId> key{
            R16(Find(fields, 2)->value.data),
            R32(Find(fields, 3)->value.data)};
        if (havePropertyKey && key <= priorPropertyKey)
          return Error::BadRecordOrder;
        priorPropertyKey = key;
        havePropertyKey = true;
        const PropertyRule *rule = FindPropertyRule(key.second);
        if (ownerInfo.kind == NodeKind::CharacterRun &&
            key.first == 102 && rule != nullptr &&
            rule->applicabilitySet ==
                PropertyApplicabilitySet::CharacterShape)
          ownerInfo.characterShapePropertySeen = true;
      } else if (kind == RecordKind::Coverage) {
        const ParsedField *owner = Find(fields, 1);
        const bool ownerPresent = owner != nullptr;
        NodeIdKey recordOwner{};
        if (ownerPresent)
          std::copy_n(owner->value.data, recordOwner.size(),
                      recordOwner.begin());
        if ((ownerPresent && recordOwner != currentNode) ||
            (!ownerPresent && ownerInfo.kind != NodeKind::Document))
          return Error::BadRecordOrder;
        const std::uint8_t profile = Find(fields, 2)->value.data[0];
        const auto ownerKind = static_cast<RecordKind>(
            R16(Find(fields, 6)->value.data));
        const FieldTag ownerField = R16(Find(fields, 7)->value.data);
        const ParsedField *property = Find(fields, 4);
        if (ownerKind == RecordKind::Node && ownerField != 0 &&
            !IsNodeCoverageField(ownerInfo.kind, ownerField))
          return Error::BadRecordOrder;
        if (ownerInfo.kind == NodeKind::CharacterRun && ownerPresent &&
            ownerKind == RecordKind::Node && ownerField == 102 &&
            profile == static_cast<std::uint8_t>(ProfileId::EditableText)) {
          const auto state = static_cast<CoverageState>(
              Find(fields, 3)->value.data[0]);
          ++ownerInfo.characterShapeCoverageCount;
          switch (state) {
          case CoverageState::Complete:
            ++ownerInfo.characterShapeCompleteCoverageCount;
            break;
          case CoverageState::NotExposed:
            ++ownerInfo.characterShapeNotExposedCoverageCount;
            break;
          case CoverageState::ReadFailed:
            ++ownerInfo.characterShapeReadFailedCoverageCount;
            break;
          case CoverageState::NotApplicable:
          case CoverageState::NotRequested:
          case CoverageState::ProjectionOmitted:
            ++ownerInfo.characterShapeContradictoryCoverageCount;
            break;
          }
        }
        if (ownerKind == RecordKind::Property) {
          const PropertyRule *rule =
              FindPropertyRule(R32(property->value.data));
          const ProfileId profileId = static_cast<ProfileId>(profile);
          if (rule == nullptr ||
              (rule->profileBits & ProfileBit(profileId)) == 0 ||
              !IsPropertyApplicable(
                  *rule, ownerInfo.kind, ownerField,
                  ownerInfo.kind == NodeKind::Definition,
                  ownerInfo.definition))
            return Error::BadRecordOrder;
        }
        Bytes key;
        U8(&key, ownerPresent ? 1U : 0U);
        U16(&key, static_cast<std::uint16_t>(ownerKind));
        U16(&key, ownerField);
        U8(&key, profile);
        U8(&key, property != nullptr ? 1U : 0U);
        if (property != nullptr)
          U32(&key, R32(property->value.data));
        U8(&key, Find(fields, 3)->value.data[0]);
        Add(&key, Find(fields, 5)->value);
        if (haveCoverageKey && key <= priorCoverageKey)
          return Error::BadRecordOrder;
        priorCoverageKey = std::move(key);
        haveCoverageKey = true;
      } else if (kind == RecordKind::Diagnostic) {
        const ParsedField *owner = Find(fields, 3);
        NodeIdKey recordOwner{};
        if (owner != nullptr)
          std::copy_n(owner->value.data, recordOwner.size(),
                      recordOwner.begin());
        if ((owner != nullptr && recordOwner != currentNode) ||
            (owner == nullptr && ownerInfo.kind != NodeKind::Document))
          return Error::BadRecordOrder;
        Bytes key;
        U32(&key, R32(Find(fields, 1)->value.data));
        U8(&key, Find(fields, 2)->value.data[0]);
        if (const ParsedField *property = Find(fields, 4)) {
          U8(&key, 1);
          U32(&key, R32(property->value.data));
        } else {
          U8(&key, 0);
        }
        Add(&key, Find(fields, 5)->value);
        Add(&key, Find(fields, 6)->value);
        if (haveDiagnosticKey && key <= priorDiagnosticKey)
          return Error::BadRecordOrder;
        priorDiagnosticKey = std::move(key);
        haveDiagnosticKey = true;
      }
      nodePhase = phase;
    }
    previous = kind;
    if (parsed != nullptr) {
      ParsedFields outerDescriptors{};
      error = ParseFields({record.data + 24, record.size - 24},
                          R16(record.data + 6), &outerDescriptors);
      if (error != Error::None) return error;
      if (descriptorArena != nullptr) {
        AppendParsedFields(outerDescriptors, descriptorArena, &retained.outer);
        if (kind == RecordKind::Node) {
          ParsedFields commonDescriptors{}, payloadDescriptors{};
          error = ParseNested(Find(outerDescriptors, 1)->value,
                              &commonDescriptors);
          if (error != Error::None) return error;
          error = ParseNested(Find(outerDescriptors, 2)->value,
                              &payloadDescriptors);
          if (error != Error::None) return error;
          AppendParsedFields(commonDescriptors, descriptorArena,
                             &retained.common);
          AppendParsedFields(payloadDescriptors, descriptorArena,
                             &retained.payload);
        }
      }
      ++gDescriptorRecordBuilds;
      parsed->push_back(std::move(retained));
      if (!parsed->back().owned.empty())
        parsed->back().bytes = View(parsed->back().owned);
    }
    offset += recordBytes;
    if (*recordCount == (std::numeric_limits<std::uint64_t>::max)())
      return Error::LengthOverflow;
    ++*recordCount;
  }
  if (stream.kind == StreamKind::Capture) {
    for (const auto &group : remaps) {
      std::size_t retainedTableCellSplit = 0;
      std::size_t newTableCellSplit = 0;
      bool allTableCellSplit = true;
      for (const ReceiptRemap &member : group.second) {
        allTableCellSplit = allTableCellSplit &&
            member.reason == RemapReason::TableCellSplit;
        if (member.disposition == RemapDisposition::Retained &&
            member.reason == RemapReason::TableCellSplit)
          ++retainedTableCellSplit;
        if (member.disposition == RemapDisposition::New &&
            member.reason == RemapReason::TableCellSplit)
          ++newTableCellSplit;
      }
      const bool completeTableCellSplit = allTableCellSplit &&
          retainedTableCellSplit == 1 && newTableCellSplit >= 1 &&
          group.second.size() ==
              retainedTableCellSplit + newTableCellSplit;
      if ((group.second.size() != 1 || allTableCellSplit) &&
          !completeTableCellSplit)
        return Error::IllegalStreamState;
    }
    for (const auto &tombstone : tombstones) {
      const auto group = remaps.find(tombstone.first);
      if (group == remaps.end() || group->second.size() != 1)
        return Error::IllegalStreamState;
      const ReceiptRemap &remap = group->second.front();
      if (remap.disposition != RemapDisposition::Tombstoned &&
          remap.disposition != RemapDisposition::Ambiguous)
        return Error::IllegalStreamState;
      const bool reasonMatches =
          (tombstone.second == TombstoneReason::Delete &&
           remap.reason == RemapReason::Delete) ||
          (tombstone.second == TombstoneReason::Coalesce &&
           remap.reason == RemapReason::Coalesce) ||
          (tombstone.second == TombstoneReason::MergeAbsorbed &&
           remap.reason == RemapReason::Merge) ||
          (tombstone.second == TombstoneReason::ExternalUnmatched &&
           ((remap.disposition == RemapDisposition::Tombstoned &&
             remap.reason == RemapReason::Delete) ||
            (remap.disposition == RemapDisposition::Ambiguous &&
             remap.reason == RemapReason::Ambiguous)));
      if (!reasonMatches)
        return Error::IllegalStreamState;
    }
    for (const auto &group : remaps) {
      const bool terminal = group.second.size() == 1 &&
          (group.second.front().disposition ==
               RemapDisposition::Tombstoned ||
           group.second.front().disposition == RemapDisposition::Ambiguous);
      if (terminal !=
          (tombstones.find(group.first) != tombstones.end()))
        return Error::IllegalStreamState;
    }
    for (const PendingEdge &edge : pendingEdges) {
      const auto source = nodes.find(edge.source),
                 target = nodes.find(edge.target);
      if (source == nodes.end() || target == nodes.end() ||
          !IsLegalReferenceEdge(edge.kind, source->second.kind,
                                target->second.kind, source->second.definition,
                                target->second.definition, source->second.story,
                                target->second.story))
        return Error::BadRecordOrder;
      if (edge.kind == EdgeKind::Contains &&
          (!target->second.parentPresent ||
           target->second.parent != edge.source ||
           ++target->second.containsCount != 1))
        return Error::BadRecordOrder;
      if (edge.kind == EdgeKind::Anchors &&
          (!source->second.parentPresent ||
           source->second.parent != edge.target ||
           target->second.kind != NodeKind::Paragraph ||
           ++source->second.anchorsCount != 1))
        return Error::BadRecordOrder;
      if (edge.kind == EdgeKind::CharacterShapeRef &&
          ++source->second.characterShapeRefCount > 1)
        return Error::BadRecordOrder;
    }
    for (const auto &node : nodes) {
      const bool controlFamily =
          node.second.kind == NodeKind::GenericControl ||
          node.second.kind == NodeKind::Table ||
          node.second.kind == NodeKind::Image;
      if ((node.second.kind == NodeKind::Document &&
           (node.second.parentPresent || node.second.containsCount != 0)) ||
          (node.second.kind != NodeKind::Document &&
           (!node.second.parentPresent || node.second.containsCount != 1)) ||
          (controlFamily && node.second.anchorsCount != 1) ||
          (!controlFamily && node.second.anchorsCount != 0) ||
          (node.second.kind == NodeKind::CharacterRun &&
           ([&node]() {
             const bool completeMode =
                 node.second.characterShapeCoverageCount == 1 &&
                 node.second.characterShapeCompleteCoverageCount == 1 &&
                 node.second.characterShapeContradictoryCoverageCount == 0;
             const bool terminalMode =
                 node.second.characterShapeCoverageCount == 1 &&
                 (node.second.characterShapeNotExposedCoverageCount == 1 ||
                  node.second.characterShapeReadFailedCoverageCount == 1) &&
                 node.second.characterShapeContradictoryCoverageCount == 0;
             return (!completeMode && !terminalMode) ||
                 (completeMode &&
                  (node.second.characterShapeRefCount != 1 ||
                   !node.second.characterShapePropertySeen)) ||
                 (terminalMode &&
                  (node.second.characterShapeRefCount != 0 ||
                   node.second.characterShapePropertySeen));
           })()))
        return Error::BadRecordOrder;
    }
  }
  return *recordCount != 0 && sawDocument && *recordCount == declaredRecords
             ? Error::None
             : Error::BadRecordOrder;
}
} // namespace

Error ValidateRecordStream(const RecordStream &stream,
                           std::uint64_t *recordCount) noexcept {
  gLegacyValidatorWalks.fetch_add(1, std::memory_order_relaxed);
  return WalkParsedRecordStream(stream, recordCount, nullptr, nullptr);
}

void ResetCodecDebugCounters() noexcept {
  gParsedRecordWalks.store(0, std::memory_order_relaxed);
  gLegacyValidatorWalks.store(0, std::memory_order_relaxed);
  gSourceBytesRead.store(0, std::memory_order_relaxed);
  gRetainedRecordCopies.store(0, std::memory_order_relaxed);
  gBlobHashOperations.store(0, std::memory_order_relaxed);
  gBlobHashBufferBytes.store(0, std::memory_order_relaxed);
  gDescriptorRecordBuilds = 0;
  gDescriptorRecordReuses = 0;
  gDescriptorFields = 0;
  gFieldDecodeWalks = 0;
  gDescriptorArenaGrowths = 0;
  gCanonicalWalkQpc = 0;
  gAuthorityPassQpc = 0;
  gSemanticPassQpc = 0;
  gValidatedCanonicalPostWalks.store(0, std::memory_order_relaxed);
  gIncrementalAccumulatorFinalizations.store(0, std::memory_order_relaxed);
}

void SetCanonicalDescriptorReuseForTesting(const bool enabled) noexcept {
  gCanonicalDescriptorReuseEnabled = enabled;
}

CodecDebugCounters ReadCodecDebugCounters() noexcept {
  return {
      gParsedRecordWalks.load(std::memory_order_relaxed),
      gLegacyValidatorWalks.load(std::memory_order_relaxed),
      gSourceBytesRead.load(std::memory_order_relaxed),
      gRetainedRecordCopies.load(std::memory_order_relaxed),
      gBlobHashOperations.load(std::memory_order_relaxed),
      gBlobHashBufferBytes.load(std::memory_order_relaxed),
      gDescriptorRecordBuilds,
      gDescriptorRecordReuses,
      gDescriptorFields,
      gFieldDecodeWalks,
      gDescriptorArenaGrowths,
      gCanonicalWalkQpc,
      gAuthorityPassQpc,
      gSemanticPassQpc,
      gValidatedCanonicalPostWalks.load(std::memory_order_relaxed),
      gIncrementalAccumulatorFinalizations.load(std::memory_order_relaxed),
  };
}

namespace {
using UuidKey = std::array<std::uint8_t, 16>;
struct Authority final {
  NodeKind kind = NodeKind::Document;
  CanonicalPath path{};
  std::uint64_t ordinal = 0;
};
using AuthorityMap = std::unordered_map<UuidKey, Authority, UuidKeyHash>;
struct PropertyBag final {
  FieldTag field = 0;
  std::vector<RecordId> ids{};
  std::size_t consumed = 0;
};
struct StreamingHash final {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  Bytes object{};
  StreamingHash() = default;
  StreamingHash(const StreamingHash &) = delete;
  StreamingHash &operator=(const StreamingHash &) = delete;
  StreamingHash(StreamingHash &&other) noexcept
      : algorithm(other.algorithm), hash(other.hash),
        object(std::move(other.object)) {
    other.algorithm = nullptr;
    other.hash = nullptr;
  }
  StreamingHash &operator=(StreamingHash &&other) noexcept {
    if (this != &other) {
      Reset();
      algorithm = other.algorithm;
      hash = other.hash;
      object = std::move(other.object);
      other.algorithm = nullptr;
      other.hash = nullptr;
    }
    return *this;
  }
  ~StreamingHash() { Reset(); }
  void Reset() noexcept {
    if (hash != nullptr)
      BCryptDestroyHash(hash);
    if (algorithm != nullptr)
      BCryptCloseAlgorithmProvider(algorithm, 0);
    hash = nullptr;
    algorithm = nullptr;
    object.clear();
  }
  bool Initialize() {
    DWORD length = 0, returned = 0;
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM,
                                    nullptr, 0) < 0 ||
        BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                          reinterpret_cast<PUCHAR>(&length), sizeof(length),
                          &returned, 0) < 0)
      return false;
    object.resize(length);
    return BCryptCreateHash(algorithm, &hash, object.data(), length, nullptr, 0,
                            0) >= 0;
  }
  bool Add(ByteView bytes) {
    return bytes.size <= MAXDWORD &&
           BCryptHashData(hash, const_cast<PUCHAR>(bytes.data),
                          static_cast<ULONG>(bytes.size), 0) >= 0;
  }
  bool Finish(Sha256 *digest) {
    return BCryptFinishHash(hash, digest->bytes.data(),
                            static_cast<ULONG>(digest->bytes.size()), 0) >= 0;
  }
};
struct WorkFrame final {
  UuidKey id{};
  CanonicalPath path{};
  NodeKind kind = NodeKind::Document;
  std::uint64_t sibling = 0;
  DefinitionKind definitionKind = DefinitionKind::Style;
  bool nativeDefinitionValue = false;
  SemanticPayloadInput nativeDefinition{};
  bool declaredDefinitionAggregatePresent = false;
  Sha256 declaredDefinitionAggregate{};
  std::uint64_t binaryLength = 0;
  Sha256 binaryDigest{};
  StreamingHash binaryContent{};
  std::uint64_t binaryContentLength = 0;
  bool sawAssetChunk = false;
  std::vector<SemanticPayloadInput> payloads{};
  std::vector<SemanticPropertyInput> properties{};
  std::vector<Sha256> children{};
  std::vector<ReferenceInput> references{};
  std::vector<PropertyBag> propertyBags{};
  FieldTag priorPropertyField = 0;
  PropertyKeyId priorPropertyKey = 0;
  bool hasProperty = false;
  std::vector<PropertyKeyId> propertyValueKeys{};
  std::vector<PropertyKeyId> conditionalNotApplicableKeys{};
};
UuidKey ReadUuid(ByteView value) {
  UuidKey key{};
  if (value.size == key.size())
    std::copy_n(value.data, key.size(), key.begin());
  return key;
}
ContentId ReadContentId(const std::uint8_t *bytes) {
  ContentId id{};
  std::copy_n(bytes, id.bytes.size(), id.bytes.begin());
  return id;
}
bool IsZero(const UuidKey &key) {
  for (std::uint8_t byte : key)
    if (byte != 0)
      return false;
  return true;
}
struct BlobHashWorkspace final {
  ~BlobHashWorkspace() {
    if (algorithm != nullptr)
      BCryptCloseAlgorithmProvider(algorithm, 0);
  }
  bool Prepare(const std::uint32_t bufferBytes) {
    DWORD returned = 0;
    DWORD objectBytes = 0;
    if (algorithm == nullptr &&
        BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM,
                                    nullptr, 0) < 0)
      return false;
    if (object.empty()) {
      if (BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                            reinterpret_cast<PUCHAR>(&objectBytes),
                            sizeof(objectBytes), &returned, 0) < 0 ||
          objectBytes == 0)
        return false;
      object.resize(objectBytes);
    }
    if (buffer.size() < bufferBytes)
      buffer.resize(bufferBytes);
    return true;
  }
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  Bytes object{};
  Bytes buffer{};
};

Error DereferenceBlob(const CanonicalizationInput &input, ByteView slice,
                      bool rawContent, Bytes *transformed) {
  if (slice.size != 64)
    return Error::BadScalar;
  const std::uint64_t physicalOffset = R64(slice.data + 16);
  const std::uint64_t length = R64(slice.data + 24);
  Sha256 expected{};
  std::copy_n(slice.data + 32, expected.bytes.size(), expected.bytes.begin());
  if (input.readBlob == nullptr)
    return Error::BadScalar;
  auto* const workspace =
      static_cast<BlobHashWorkspace*>(input.blobHashWorkspace);
  if (workspace == nullptr) return Error::BadScalar;
  BCRYPT_HASH_HANDLE hash = nullptr;
  constexpr std::uint32_t kMaximumChunk = 1U << 20;
  const std::uint32_t bufferBytes = static_cast<std::uint32_t>(
      (std::max<std::uint64_t>)(
          1, (std::min<std::uint64_t>)(length, kMaximumChunk)));
  gBlobHashOperations.fetch_add(1, std::memory_order_relaxed);
  gBlobHashBufferBytes.fetch_add(bufferBytes, std::memory_order_relaxed);
  bool ok = workspace->Prepare(bufferBytes) &&
            BCryptCreateHash(
                workspace->algorithm, &hash, workspace->object.data(),
                static_cast<ULONG>(workspace->object.size()), nullptr, 0, 0) >=
                0;
  const ContentId contentId = ReadContentId(slice.data);
  if (rawContent && length > static_cast<std::uint64_t>(
                                 (std::numeric_limits<std::size_t>::max)()))
    ok = false;
  if (ok && rawContent)
    transformed->clear();
  std::uint64_t done = 0;
  while (ok && done != length) {
    const std::uint32_t wanted = static_cast<std::uint32_t>(
        (std::min<std::uint64_t>)(length - done, bufferBytes));
    std::uint32_t actual = 0;
    ok = input.readBlob(
             input.blobContext, contentId, physicalOffset + done,
             workspace->buffer.data(), wanted, &actual) &&
         actual == wanted &&
         BCryptHashData(hash, workspace->buffer.data(), actual, 0) >= 0;
    if (ok && rawContent)
      transformed->insert(
          transformed->end(), workspace->buffer.data(),
          workspace->buffer.data() + actual);
    done += actual;
  }
  Sha256 actualDigest{};
  ok =
      ok &&
      BCryptFinishHash(hash, actualDigest.bytes.data(),
                       static_cast<ULONG>(actualDigest.bytes.size()), 0) >= 0 &&
      Equal(expected, actualDigest);
  if (hash != nullptr)
    BCryptDestroyHash(hash);
  if (!ok)
    return Error::BadScalar;
  if (input.blobClosure != nullptr) {
    CanonicalBlobSlice planned{};
    planned.contentId = contentId;
    planned.offset = physicalOffset;
    planned.length = length;
    planned.digest = expected;
    const auto duplicate = std::find_if(
        input.blobClosure->begin(), input.blobClosure->end(),
        [&planned](const CanonicalBlobSlice &prior) {
          return prior.contentId.bytes == planned.contentId.bytes &&
                 prior.offset == planned.offset && prior.length == planned.length &&
                 Equal(prior.digest, planned.digest);
        });
    if (duplicate == input.blobClosure->end())
      input.blobClosure->push_back(planned);
  }
  if (!rawContent) {
    *transformed = Uint64(length);
    transformed->insert(transformed->end(), expected.bytes.begin(),
                        expected.bytes.end());
  }
  return Error::None;
}
Error TransformSemanticValue(const CanonicalizationInput &input,
                             ScalarTag scalar, ByteView value,
                             const AuthorityMap &authority,
                             Bytes *transformed) {
  if (scalar == ScalarTag::BlobSlice)
    return DereferenceBlob(input, value, false, transformed);
  if (scalar == ScalarTag::UUID128) {
    const auto target = authority.find(ReadUuid(value));
    if (target == authority.end())
      return Error::BadRecordOrder;
    if (target->second.kind == NodeKind::Definition ||
        target->second.kind == NodeKind::BinaryData)
      *transformed = Uint64(target->second.ordinal);
    else
      *transformed = EncodePath(target->second.path);
    return Error::None;
  }
  if (value.size >
      static_cast<std::uint64_t>((std::numeric_limits<std::size_t>::max)()))
    return Error::LengthOverflow;
  transformed->assign(value.data,
                      value.data + static_cast<std::size_t>(value.size));
  return Error::None;
}
Error TransformPayloadValue(const CanonicalizationInput &input, NodeKind kind,
                            FieldTag tag, ScalarTag scalar, ByteView value,
                            const AuthorityMap &authority,
                            Bytes *transformed) {
  const bool paragraphRange =
      (kind == NodeKind::CharacterRun && tag == 100) ||
      (kind == NodeKind::SpecialCharacter && tag == 100) ||
      (kind == NodeKind::GeneratedText && tag == 100);
  if (paragraphRange) {
    if (value.size != 32)
      return Error::BadNestedStruct;
    const auto paragraph = authority.find(ReadUuid({value.data, 16}));
    if (paragraph == authority.end() ||
        paragraph->second.kind != NodeKind::Paragraph)
      return Error::BadRecordOrder;
    *transformed = EncodePath(paragraph->second.path);
    transformed->insert(transformed->end(), value.data + 16, value.data + 32);
    return Error::None;
  }
  if (kind == NodeKind::Image && tag == 110) {
    if (value.size != 56)
      return Error::BadNestedStruct;
    *transformed = Uint64(R64(value.data + 16));
    transformed->insert(transformed->end(), value.data + 24, value.data + 56);
    return Error::None;
  }
  return TransformSemanticValue(input, scalar, value, authority, transformed);
}
ByteView ObservationValue(const ParsedField &field, ObservationState *state) {
  *state = static_cast<ObservationState>(field.value.data[0]);
  if (*state != ObservationState::Value)
    return {};
  const std::uint64_t detail = R64(field.value.data + 8);
  return {field.value.data + 24 + detail * 2, R64(field.value.data + 16)};
}
const NodeFieldRule *PayloadRule(NodeKind kind, FieldTag tag) {
  for (const auto &rule : kNodePayloadFieldRules)
    if (rule.node == kind && rule.tag == tag)
      return &rule;
  if (kind == NodeKind::Table || kind == NodeKind::Image)
    for (const auto &rule : kNodePayloadFieldRules)
      if (rule.node == NodeKind::GenericControl && rule.tag == tag)
        return &rule;
  return nullptr;
}
const NodeFieldRule *OwnerFieldRule(NodeKind kind, FieldTag tag) {
  for (const auto &rule : kCommonNodeFieldRules)
    if (rule.tag == tag)
      return &rule;
  return PayloadRule(kind, tag);
}
bool ResolvePropertyOwningProfile(
    const NodeKind kind, const FieldTag ownerField,
    const DefinitionKind definitionKind, ProfileId *profile) {
  const NodeFieldRule *const ownerRule = OwnerFieldRule(kind, ownerField);
  if (ownerRule == nullptr || profile == nullptr)
    return false;
  *profile = ResolveNodeFieldProfile(*ownerRule, definitionKind);
  return true;
}
void AddPropertyBag(WorkFrame *frame, FieldTag field,
                    const ParsedField &array) {
  PropertyBag bag{};
  bag.field = field;
  std::uint64_t at = 16;
  for (std::uint64_t index = 0; index < array.count; ++index) {
    const std::uint64_t length = R64(array.value.data + at);
    at += 8;
    if (length == 8)
      bag.ids.push_back(R64(array.value.data + at));
    at += length;
  }
  frame->propertyBags.push_back(std::move(bag));
}
Error FinishFrame(
    std::vector<WorkFrame> *stack,
    Sha256 *document,
    const std::uint64_t profiles,
    bool *semanticComplete,
    bool *layoutComplete,
    std::vector<NodeFingerprintResult> *fingerprints) {
  if (stack->empty())
    return Error::BadRecordOrder;
  WorkFrame frame = std::move(stack->back());
  for (const PropertyBag &bag : frame.propertyBags)
    if (bag.consumed != bag.ids.size())
      return Error::BadRecordOrder;
  for (const PropertyRule &rule : kPropertyRegistryV1) {
    FieldTag ownerField = 102;
    if (rule.applicabilitySet == PropertyApplicabilitySet::DocumentMetadata)
      ownerField = 100;
    else if (rule.applicabilitySet == PropertyApplicabilitySet::CharacterShape)
      ownerField = frame.kind == NodeKind::Paragraph ? 104 : 102;
    else if (rule.applicabilitySet == PropertyApplicabilitySet::ParagraphShape)
      ownerField = frame.kind == NodeKind::Paragraph ? 103 : 102;
    else if (rule.applicabilitySet == PropertyApplicabilitySet::Control ||
             rule.applicabilitySet == PropertyApplicabilitySet::Table)
      ownerField = 106;
    else if (rule.applicabilitySet == PropertyApplicabilitySet::Cell)
      ownerField = 109;
    else if (rule.applicabilitySet == PropertyApplicabilitySet::Image)
      ownerField = 112;
    else if (rule.applicabilitySet == PropertyApplicabilitySet::ImageBinary)
      ownerField = 11;
    else if (rule.applicabilitySet == PropertyApplicabilitySet::LayoutAll ||
             rule.applicabilitySet == PropertyApplicabilitySet::LayoutControl)
      ownerField = 10;
    if (rule.requirement != RequirementClass::RequiredWhenApplicable ||
        !IsPropertyApplicable(rule, frame.kind, ownerField,
                              frame.kind == NodeKind::Definition,
                              frame.definitionKind))
      continue;
    ProfileId owningProfile{};
    if (!ResolvePropertyOwningProfile(frame.kind, ownerField,
                                      frame.definitionKind, &owningProfile))
      return Error::BadScalar;
    const bool owningProfileRequested =
        (profiles & ProfileBit(owningProfile)) != 0;
    const bool semanticRequired =
        owningProfile != ProfileId::Layout && owningProfileRequested &&
        rule.root == RootClass::Semantic &&
        (rule.profileBits & ProfileBit(owningProfile)) != 0;
    const bool layoutRequired =
        owningProfile == ProfileId::Layout && owningProfileRequested;
    if (!semanticRequired && !layoutRequired)
      continue;
    const bool hasValue=
        std::find(frame.propertyValueKeys.begin(),frame.propertyValueKeys.end(),
                  rule.id)!=frame.propertyValueKeys.end();
    const bool conditionExcluded=
        HasQualifier(rule.qualifiers,QualifierFlags::WindowsBrushFillCondition) &&
        std::find(frame.conditionalNotApplicableKeys.begin(),
                  frame.conditionalNotApplicableKeys.end(),rule.id)!=
            frame.conditionalNotApplicableKeys.end();
    if (!hasValue && !conditionExcluded) {
      if (semanticRequired)
        *semanticComplete = false;
      if (layoutRequired)
        *layoutComplete = false;
    }
  }
  stack->pop_back();
  Sha256 fingerprint{};
  if (frame.kind == NodeKind::BinaryData) {
    Sha256 actual{};
    if (!frame.sawAssetChunk ||
        frame.binaryContentLength != frame.binaryLength ||
        !frame.binaryContent.Finish(&actual) ||
        !Equal(actual, frame.binaryDigest))
      return Error::BadScalar;
  }
  if (frame.kind == NodeKind::Definition) {
    const Sha256 aggregate = DefinitionPropertyAggregate(frame.properties);
    if (!frame.declaredDefinitionAggregatePresent ||
        !Equal(aggregate, frame.declaredDefinitionAggregate))
      return Error::BadScalar;
    fingerprint = DefinitionFingerprint(
        frame.definitionKind,
        frame.nativeDefinitionValue ? &frame.nativeDefinition : nullptr,
        aggregate, frame.references);
  } else if (frame.kind == NodeKind::BinaryData) {
    fingerprint = BinaryFingerprint(frame.binaryLength, frame.binaryDigest);
  } else {
    NodeFingerprintInput node{frame.path,      frame.kind,       frame.sibling,
                              frame.payloads,  frame.properties, frame.children,
                              frame.references};
    fingerprint = NodeFingerprint(std::move(node));
  }
  if (fingerprints != nullptr) {
    NodeFingerprintResult computed;
    std::copy(frame.id.begin(), frame.id.end(), computed.nodeId.bytes.begin());
    computed.fingerprint = fingerprint;
    fingerprints->push_back(computed);
  }
  if (stack->empty()) {
    if (frame.kind != NodeKind::Document)
      return Error::BadRecordOrder;
    *document = fingerprint;
  } else {
    stack->back().children.push_back(fingerprint);
  }
  return Error::None;
}
CanonicalStreamResult StreamResult(const std::vector<Bytes> &items) {
  const Bytes stream = PackedItems(items);
  return {static_cast<std::uint64_t>(items.size()),
          static_cast<std::uint64_t>(stream.size()), Hash(View(stream))};
}
Error DigestParsedRecordStream(
    const std::vector<ParsedRecordWalkerRecord> &records,
    const std::uint64_t byteLength, CanonicalStreamResult *result) {
  StreamingHash hash;
  if (!hash.Initialize())
    return Error::BadScalar;
  for (const auto &record : records)
    if (!hash.Add(record.bytes))
      return Error::BadScalar;
  result->itemCount = records.size();
  result->byteLength = byteLength;
  return hash.Finish(&result->digest) ? Error::None : Error::BadScalar;
}
} // namespace

class CanonicalArtifact final {
public:
  CanonicalArtifact(const CanonicalArtifact &) = delete;
  CanonicalArtifact &operator=(const CanonicalArtifact &) = delete;

private:
  friend Error CreateCanonicalArtifact(const CanonicalizationInput &,
                                       CanonicalArtifactPin *) noexcept;
  friend Error CreateCanonicalArtifactFromFacts(
      const RecordStream &, CanonicalizationResult,
      CanonicalArtifactPin *) noexcept;
  friend const CanonicalizationResult *CanonicalArtifactFacts(
      const CanonicalArtifactPin &) noexcept;
  friend bool CanonicalArtifactSourceMatches(
      const CanonicalArtifactPin &, const RecordStream &) noexcept;
  friend void *CanonicalArtifactSourceContext(
      const CanonicalArtifactPin &) noexcept;
  CanonicalArtifact(const RecordStream &source,
                    CanonicalizationResult facts) noexcept
      : source_(source), facts_(std::move(facts)) {}
  RecordStream source_{};
  CanonicalizationResult facts_{};
};

Error CanonicalizeRecordStream(const CanonicalizationInput &supplied,
                               CanonicalizationResult *result) noexcept {
  if (result == nullptr)
    return Error::BadFlags;
  *result = {};
  CanonicalizationInput input = supplied;
  BlobHashWorkspace blobHashWorkspace;
  input.blobClosure = &result->blobClosure;
  input.blobHashWorkspace = &blobHashWorkspace;
  result->streamKind = input.records.kind;
  std::uint64_t recordCount = 0;
  std::vector<ParsedRecordWalkerRecord> parsedRecords;
  std::vector<ParsedField> descriptorArena;
  LARGE_INTEGER stageStarted{}, stageFinished{};
  QueryPerformanceCounter(&stageStarted);
  gParsedRecordWalks.fetch_add(1, std::memory_order_relaxed);
  if (!input.records.incremental)
    gValidatedCanonicalPostWalks.fetch_add(1, std::memory_order_relaxed);
  Error error = WalkParsedRecordStream(
      input.records, &recordCount, &parsedRecords, &descriptorArena);
  QueryPerformanceCounter(&stageFinished);
  gCanonicalWalkQpc += static_cast<std::uint64_t>(
      stageFinished.QuadPart - stageStarted.QuadPart);
  if (error != Error::None)
    return error;
  if (input.records.kind == StreamKind::QueryView) {
    error = DigestParsedRecordStream(parsedRecords, input.records.length,
                                     &result->recordStream);
    if (error != Error::None || parsedRecords.empty())
      return error != Error::None ? error : Error::BadRecordOrder;
    const ParsedRecordWalkerRecord &manifest = parsedRecords.front();
    ParsedFields decodedFields{};
    ParsedFieldSpan fields{};
    if (gCanonicalDescriptorReuseEnabled) {
      fields = DescriptorSpan(descriptorArena, manifest.outer);
      ++gDescriptorRecordReuses;
    } else {
      error = ParseFields({manifest.bytes.data + 24,
                           manifest.bytes.size - 24},
                          R16(manifest.bytes.data + 6), &decodedFields);
      if (error != Error::None) return error;
      fields = {decodedFields.fields.data(), decodedFields.count};
    }
    const ByteView version = Find(fields, 1)->value;
    if (version.size != kGraphVersionBytesV1 || version.data[10] > 1 ||
        version.data[11] > 1 ||
        (version.data[11] != 0 && version.data[10] == 0))
      return Error::IllegalLayoutState;
    std::copy_n(Find(fields, 5)->value.data,
                result->viewIndexDigest.bytes.size(),
                result->viewIndexDigest.bytes.begin());
    result->closedProfileBits = R64(Find(fields, 3)->value.data);
    result->integrity =
        static_cast<CaptureIntegrity>(Find(fields, 2)->value.data[0]);
    const std::vector<Bytes> empty;
    result->coverage = StreamResult(empty);
    result->unavailable = StreamResult(empty);
    result->diagnostics = StreamResult(empty);
    if (input.records.incremental)
      gIncrementalAccumulatorFinalizations.fetch_add(1, std::memory_order_relaxed);
    return Error::None;
  }
  try {
    AuthorityMap authority;
    authority.reserve(parsedRecords.size() / 4);
    std::uint64_t definitionOrdinal = 0, binaryOrdinal = 0;
    CaptureIntegrity integrity = CaptureIntegrity::Complete;
    std::uint64_t profiles = 0;
    StreamingHash recordHash;
    if (!recordHash.Initialize())
      return Error::BadScalar;
    std::uint64_t hashedRecords = 0;
    QueryPerformanceCounter(&stageStarted);
    for (const ParsedRecordWalkerRecord &record : parsedRecords) {
      if (!recordHash.Add(record.bytes))
        return Error::BadScalar;
      ++hashedRecords;
      const RecordKind kind = static_cast<RecordKind>(R16(record.bytes.data));
      ParsedFields decodedFields{}, decodedCommon{};
      ParsedFieldSpan fields{}, common{};
      if (gCanonicalDescriptorReuseEnabled) {
        fields = DescriptorSpan(descriptorArena, record.outer);
        common = DescriptorSpan(descriptorArena, record.common);
        ++gDescriptorRecordReuses;
      } else {
        error = ParseFields({record.bytes.data + 24, record.bytes.size - 24},
                            R16(record.bytes.data + 6), &decodedFields);
        if (error != Error::None) return error;
        fields = {decodedFields.fields.data(), decodedFields.count};
        if (kind == RecordKind::Node) {
          error = ParseNested(Find(fields, 1)->value, &decodedCommon);
          if (error != Error::None) return error;
          common = {decodedCommon.fields.data(), decodedCommon.count};
        }
      }
      if (kind == RecordKind::Manifest) {
        const ParsedField *const versionField = Find(fields, 1);
        if (versionField == nullptr ||
            versionField->scalar != ScalarTag::Struct ||
            versionField->value.size != kGraphVersionBytesV1) {
          return Error::BadScalar;
        }
        result->patchDescriptors.push_back({
            record.offset + static_cast<std::uint64_t>(
                versionField->value.data - record.bytes.data),
            1, ScalarTag::Struct, versionField->value.size, true, {}});
        integrity =
            static_cast<CaptureIntegrity>(Find(fields, 2)->value.data[0]);
        profiles = R64(Find(fields, 3)->value.data);
        std::copy_n(Find(fields, 5)->value.data,
                    result->viewIndexDigest.bytes.size(),
                    result->viewIndexDigest.bytes.begin());
      } else if (kind == RecordKind::Tombstone) {
        result->requiresActiveLineage = true;
      } else if (kind == RecordKind::Remap) {
        const ParsedField* const dispositionField = Find(fields, 3);
        const ParsedField* const reasonField = Find(fields, 4);
        if (dispositionField == nullptr || dispositionField->value.size != 1 ||
            reasonField == nullptr || reasonField->value.size != 2)
          return Error::BadScalar;
        const auto disposition = static_cast<RemapDisposition>(
            dispositionField->value.data[0]);
        const auto reason = static_cast<RemapReason>(
            R16(reasonField->value.data));
        result->requiresActiveLineage =
            result->requiresActiveLineage ||
            disposition == RemapDisposition::Retained ||
            disposition == RemapDisposition::Tombstoned ||
            disposition == RemapDisposition::Ambiguous ||
            (disposition == RemapDisposition::New &&
             (reason == RemapReason::Clone ||
              reason == RemapReason::TableCellSplit));
      } else if (kind == RecordKind::Node) {
        const UuidKey id = ReadUuid(Find(common, 1)->value);
        if (IsZero(id) || authority.find(id) != authority.end())
          return Error::BadRecordOrder;
        const NodeKind nodeKind =
            static_cast<NodeKind>(R16(Find(common, 2)->value.data));
        const ParsedField *const fingerprintField = Find(common, 8);
        if (fingerprintField == nullptr ||
            fingerprintField->scalar != ScalarTag::SHA256 ||
            fingerprintField->value.size != 32) {
          return Error::BadScalar;
        }
        CanonicalPatchDescriptor descriptor{};
        descriptor.valueOffset = record.offset + static_cast<std::uint64_t>(
            fingerprintField->value.data - record.bytes.data);
        descriptor.fieldTag = 8;
        descriptor.scalar = ScalarTag::SHA256;
        descriptor.valueBytes = fingerprintField->value.size;
        std::copy(id.begin(), id.end(), descriptor.nodeId.bytes.begin());
        result->patchDescriptors.push_back(std::move(descriptor));
        const std::uint64_t sibling = R64(Find(common, 4)->value.data);
        CanonicalPath path;
        if (nodeKind != NodeKind::Document) {
          const auto parent = authority.find(ReadUuid(Find(common, 3)->value));
          if (parent == authority.end())
            return Error::BadRecordOrder;
          path = parent->second.path;
        }
        path.push_back({nodeKind, sibling});
        std::uint64_t ordinal = 0;
        if (nodeKind == NodeKind::Definition)
          ordinal = definitionOrdinal++;
        else if (nodeKind == NodeKind::BinaryData)
          ordinal = binaryOrdinal++;
        authority.emplace(id, Authority{nodeKind, std::move(path), ordinal});
      }
    }
    QueryPerformanceCounter(&stageFinished);
    gAuthorityPassQpc += static_cast<std::uint64_t>(
        stageFinished.QuadPart - stageStarted.QuadPart);
    result->recordStream.itemCount = hashedRecords;
    result->recordStream.byteLength = parsedRecords.empty()
        ? 0 : parsedRecords.back().next;
    if (hashedRecords != recordCount ||
        !recordHash.Finish(&result->recordStream.digest))
      return Error::RecordIdMismatch;

    std::vector<WorkFrame> stack;
    std::unordered_map<UuidKey, std::vector<UuidKey>, UuidKeyHash>
        semanticReferences;
    semanticReferences.reserve(authority.size());
    std::vector<CaptureObservationInput> layoutValues, captureValues,
        unavailable;
    std::vector<CoverageInput> coverage;
    std::vector<DiagnosticInput> diagnostics;
    std::vector<LayoutFactInput> layoutFacts;
    bool semanticComplete = true;
    bool layoutComplete = true;
    Sha256 documentFingerprint{};
    QueryPerformanceCounter(&stageStarted);
    for (const ParsedRecordWalkerRecord &record : parsedRecords) {
      const RecordKind kind = static_cast<RecordKind>(R16(record.bytes.data));
      ParsedFields decodedFields{}, decodedCommon{}, decodedPayload{};
      ParsedFieldSpan fields{}, common{}, payload{};
      if (gCanonicalDescriptorReuseEnabled) {
        fields = DescriptorSpan(descriptorArena, record.outer);
        common = DescriptorSpan(descriptorArena, record.common);
        payload = DescriptorSpan(descriptorArena, record.payload);
        ++gDescriptorRecordReuses;
      } else {
        error = ParseFields({record.bytes.data + 24, record.bytes.size - 24},
                            R16(record.bytes.data + 6), &decodedFields);
        if (error != Error::None) return error;
        fields = {decodedFields.fields.data(), decodedFields.count};
        if (kind == RecordKind::Node) {
          error = ParseNested(Find(fields, 1)->value, &decodedCommon);
          if (error != Error::None) return error;
          error = ParseNested(Find(fields, 2)->value, &decodedPayload);
          if (error != Error::None) return error;
          common = {decodedCommon.fields.data(), decodedCommon.count};
          payload = {decodedPayload.fields.data(), decodedPayload.count};
        }
      }
      if (kind == RecordKind::Node) {
        const UuidKey id = ReadUuid(Find(common, 1)->value);
        const Authority &nodeAuthority = authority.find(id)->second;
        while (!stack.empty() &&
               stack.back().path.size() >= nodeAuthority.path.size()) {
          error = FinishFrame(
              &stack,
              &documentFingerprint,
              profiles,
              &semanticComplete,
              &layoutComplete,
              &result->nodeFingerprints);
          if (error != Error::None)
            return error;
        }
        if (nodeAuthority.kind != NodeKind::Document &&
            (stack.empty() ||
             stack.back().id != ReadUuid(Find(common, 3)->value)))
          return Error::BadRecordOrder;
        WorkFrame frame{};
        frame.id = id;
        frame.path = nodeAuthority.path;
        frame.kind = nodeAuthority.kind;
        frame.sibling = frame.path.back().siblingOrdinal;
        if (frame.kind == NodeKind::BinaryData &&
            !frame.binaryContent.Initialize())
          return Error::BadScalar;
        for (FieldTag commonBag : {FieldTag{10}, FieldTag{11}})
          if (const ParsedField *array = Find(common, commonBag))
            AddPropertyBag(&frame, commonBag, *array);
        for (std::uint16_t index = 0; index < payload.count; ++index) {
          const ParsedField &field = payload.fields[index];
          const NodeFieldRule *rule = PayloadRule(frame.kind, field.tag);
          if (rule == nullptr)
            return Error::BadNestedStruct;
          if (frame.kind == NodeKind::Definition && field.tag == 100)
            frame.definitionKind =
                static_cast<DefinitionKind>(R16(field.value.data));
          if (frame.kind == NodeKind::Definition && field.tag == 103) {
            std::copy_n(field.value.data,
                        frame.declaredDefinitionAggregate.bytes.size(),
                        frame.declaredDefinitionAggregate.bytes.begin());
            frame.declaredDefinitionAggregatePresent = true;
          }
          if (frame.kind == NodeKind::BinaryData && field.tag == 101)
            frame.binaryLength = R64(field.value.data);
          if (frame.kind == NodeKind::BinaryData && field.tag == 102)
            std::copy_n(field.value.data, frame.binaryDigest.bytes.size(),
                        frame.binaryDigest.bytes.begin());
          if (rule->array) {
            if ((field.flags & kFieldFlagArray) != 0) {
              AddPropertyBag(&frame, field.tag, field);
            } else if (rule->observation) {
              ObservationState arrayState{};
              const ByteView arrayValue = ObservationValue(field, &arrayState);
              if (arrayState == ObservationState::Value &&
                  arrayValue.size >= 16) {
                ParsedField nestedArray{field.tag, kFieldFlagArray,
                                        ScalarTag::Uint64,
                                        R64(arrayValue.data + 8), arrayValue};
                AddPropertyBag(&frame, field.tag, nestedArray);
              }
            }
            continue;
          }
          if (rule->root == RootClass::None)
            continue;
          ObservationState state = ObservationState::Value;
          ByteView value = field.value;
          if (rule->observation)
            value = ObservationValue(field, &state);
          CaptureObservationInput captureValue{
              frame.path, RecordKind::Node,       field.tag, false, 0,
              state,      PropertyOrigin::Unknown};
          if (rule->observation) {
            captureValue.hresult =
                static_cast<std::int32_t>(R32(field.value.data + 4));
            const std::uint64_t detailUnits = R64(field.value.data + 8);
            captureValue.detailUtf16.assign(
                field.value.data + 24,
                field.value.data + 24 +
                    static_cast<std::size_t>(detailUnits * 2));
          }
          if (state != ObservationState::Value) {
            unavailable.push_back(std::move(captureValue));
            const ProfileId owningProfile =
                ResolveNodeFieldProfile(*rule, frame.definitionKind);
            if (state != ObservationState::NotApplicable) {
              if (owningProfile == ProfileId::Layout &&
                  (profiles & ProfileBit(ProfileId::Layout)) != 0)
                layoutComplete = false;
              else if (rule->root == RootClass::Semantic &&
                       (profiles & ProfileBit(owningProfile)) != 0)
                semanticComplete = false;
            }
            continue;
          }
          Bytes transformed;
          error =
              TransformPayloadValue(input, frame.kind, field.tag, field.scalar,
                                    value, authority, &transformed);
          if (error != Error::None)
            return error;
          if (rule->root == RootClass::Capture) {
            captureValue.canonicalValue = std::move(transformed);
            captureValues.push_back(std::move(captureValue));
            continue;
          }
          if (rule->root == RootClass::Layout) {
            captureValue.canonicalValue = transformed;
            layoutValues.push_back(captureValue);
            continue;
          }
          SemanticPayloadInput semantic{field.tag, field.scalar,
                                        std::move(transformed)};
          if (frame.kind == NodeKind::Definition && field.tag == 101) {
            frame.nativeDefinition = semantic;
            frame.nativeDefinitionValue = true;
          } else if (frame.kind != NodeKind::BinaryData) {
            frame.payloads.push_back(std::move(semantic));
          }
        }
        stack.push_back(std::move(frame));
      } else if (kind == RecordKind::Property) {
        if (stack.empty())
          return Error::BadRecordOrder;
        const UuidKey owner = ReadUuid(Find(fields, 1)->value);
        if (owner != stack.back().id)
          return Error::BadRecordOrder;
        const FieldTag ownerField = R16(Find(fields, 2)->value.data);
        const PropertyKeyId key = R32(Find(fields, 3)->value.data);
        const RecordId propertyRecordId = R64(record.bytes.data + 8);
        PropertyBag *authorityBag = nullptr;
        for (PropertyBag &bag : stack.back().propertyBags)
          if (bag.field == ownerField) {
            authorityBag = &bag;
            break;
          }
        if (authorityBag == nullptr ||
            authorityBag->consumed >= authorityBag->ids.size() ||
            authorityBag->ids[authorityBag->consumed] != propertyRecordId)
          return Error::BadRecordOrder;
        ++authorityBag->consumed;
        if (stack.back().hasProperty &&
            (ownerField < stack.back().priorPropertyField ||
             (ownerField == stack.back().priorPropertyField &&
              key <= stack.back().priorPropertyKey)))
          return key == stack.back().priorPropertyKey ? Error::DuplicateField
                                                      : Error::BadRecordOrder;
        stack.back().hasProperty = true;
        stack.back().priorPropertyField = ownerField;
        stack.back().priorPropertyKey = key;
        const PropertyRule *rule = FindPropertyRule(key);
        if (rule == nullptr ||
            !IsPropertyApplicable(*rule, stack.back().kind, ownerField,
                                  stack.back().kind == NodeKind::Definition,
                                  stack.back().definitionKind))
          return Error::BadScalar;
        const ParsedField &observation = *Find(fields, 4);
        ObservationState state{};
        const ByteView value = ObservationValue(observation, &state);
        const auto origin =
            static_cast<PropertyOrigin>(Find(fields, 5)->value.data[0]);
        if (!PropertyOriginAllowedForObservation(
                rule->origin, state, origin))
          return Error::BadScalar;
        CaptureObservationInput captureValue{
            stack.back().path,
            RecordKind::Property,
            ownerField,
            true,
            key,
            state,
            origin,
            static_cast<std::int32_t>(R32(observation.value.data + 4))};
        const std::uint64_t detailUnits = R64(observation.value.data + 8);
        captureValue.detailUtf16.assign(
            observation.value.data + 24,
            observation.value.data + 24 +
                static_cast<std::size_t>(detailUnits * 2));
        if (state == ObservationState::Value) {
          stack.back().propertyValueKeys.push_back(key);
          error = TransformSemanticValue(input, rule->scalar, value, authority,
                                         &captureValue.canonicalValue);
          if (error != Error::None)
            return error;
          if (rule->root == RootClass::Semantic)
            stack.back().properties.push_back(
                {key, origin, captureValue.canonicalValue});
          else if (rule->root == RootClass::Layout) {
            layoutFacts.push_back(
                {stack.back().path, key, captureValue.canonicalValue});
            layoutValues.push_back(captureValue);
          } else if (rule->root == RootClass::Capture) {
            captureValues.push_back(captureValue);
          }
        } else {
          unavailable.push_back(captureValue);
          if (!ObservationSatisfiesCompleteness(*rule, state, true)) {
            ProfileId owningProfile{};
            if (!ResolvePropertyOwningProfile(
                    stack.back().kind, ownerField,
                    stack.back().definitionKind, &owningProfile))
              return Error::BadScalar;
            if (owningProfile == ProfileId::Layout) {
              if ((profiles & ProfileBit(ProfileId::Layout)) != 0)
                layoutComplete = false;
            } else if (rule->root == RootClass::Semantic &&
                       (rule->profileBits & ProfileBit(owningProfile)) != 0 &&
                       (profiles & ProfileBit(owningProfile)) != 0) {
              semanticComplete = false;
            }
          }
        }
      } else if (kind == RecordKind::Edge) {
        if (stack.empty())
          return Error::BadRecordOrder;
        const EdgeKind edge =
            static_cast<EdgeKind>(R16(Find(fields, 1)->value.data));
        const UuidKey source = ReadUuid(Find(fields, 2)->value);
        const UuidKey targetId = ReadUuid(Find(fields, 3)->value);
        const auto target = authority.find(targetId);
        const auto sourceAuthority = authority.find(source);
        if (target == authority.end() || sourceAuthority == authority.end())
          return Error::BadRecordOrder;
        if (edge == EdgeKind::Contains) {
          if (source != stack.back().id || target->second.path.size() <= 1 ||
              sourceAuthority->second.path.size() + 1 !=
                  target->second.path.size() ||
              !std::equal(
                  sourceAuthority->second.path.begin(),
                  sourceAuthority->second.path.end(),
                  target->second.path.begin(),
                  [](const PathSegment &left, const PathSegment &right) {
                    return left.kind == right.kind &&
                           left.siblingOrdinal == right.siblingOrdinal;
                  }))
            return Error::BadRecordOrder;
        } else {
          if (source != stack.back().id)
            return Error::BadRecordOrder;
          semanticReferences[source].push_back(targetId);
          ReferenceInput reference{};
          reference.edge = edge;
          reference.targetKind = target->second.kind;
          reference.ordinalTarget =
              target->second.kind == NodeKind::Definition ||
              target->second.kind == NodeKind::BinaryData;
          reference.targetOrdinal = target->second.ordinal;
          reference.targetPath = target->second.path;
          if (!stack.back().references.empty() &&
              !ReferenceLess(stack.back().references.back(), reference))
            return Error::BadRecordOrder;
          stack.back().references.push_back(std::move(reference));
        }
      } else if (kind == RecordKind::AssetChunk) {
        if (stack.empty() || stack.back().kind != NodeKind::BinaryData)
          return Error::BadRecordOrder;
        WorkFrame &frame = stack.back();
        const std::uint64_t total = R64(Find(fields, 2)->value.data);
        const std::uint64_t chunkOffset = R64(Find(fields, 3)->value.data);
        const ByteView encodedContent = Find(fields, 4)->value;
        const ByteView content{encodedContent.data + 8,
                               R64(encodedContent.data)};
        Sha256 fullDigest{};
        std::copy_n(Find(fields, 5)->value.data, fullDigest.bytes.size(),
                    fullDigest.bytes.begin());
        if ((frame.sawAssetChunk &&
             frame.binaryContentLength == frame.binaryLength) ||
            total != frame.binaryLength ||
            chunkOffset != frame.binaryContentLength ||
            !Equal(fullDigest, frame.binaryDigest) || content.size > 1048576 ||
            content.size > total ||
            (content.size != 1048576 && chunkOffset != total - content.size) ||
            !frame.binaryContent.Add(content))
          return Error::BadScalar;
        frame.binaryContentLength += content.size;
        frame.sawAssetChunk = true;
      } else if (kind == RecordKind::Coverage) {
        CoverageInput item{};
        UuidKey coverageOwner{};
        bool coverageOwnerPresent=false;
        if (const ParsedField *owner = Find(fields, 1)) {
          coverageOwner=ReadUuid(owner->value);
          const auto found = authority.find(coverageOwner);
          if (found == authority.end())
            return Error::BadRecordOrder;
          coverageOwnerPresent=true;
          item.ownerPresent = true;
          item.ownerPath = found->second.path;
        }
        item.profile = Find(fields, 2)->value.data[0];
        item.state = static_cast<CoverageState>(Find(fields, 3)->value.data[0]);
        if (const ParsedField *key = Find(fields, 4)) {
          item.propertyKeyPresent = true;
          item.propertyKey = R32(key->value.data);
        }
        error = DereferenceBlob(input, Find(fields, 5)->value, true,
                                &item.detailUtf16);
        if (error != Error::None || (item.detailUtf16.size() & 1U) != 0)
          return Error::BadScalar;
        item.ownerRecordKind =
            static_cast<RecordKind>(R16(Find(fields, 6)->value.data));
        item.ownerFieldTag = R16(Find(fields, 7)->value.data);
        const PropertyRule* const conditionalRule=
            item.propertyKeyPresent?FindPropertyRule(item.propertyKey):nullptr;
        if (!stack.empty() && coverageOwnerPresent &&
            coverageOwner==stack.back().id && conditionalRule!=nullptr &&
            HasQualifier(conditionalRule->qualifiers,
                         QualifierFlags::WindowsBrushFillCondition) &&
            item.profile==static_cast<std::uint8_t>(ProfileId::EditableObjects) &&
            item.state==CoverageState::NotApplicable &&
            item.ownerRecordKind==RecordKind::Property &&
            item.ownerFieldTag==102 && stack.back().kind==NodeKind::Definition &&
            stack.back().definitionKind==DefinitionKind::BorderFill) {
          stack.back().conditionalNotApplicableKeys.push_back(item.propertyKey);
        }
        if (item.state == CoverageState::NotExposed ||
            item.state == CoverageState::ReadFailed) {
          const PropertyRule *rule = item.propertyKeyPresent
                                         ? FindPropertyRule(item.propertyKey)
                                         : nullptr;
          if (rule == nullptr ||
              rule->requirement == RequirementClass::RequiredWhenApplicable) {
            if (item.profile ==
                static_cast<std::uint8_t>(ProfileId::Layout)) {
              if ((profiles & ProfileBit(ProfileId::Layout)) != 0)
                layoutComplete = false;
            } else if (item.profile == kNoProfile) {
              if (rule != nullptr && rule->root == RootClass::Layout)
                layoutComplete = false;
              else
                semanticComplete = false;
            } else {
              const ProfileId coverageProfile =
                  static_cast<ProfileId>(item.profile);
              if ((profiles & ProfileBit(coverageProfile)) != 0 &&
                  (rule == nullptr || rule->root == RootClass::Semantic))
                semanticComplete = false;
            }
          }
        }
        coverage.push_back(std::move(item));
      } else if (kind == RecordKind::Diagnostic) {
        DiagnosticInput item{};
        item.code =
            static_cast<DiagnosticCode>(R32(Find(fields, 1)->value.data));
        item.severity = static_cast<Severity>(Find(fields, 2)->value.data[0]);
        if (const ParsedField *owner = Find(fields, 3)) {
          const auto found = authority.find(ReadUuid(owner->value));
          if (found == authority.end())
            return Error::BadRecordOrder;
          item.ownerPresent = true;
          item.ownerPath = found->second.path;
        }
        if (const ParsedField *key = Find(fields, 4)) {
          item.propertyKeyPresent = true;
          item.propertyKey = R32(key->value.data);
        }
        item.hresult =
            static_cast<std::int32_t>(R32(Find(fields, 5)->value.data));
        error = DereferenceBlob(input, Find(fields, 6)->value, true,
                                &item.detailUtf16);
        if (error != Error::None || (item.detailUtf16.size() & 1U) != 0)
          return Error::BadScalar;
        diagnostics.push_back(std::move(item));
      }
    }
    std::unordered_map<UuidKey, std::uint8_t, UuidKeyHash> colors;
    colors.reserve(semanticReferences.size());
    std::function<bool(const UuidKey &)> cycle = [&](const UuidKey &node) {
      std::uint8_t &color = colors[node];
      if (color == 1)
        return true;
      if (color == 2)
        return false;
      color = 1;
      const auto edges = semanticReferences.find(node);
      if (edges != semanticReferences.end())
        for (const UuidKey &target : edges->second)
          if (semanticReferences.find(target) != semanticReferences.end() &&
              cycle(target))
            return true;
      color = 2;
      return false;
    };
    for (const auto &entry : semanticReferences)
      if (cycle(entry.first))
        return Error::BadRecordOrder;
    QueryPerformanceCounter(&stageFinished);
    gSemanticPassQpc += static_cast<std::uint64_t>(
        stageFinished.QuadPart - stageStarted.QuadPart);
    while (!stack.empty()) {
      error = FinishFrame(
          &stack,
          &documentFingerprint,
          profiles,
          &semanticComplete,
          &layoutComplete,
          &result->nodeFingerprints);
      if (error != Error::None)
        return error;
    }
    result->rootsPresent = true;
    result->closedProfileBits = profiles;
    result->integrity = integrity;
    result->observedSemanticRoot = ObservedSemanticRoot(documentFingerprint);
    result->semanticCertified =
        integrity == CaptureIntegrity::Complete && semanticComplete;
    Bytes layoutEnvironment;
    if (input.layoutEnvironment.size != 0)
      layoutEnvironment.assign(
          input.layoutEnvironment.data,
          input.layoutEnvironment.data +
              static_cast<std::size_t>(input.layoutEnvironment.size));
    const bool layoutRequested =
        (profiles & ProfileBit(ProfileId::Layout)) != 0;
    if (layoutRequested &&
        !layout::ValidateLayoutEnvironmentV1(layoutEnvironment))
      layoutComplete = false;
    result->layoutPresent =
        result->semanticCertified && layoutRequested && layoutComplete;
    if (result->layoutPresent)
      result->layoutRoot = LayoutRoot(
          {result->observedSemanticRoot, layoutFacts, layoutEnvironment});
    CaptureRootInput capture{};
    capture.closedProfileBits = profiles;
    capture.integrity = integrity;
    capture.semanticCertified = result->semanticCertified;
    capture.observedSemanticRoot = result->observedSemanticRoot;
    capture.layoutPresent = result->layoutPresent;
    capture.layoutRoot = result->layoutRoot;
    capture.layoutValues = layoutValues;
    capture.captureOnlyValues = captureValues;
    capture.coverage = coverage;
    capture.unavailable = unavailable;
    capture.diagnostics = diagnostics;
    error = CaptureRoot(std::move(capture), &result->captureRoot);
    if (error != Error::None)
      return error;
    std::vector<Bytes> encodedCoverage, encodedUnavailable, encodedDiagnostics;
    for (const auto &item : coverage)
      encodedCoverage.push_back(CoverageBytes(item));
    for (const auto &item : unavailable)
      encodedUnavailable.push_back(UnavailableBytes(item));
    for (const auto &item : diagnostics)
      encodedDiagnostics.push_back(DiagnosticBytes(item));
    result->coverage = StreamResult(encodedCoverage);
    result->unavailable = StreamResult(encodedUnavailable);
    result->diagnostics = StreamResult(encodedDiagnostics);
    if (input.records.incremental)
      gIncrementalAccumulatorFinalizations.fetch_add(1, std::memory_order_relaxed);
    return Error::None;
  } catch (...) {
    return Error::LengthOverflow;
  }
}

Error CreateCanonicalArtifactFromFacts(
    const RecordStream &source,
    CanonicalizationResult facts,
    CanonicalArtifactPin *artifact) noexcept {
  if (artifact == nullptr || !facts.rootsPresent ||
      facts.streamKind != source.kind ||
      facts.recordStream.byteLength != source.length)
    return Error::BadFlags;
  *artifact = {};
  try {
    *artifact = std::shared_ptr<const CanonicalArtifact>(
        new CanonicalArtifact(source, std::move(facts)));
    return Error::None;
  } catch (...) {
    return Error::LengthOverflow;
  }
}
Error CreateCanonicalArtifact(const CanonicalizationInput &input,
                              CanonicalArtifactPin *artifact) noexcept {
  if (artifact == nullptr) return Error::BadFlags;
  CanonicalizationResult facts{};
  const Error error = CanonicalizeRecordStream(input, &facts);
  return error == Error::None
      ? CreateCanonicalArtifactFromFacts(
            input.records, std::move(facts), artifact)
      : error;
}
const CanonicalizationResult *CanonicalArtifactFacts(
    const CanonicalArtifactPin &artifact) noexcept {
  return artifact == nullptr ? nullptr : &artifact->facts_;
}
bool CanonicalArtifactSourceMatches(
    const CanonicalArtifactPin &artifact,
    const RecordStream &records) noexcept {
  return artifact != nullptr && artifact->source_.context == records.context &&
         artifact->source_.read == records.read &&
         artifact->source_.length == records.length &&
         artifact->source_.kind == records.kind;
}
void *CanonicalArtifactSourceContext(
    const CanonicalArtifactPin &artifact) noexcept {
  return artifact == nullptr ? nullptr : artifact->source_.context;
}
} // namespace hancom::graph::codec
