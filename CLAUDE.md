# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 저장소 구조

두 계층으로 나뉜다.

- **저장소 루트** — 배포·설치 계층. `install.ps1` / `uninstall.ps1` / `restore-update.ps1`, `CHANGELOG.md`, `docs/release-integrity.md`, `.github/workflows/publish-release.yml`(GitHub Release 게시 CI)이 여기 있다. Codex/Claude Code 플러그인 배포판을 패키징하고 원상복구하는 역할만 한다.
- **`plugins/gsg-hwp/`** — 실제 플러그인 소스. 여기서 코드를 읽고 고친다.
  - `addon/` — 한/글 프로세스 내부에서 동작하는 Win32 C++ 컴포넌트 4종(`HancomLiveBridgeNative`, `HancomEventBridge`, `HancomMcpLauncher`, `HancomApiHarness`). Visual Studio `.vcxproj`로 빌드.
  - `skills/automate-hancom-documents/` — Python MCP 서버 전체.
    - `SKILL.md` — 에이전트가 이 플러그인의 도구를 어떻게 호출해야 하는지 규정하는 행동 계약서(영문). 공개 도구의 동작을 바꾸면 이 문서도 함께 갱신해야 한다.
    - `scripts/` — MCP 서버 구현(291개 모듈). `hwp_mcp.py`가 `FastMCP` 서버를 조립하고, `hwp_mcp_hot_reload.py`가 실제 stdio 진입점(핫리로드 슈퍼바이저)이다. `hwp_live_*`는 한/글 세션·브리지·검사, `hwp_public_*`는 에이전트에 노출되는 공개 도구 구현, `hwp_operation_*`는 작업 저널·멱등성, `hwp_reference_*`는 참조 이미지→네이티브 레이아웃 재구성, `hwp_official_api_*`는 공식 API 카탈로그 검색/커버리지를 담당한다.
    - `references/*.md` — 작업 유형별로 에이전트가 읽는 라우팅 문서(라이브 검사, 표/이미지, 레이아웃 재구성, 공식 API, 검증/복구, 배치 워크플로 등).
    - `resources/` — 패키징된 한컴 공식 API 카탈로그(JSON/gzip).
  - `tests/` — `skills/.../scripts`의 각 모듈에 대응하는 pytest 스위트(149개 파일). 실제 한/글 프로세스 없이 `unittest.mock`으로 브리지·COM 경계를 모킹해서 돌린다.
  - `compatibility-manifest.json` — 배포 버전, 네이티브 DLL/런처 SHA-256, 공개 도구 수, 프로토콜 버전을 고정한다. CI(`publish-release.yml`)는 이 파일의 `distribution`이 `.codex-plugin/plugin.json`의 버전과 일치하는지 검사한다.
- 저장소 루트 `tests/*.ps1` — Windows에 실제 설치·업데이트·제거를 수행해 검증하는 릴리스 안전 QA 스크립트(`auto_update_qa.ps1`, `security_module_installation_qa.ps1`, `release_installation_qa.ps1`). `plugins/gsg-hwp/tests/`의 pytest 스위트와는 별개이며, 릴리스 게시 CI에서만 실행된다.

## 개발 명령

Python 작업은 모두 `plugins/gsg-hwp/`에서 `uv`로 실행한다(시스템 Python이 아니라 저장소 `pyproject.toml`이 고정한 Python 3.12.10).

```powershell
cd plugins/gsg-hwp
uv sync --extra test          # pytest, ruff, basedpyright 포함 설치
uv run pytest                 # 전체 테스트 (testpaths=tests)
uv run pytest tests/test_hwp_mcp_startup.py           # 파일 단위
uv run pytest tests/test_hwp_mcp_startup.py -k <name> # 이름/패턴 단위
uv run ruff check .
uv run basedpyright
```

`tmp/`, `.venv/`, `legacy/`, `addon/`는 pytest 수집과 basedpyright 검사 모두에서 제외된다(작업 산출물·저장소 복제본이 쌓여 모듈명 충돌 및 대량 오탐을 일으켰던 실측 이력 때문). 새 테스트나 스크립트를 이 경로들 밖에 두어야 한다.

네이티브 컴포넌트 빌드(Visual Studio C++, `Release|Win32`):

```powershell
msbuild addon\HancomLiveBridgeNative\HancomLiveBridgeNative.vcxproj /p:Configuration=Release /p:Platform=Win32
```

`addon/HancomLiveBridgeNative/smoke`와 `topology_smoke`에는 실행 중인 한/글 프로세스에 붙어 브리지를 검증하는 Win32 스모크 테스트가 있다(`BridgeSmoke.exe --probe <PID>` 등). `addon/HancomLiveBridgeNative/README.md`에 정확한 사용법이 있다.

## 아키텍처

### 요청 경로

```
에이전트 앱(Codex/Claude Code) → PowerShell 시작 스크립트(start-mcp.ps1)
  → HancomMcpLauncher.exe (데스크톱 사용자 토큰으로 기동, 한/글과 같은 ROT를 봄)
  → Python MCP 핫리로드 프록시(hwp_mcp_hot_reload.py, HwpWorkerSupervisor)
  → Python 작업 워커(hwp_mcp.py의 FastMCP 서버, HancomBridge)
  → Windows ROT + Event Bridge → 실행 중인 한/글 프로세스
  → Win32 C++ UserAction DLL(HancomLiveBridgeNative) → ATL IDispatch 배치 객체
  → 현재 열려 있는 HWP 문서
```

