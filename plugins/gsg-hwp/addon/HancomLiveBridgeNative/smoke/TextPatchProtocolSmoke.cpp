#include "../ActionProtocol.h"

#include <iostream>
#include <string>

namespace {

bool ParseOne(
    const std::wstring& record,
    hancom::actions::Command* const command) {
    hancom::actions::Request request;
    hancom::actions::Error error;
    const std::wstring payload =
        L"HCA1\nDOC\t17\tQzovZG9jdW1lbnRzL3NhbXBsZS5od3A=\n" +
        record + L"\nEND";
    if (!hancom::actions::ParseRequest(payload, &request, &error) ||
        request.commands.size() != 1 || command == nullptr) {
        return false;
    }
    *command = request.commands.front();
    return true;
}

bool ExactPreparedRecordMatrix(
    const std::wstring& prepare,
    const std::wstring& forward,
    const std::wstring& inverse,
    const std::wstring& target) {
    hancom::actions::Command prepared;
    hancom::actions::Command applied;
    hancom::actions::Command rolledBack;
    return ParseOne(prepare, &prepared) &&
        prepared.kind == hancom::actions::CommandKind::TextPatch &&
        prepared.name == target && prepared.preflightOnly &&
        !prepared.hasExpectedText && prepared.first.empty() &&
        prepared.second == L"new" &&
        ParseOne(forward, &applied) && !applied.preflightOnly &&
        applied.hasExpectedText && applied.first == L"old" &&
        applied.second == L"new" &&
        ParseOne(inverse, &rolledBack) && !rolledBack.preflightOnly &&
        rolledBack.hasExpectedText && rolledBack.first == L"new" &&
        rolledBack.second == L"old";
}

}  // namespace

bool TextPatchProtocolSmoke() {
    const bool range = ExactPreparedRecordMatrix(
        L"PREPARE_TEXT\tRANGE\t0\t1\t0\t0\t1\t3\tbmV3",
        L"PATCH_TEXT\tRANGE\t0\t1\t0\t0\t1\t3\tb2xk\tbmV3",
        L"PATCH_TEXT\tRANGE\t0\t1\t0\t0\t1\t3\tbmV3\tb2xk",
        L"RANGE");
    const bool cell = ExactPreparedRecordMatrix(
        L"PREPARE_TEXT\tCELL\tdGFibGUtMQ==\tA1\t0\t0\tbmV3",
        L"PATCH_TEXT\tCELL\tdGFibGUtMQ==\tA1\t0\t0\tb2xk\tbmV3",
        L"PATCH_TEXT\tCELL\tdGFibGUtMQ==\tA1\t0\t0\tbmV3\tb2xk",
        L"CELL");
    std::wcout << L"TEXT_PATCH_PREPARED_RANGE_PROTOCOL " << range << L'\n'
               << L"TEXT_PATCH_PREPARED_CELL_PROTOCOL " << cell << L'\n'
               << L"TEXT_PATCH_FORWARD_EXPECTED_B64 b2xk\n"
               << L"TEXT_PATCH_INVERSE_EXPECTED_B64 bmV3\n";
    hancom::actions::Command emptyCommand;
    const bool emptyCell = ParseOne(
        L"PATCH_TEXT\tCELL\tdGFibGUtMQ==\tA1\t0\t0\t\tbmV3", &emptyCommand) &&
        emptyCommand.hasExpectedText && emptyCommand.first.empty();
    const bool emptyFindRejected = !ParseOne(
        L"PATCH_TEXT\tFIND\tdGFibGUtMQ==\tA1\t0\t0\t\tbmV3", &emptyCommand);
    std::wcout << L"TEXT_PATCH_EMPTY_CELL_PROTOCOL " << emptyCell << L'\n'
               << L"TEXT_PATCH_EMPTY_FIND_REJECTED " << emptyFindRejected << L'\n';
    hancom::actions::Command literal;
    const bool literalRange = ParseOne(
        L"PREPARE_TEXT\tLITERAL_RANGE\t0\t1\t4\t7\tdGhlIG9sZA==\tb2xk\tbmV3", &literal) &&
        literal.literalRange && literal.preflightOnly && literal.hasExpectedText &&
        literal.paragraphText == L"the old" && literal.first == L"old";
    const bool badLiteral = !ParseOne(
        L"PREPARE_TEXT\tLITERAL_RANGE\t0\t1\t0\t3\tdGhlIG9sZA==\tb2xk\tbmV3", &literal);
    std::wcout << L"TEXT_PATCH_LITERAL_RANGE_PROTOCOL " << literalRange << L'\n'
               << L"TEXT_PATCH_LITERAL_CONTEXT_GUARD " << badLiteral << L'\n';
    return range && cell && emptyCell && emptyFindRejected && literalRange && badLiteral;
}