핵심 설계는 Python이 문서를 직접 COM 호출하는 것이 아니라, 한/글 프로세스 안에 로드된 네이티브 DLL이 `IHwpObject`와 ATL 기반 `IDispatch` 배치 객체를 ROT에 공개하고 Python은 그 배치 프로토콜(`ExecuteActions`/HCA1, `InspectPagesV3`/HPM1, `InspectStructure`/HDS1 등, 현재 프로토콜 버전 12)을 통해서만 상호작용한다는 점이다.

### MCP 서버: 프록시-워커 분리와 핫리로드

`hwp_mcp_hot_reload.py`(stdio 진입점)는 실제 도구 구현을 별도 워커 프로세스로 기동·감시한다. 워커 소스 해시가 바뀌면 자동으로 재기동하고 도구 목록 변경 알림을 보낸다. `hwp_runtime_info`/`hwp_reload`가 이 상태(런타임 워치 경로, `tool_schema_hash`)를 조회·강제 재기동한다.

### 두 개의 MCP 프로필

`HANCOM_MCP_PROFILE` 환경 변수(`hwp_mcp_registry.py`)로 `production`(기본값)과 `qa`를 나눈다. `production`은 업무 도구 44개 + `hwp_reload` = 45개만 노출한다(공개 도구 목록은 `compatibility-manifest.json`의 `exposed_tool_count`, `production_sessionless_reads`로 고정). `qa`에서만 일반화된 `hwp_operate` 및 진단용 도구가 열린다(`references/qa-operation-internals.md`). 실사용 도구를 늘릴 때는 범용 API를 그대로 노출하지 말고 작업 중심 공개 도구/recipe로 좁혀서 추가한다(`AGENTS.md`의 "기능 확장 요청" 절 참고).

### 작업 멱등성과 검증

모든 공개 쓰기 호출은 호출자가 지정한 `operation_id`를 요구하고 `OperationJournal`이 종결 결과를 재생 가능하게 보관한다(기본 30일, 또는 10,000건/64MiB 초과 시 오래된 것부터 축출). 도구는 디스패치 성공이 아니라 도구별 readback(변경된 범위·상태 토큰 재확인)까지 마쳐야 성공으로 취급한다 — 이 규약이 `SKILL.md`와 `references/verification-recovery.md`에 상세히 규정되어 있고 테스트(`test_hwp_operation_*`, `test_hwp_public_idempotency_retry.py` 등)가 이를 검증한다.

### 공식 API 카탈로그

1,452개 한컴 공식 API 케이스 중 1,448개가 네이티브 라우팅으로 노출되고, 나머지 4개는 검증 실패로 명시 제외된다(`compatibility-manifest.json`의 `official_api_disabled_case_ids`). 전체 API를 개별 MCP 도구로 노출하지 않는 것이 원칙이며, `hwp_search_tools`(production, `include_official_api=true`)와 `hwp_get_official_api_coverage`로만 조회한다.

### 라이선스 경계

이 저장소 코드는 MIT지만, 한컴 Automation 자체의 상업적 이용에는 한글과컴퓨터의 별도 승인·라이선스가 필요하다(`THIRD_PARTY_NOTICES.md`). 이 구분은 코드에서 자동으로 드러나지 않으므로 라이선스 관련 안내를 할 때 유지한다.

## 설치·업데이트·제거 에이전트 지침

이 저장소를 설치·업데이트·제거해 달라는 요청을 받으면 이 절과 `README.md`, `AGENTS.md`를 먼저 읽는다. 레지스트리를 직접 편집하거나 DLL을 임의 경로에 복사하지 않고 저장소의 스크립트만 사용한다.

- 실제 변경 전에 `install.ps1` 미리보기를 실행하고 DLL 2개·HKCU 레지스트리 3개 값·백업·자동 업데이트·복원 방법을 사용자에게 안내한다.
- 한/글이 실행 중일 때 DLL을 교체하지 않는다.
- 실제 설치는 `install.ps1 -AcceptChanges`만 사용한다.
- 설치와 업데이트는 시스템 Python이 아닌 버전별 uv 관리 `.venv`(`%LOCALAPPDATA%\GSG_HWP\runtime\<버전>\.venv`)를 사용한다.
- 레지스트리 직접 편집, `regsvr32`, 관리자 권한, 키보드·마우스 자동화를 사용하지 않는다.
- 설치 후 `plugins/gsg-hwp/scripts/start-mcp.ps1`의 절대 경로를 `claude mcp add --transport stdio --scope user`로 등록한다.
- 제거 요청 시 한/글을 닫고 `uninstall.ps1` 미리보기 후 `uninstall.ps1 -AcceptChanges`로 설치 전 네이티브/파일 경로 보안 DLL과 레지스트리 상태를 복원한다.
- 직전 자동 업데이트만 되돌리는 요청이면 `restore-update.ps1` 미리보기 후 `restore-update.ps1 -AcceptChanges`를 사용한다.
- 기능이 공개 도구에 없으면 1,448개 활성 공식 API 라우트를 조사해 필요한 기능만 전용 도구/recipe로 연결한다. 전체 API를 한꺼번에 도구로 노출하지 않는다.

전체 절차(변경 전 사용자 안내 문구, 단계별 명령, Codex/Claude Code 등록 방법, 제거·복구 절차)는 `AGENTS.md`에 있다.
