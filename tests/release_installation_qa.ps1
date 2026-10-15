#Requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]
        $Expected,
        [Parameter(Mandatory = $true)]
        $Actual,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ($Expected -ne $Actual) {
        throw "$Message (expected=$Expected, actual=$Actual)"
    }
}

function Get-GsgHwpLineHash {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Text
    )

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text.Trim())
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (($sha256.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    finally {
        $sha256.Dispose()
    }
}

function Test-GsgHwpDocumentedClaim {
    param(
        [Parameter(Mandatory = $true)]
        [string]$File,
        [Parameter(Mandatory = $true)]
        [string]$Check,
        [Parameter(Mandatory = $true)]
        [string]$Token,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Line
    )

    $lineHash = Get-GsgHwpLineHash -Text $Line
    foreach ($entry in $script:documentationClaimAllowlist) {
        if ($entry.File -ceq $File -and $entry.Check -ceq $Check -and
            $entry.Token -ceq $Token -and $entry.LineSha256 -ceq $lineHash) {
            $entry.Consumed = $true
            return $true
        }
    }
    return $false
}

function Get-GsgHwpDocumentRows {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [switch]$StripFences
    )

    $documentName = Split-Path -Leaf $Path
    $isMarkdown = $documentName.EndsWith(".md", [StringComparison]::OrdinalIgnoreCase)
    if ([string]::IsNullOrWhiteSpace($script:documentCurrentVersion)) {
        throw ("Get-GsgHwpDocumentRows ran before `$script:documentCurrentVersion was read from " +
            "compatibility-manifest.json, so it cannot tell the current release section from history")
    }
    $lines = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) -split "`r?`n"
    $rows = @()
    $inFence = $false
    $inHistory = $false
    $releaseBlocks = 0
    $sawReleaseHeading = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        $line = $lines[$index]
        if ($isMarkdown -and $line -match '^\s*```') {
            $inFence = -not $inFence
            continue
        }
        if (-not $inFence) {
            if ($documentName -eq "README.md") {
                if ($line -match '^### v(?<version>\d+\.\d+\.\d+) ') {
                    # The section for the version being released describes what
                    # this build ships, so it is current state and stays in the
                    # scan. History starts at the next '### v...' heading.
                    $inHistory = ($sawReleaseHeading -or
                        $Matches['version'] -cne $script:documentCurrentVersion)
                    $sawReleaseHeading = $true
                }
                elseif ($inHistory -and $line -match '^## ') {
                    $inHistory = $false
                }
            }
            elseif ($documentName -eq "CHANGELOG.md" -and $line -match '^## \[') {
                $releaseBlocks++
                if ($releaseBlocks -ge 2) {
                    break
                }
            }
        }
        if ($inHistory) {
            continue
        }
        if ($StripFences -and $inFence) {
            continue
        }
        $rows += [pscustomobject][ordered]@{ Number = $index + 1; Text = $line }
    }
    return , $rows
}

# Read from compatibility-manifest.json below, before any document is scanned.
# Get-GsgHwpDocumentRows uses it to keep the release section for the version
# being shipped inside the current-state scan instead of treating it as history.
$documentCurrentVersion = ""

# Reviewed exceptions for the documentation scanners below. An exception applies
# only when the file, the check, the token and the SHA-256 of the trimmed line
# all agree, so a single edited character sends the exception back through
# review. Entries that stop matching fail the gate instead of rotting.
$documentationClaimAllowlist = @(
    [pscustomobject][ordered]@{
        File = "README.md"
        Check = "semver"
        Token = "1.0.1"
        LineSha256 = "1218a45e1fc397ce3a3b0976802da4c8724f654b83c4e45b142c8b491dd1dc73"
        Reason = "Names the release that introduced the security module registration, not a version this build ships"
        Consumed = $false
    },
    [pscustomobject][ordered]@{
        File = "CLAUDE.md"
        Check = "tool_name"
        Token = "hwp_running"
        LineSha256 = "ebc6b0098be321cdfdb2f62a1ffd4256a68a5d204383fe77ab39dfb5b24872d2"
        Reason = "A pending-native-install.json reason value, not an MCP tool name"
        Consumed = $false
    },
    [pscustomobject][ordered]@{
        File = "docs\release-integrity.md"
        Check = "semver"
        Token = "1.2.4"
        LineSha256 = "9d9ecb8e326f60dcdec6624914dbc8d1450a825ec6be278a43bd7f6d0311bba7"
        Reason = "Names the release after which the launcher and event bridge stopped changing, not a version this build ships"
        Consumed = $false
    },
    [pscustomobject][ordered]@{
        File = "docs\release-integrity.md"
        Check = "api_count"
        Token = "4,365,083"
        LineSha256 = "6e5bbcff4abb28f4dc1588daa4609b2c312b365b6c955a40c5f5135ca8fdd33c"
        Reason = "The release archive is built by CI, so neither its SHA-256 nor its size can be recomputed here. Pinning the whole row sends any edit to either value back through review"
        Consumed = $false
    }
)

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pluginRoot = Join-Path $repositoryRoot "plugins\gsg-hwp"
$modulePath = Join-Path $pluginRoot "scripts\GsgHwp.Installation.psm1"
$requiredFiles = @(
    ".agents\plugins\marketplace.json",
    ".github\workflows\publish-release.yml",
    ".mcp.json",
    "AGENTS.md",
    "CLAUDE.md",
    "CHANGELOG.md",
    "docs\release-integrity.md",
    "LICENSE",
    "QUICKSTART-KO.md",
    "README.md",
    "restore-update.ps1",
    "THIRD_PARTY_NOTICES.md",
    "install.ps1",
    "uninstall.ps1",
    "plugins\gsg-hwp\.codex-plugin\plugin.json",
    "plugins\gsg-hwp\.mcp.json",
    "plugins\gsg-hwp\compatibility-manifest.json",
    "plugins\gsg-hwp\LICENSE",
    "plugins\gsg-hwp\THIRD_PARTY_NOTICES.md",
    "plugins\gsg-hwp\scripts\start-mcp.ps1",
    "plugins\gsg-hwp\scripts\GsgHwp.Bootstrap.psm1",
    "plugins\gsg-hwp\scripts\GsgHwp.Update.psm1",
    "plugins\gsg-hwp\update-policy.json"
)

foreach ($relativePath in $requiredFiles) {
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $repositoryRoot $relativePath)) `
        -Message "Required release file is missing: $relativePath"
}

$powerShellFiles = Get-ChildItem -LiteralPath $repositoryRoot -Recurse -File |
    Where-Object { $_.Extension -in @(".ps1", ".psm1") }
foreach ($powerShellFile in $powerShellFiles) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $powerShellFile.FullName,
        [ref]$tokens,
        [ref]$parseErrors
    )
    Assert-Equal -Expected 0 -Actual @($parseErrors).Count `
        -Message "PowerShell parse error in $($powerShellFile.FullName)"
}

$plugin = Get-Content -LiteralPath (Join-Path $pluginRoot ".codex-plugin\plugin.json") -Raw -Encoding UTF8 |
    ConvertFrom-Json
$marketplace = Get-Content -LiteralPath (Join-Path $repositoryRoot ".agents\plugins\marketplace.json") -Raw -Encoding UTF8 |
    ConvertFrom-Json
$manifest = Get-Content -LiteralPath (Join-Path $pluginRoot "compatibility-manifest.json") -Raw -Encoding UTF8 |
    ConvertFrom-Json
$mcp = Get-Content -LiteralPath (Join-Path $pluginRoot ".mcp.json") -Raw -Encoding UTF8 |
    ConvertFrom-Json
$projectMetadata = Get-Content -LiteralPath (Join-Path $pluginRoot "pyproject.toml") -Raw -Encoding UTF8

# Every document scan below runs through Get-GsgHwpDocumentRows, which needs the
# version being released to know where the README release history begins.
$documentCurrentVersion = [string]$manifest.distribution
Assert-True -Condition ([regex]::IsMatch($documentCurrentVersion, '^\d+\.\d+\.\d+$')) `
    -Message "compatibility-manifest.json distribution is not a three-part version, so the README history boundary cannot be resolved"

Assert-Equal -Expected "gsg-hwp" -Actual $plugin.name -Message "Plugin name mismatch"
Assert-Equal -Expected $manifest.distribution -Actual $plugin.version `
    -Message "Plugin version mismatch"
Assert-Equal -Expected "inodesign" -Actual $plugin.author.name -Message "Plugin author mismatch"
Assert-Equal -Expected "inodesign" -Actual $plugin.interface.developerName `
    -Message "Plugin developer metadata mismatch"
Assert-True -Condition ($manifest.source_version -ne $manifest.distribution) `
    -Message "Source and public distribution versions must remain distinct"
Assert-Equal -Expected "inodesign" -Actual $manifest.developer -Message "Manifest developer mismatch"
Assert-Equal -Expected "MIT" -Actual $manifest.license -Message "Distribution license mismatch"
Assert-Equal -Expected "LICENSE" -Actual $manifest.license_file -Message "License file metadata mismatch"
Assert-Equal -Expected "THIRD_PARTY_NOTICES.md" -Actual $manifest.third_party_notices `
    -Message "Third-party notices metadata mismatch"
Assert-Equal -Expected "All-Rights-Reserved" -Actual $manifest.media_license `
    -Message "Demo media license metadata mismatch"
Assert-True -Condition $manifest.hancom_automation_commercial_approval_required `
    -Message "Hancom commercial approval metadata mismatch"
Assert-True -Condition ($projectMetadata -match '(?m)^license = "MIT"\s*$') `
    -Message "Python project license metadata mismatch"
$rootLicenseHash = (Get-FileHash -LiteralPath (Join-Path $repositoryRoot "LICENSE") `
    -Algorithm SHA256).Hash
$pluginLicenseHash = (Get-FileHash -LiteralPath (Join-Path $pluginRoot "LICENSE") `
    -Algorithm SHA256).Hash
Assert-Equal -Expected $rootLicenseHash -Actual $pluginLicenseHash `
    -Message "Root and plugin license copies differ"
$rootNoticesHash = (Get-FileHash -LiteralPath (Join-Path $repositoryRoot "THIRD_PARTY_NOTICES.md") `
    -Algorithm SHA256).Hash
$pluginNoticesHash = (Get-FileHash -LiteralPath (Join-Path $pluginRoot "THIRD_PARTY_NOTICES.md") `
    -Algorithm SHA256).Hash
Assert-Equal -Expected $rootNoticesHash -Actual $pluginNoticesHash `
    -Message "Root and plugin notice copies differ"
Assert-True -Condition ($projectMetadata -match '(?m)^version = "([^"]+)"\s*$') `
    -Message "Python project version metadata is missing"
Assert-Equal -Expected $Matches[1] -Actual $manifest.mcp -Message "MCP version mismatch"
Assert-True -Condition ([regex]::IsMatch(
    [string]$manifest.native_bridge,
    '^\d+\.\d+\.\d+$'
)) -Message "Native bridge version mismatch"
Assert-Equal -Expected $manifest.tool_catalogs.worker_tools.count `
    -Actual @($manifest.production_tools).Count `
    -Message "Production tool count mismatch"
Assert-True -Condition ($manifest.production_tools -contains "hwp_insert_layout") `
    -Message "Mid-document layout tool is missing"
Assert-True -Condition ($manifest.production_tools -contains "hwp_list_window_states") `
    -Message "Window-state tool is missing"
Assert-Equal -Expected $manifest.tool_catalogs.host_visible_tools.count `
    -Actual $manifest.exposed_tool_count -Message "Exposed tool count mismatch"
Assert-Equal -Expected $manifest.tool_catalogs.qa_tools.count `
    -Actual $manifest.qa_tool_count -Message "QA tool count mismatch"
Assert-Equal -Expected "hwp_reload" -Actual $manifest.runtime_tools[0] `
    -Message "Runtime reload tool mismatch"
Assert-Equal -Expected 1452 -Actual $manifest.official_api_catalog_entries `
    -Message "Official API catalog count mismatch"
Assert-Equal -Expected 1448 -Actual $manifest.official_api_enabled_routes `
    -Message "Official API enabled route count mismatch"
Assert-Equal -Expected 4 -Actual @($manifest.official_api_disabled_case_ids).Count `
    -Message "Disabled official API count mismatch"
Assert-Equal -Expected "FilePathCheckerModule" -Actual $manifest.file_path_checker_module `
    -Message "Security module name mismatch"
Assert-Equal -Expected "pyhwpx==1.6.6" -Actual $manifest.file_path_checker_source `
    -Message "Security module source mismatch"
Assert-Equal -Expected "9ac5b97c47ac8aed1e8bca27a3eef39411361d8f68c262509f0c40a8f9d21bb6" `
    -Actual $manifest.file_path_checker_sha256 -Message "Security module checksum mismatch"
$expectedDisabledApi = @(
    "action:0608:SaveHistoryItem",
    "automation:0067:IHwpObject.ExportStyle",
    "automation:0068:IHwpObject.ImportStyle",
    "automation:0365:IDHwpParameterArray.Clone"
) | Sort-Object
Assert-Equal -Expected ($expectedDisabledApi -join "|") `
    -Actual ((@($manifest.official_api_disabled_case_ids) | Sort-Object) -join "|") `
    -Message "Disabled official API identifiers mismatch"
Assert-Equal -Expected "gsg-hwp" -Actual $marketplace.plugins[0].name -Message "Marketplace plugin mismatch"
Assert-Equal -Expected "./plugins/gsg-hwp" -Actual $marketplace.plugins[0].source.path `
    -Message "Marketplace source path mismatch"
Assert-Equal -Expected "powershell.exe" -Actual $mcp.mcpServers."gsg-hwp-beta-live".command `
    -Message "MCP must start through the portable PowerShell launcher"
$installSource = Get-Content -LiteralPath (Join-Path $repositoryRoot "install.ps1") -Raw -Encoding UTF8
$updaterSource = Get-Content -LiteralPath (
    Join-Path $pluginRoot "scripts\GsgHwp.Update.psm1"
) -Raw -Encoding UTF8
$launcherSource = Get-Content -LiteralPath (
    Join-Path $pluginRoot "scripts\start-mcp.ps1"
) -Raw -Encoding UTF8
Assert-True -Condition ($installSource -match "--managed-python") `
    -Message "Installer must require an uv-managed Python"
Assert-True -Condition ($updaterSource -match "--managed-python") `
    -Message "Updater must require an uv-managed Python"
Assert-True -Condition ($launcherSource -match "\.venv\\Scripts\\python\.exe") `
    -Message "MCP launcher must use the versioned virtual environment"
Assert-True -Condition ($launcherSource -notmatch "Get-Command\s+['`"]?python") `
    -Message "MCP launcher must not fall back to a system Python"
Assert-True -Condition ($launcherSource -match "Initialize-GsgHwpRuntime") `
    -Message "MCP launcher must provision a missing runtime on first run"
Assert-True -Condition ($launcherSource -notmatch "Clone the repository") `
    -Message "MCP launcher must not tell packaged users to clone the repository"

$launcher = Join-Path $pluginRoot "addon\HancomMcpLauncher\bin\Release\HancomMcpLauncher.exe"
$eventBridge = Join-Path $pluginRoot "addon\HancomEventBridge\bin\Release\HancomEventBridge.exe"
$nativeDll = Join-Path $pluginRoot (
    "addon\HancomLiveBridgeNative\bin\{0}\HancomLiveBridge.dll" -f $manifest.native_bridge
)
Assert-Equal -Expected $manifest.launcher_sha256 `
    -Actual (Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash.ToLowerInvariant() `
    -Message "Launcher checksum mismatch"
Assert-Equal -Expected $manifest.event_bridge_sha256 `
    -Actual (Get-FileHash -LiteralPath $eventBridge -Algorithm SHA256).Hash.ToLowerInvariant() `
    -Message "Event bridge checksum mismatch"
Assert-Equal -Expected $manifest.native_sha256 `
    -Actual (Get-FileHash -LiteralPath $nativeDll -Algorithm SHA256).Hash.ToLowerInvariant() `
    -Message "Native bridge checksum mismatch"

$forbiddenPaths = @(
    ("C:" + "\Users\"),
    ("C:" + "/Users/"),
    ("C:" + "\Work\"),
    ("C:" + "/Work/"),
    ("\Admin" + "istrator\"),
    ("/Admin" + "istrator/")
)
$forbiddenTerms = @(
    ("테스트" + "파일"),
    ("미아제" + "11"),
    ("한양" + "시장"),
    ("행당" + "동")
)
$latin1 = [System.Text.Encoding]::GetEncoding(28591)
$utf8 = [System.Text.Encoding]::UTF8
$unicode = [System.Text.Encoding]::Unicode
$releaseFiles = Get-ChildItem -LiteralPath $repositoryRoot -Recurse -File |
    Where-Object {
        $_.FullName -notmatch "[\\/](\.git|\.venv|__pycache__)[\\/]"
    }

foreach ($file in $releaseFiles) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $singleByteText = $latin1.GetString($bytes)
    $utf8Text = $utf8.GetString($bytes)
    $unicodeText = $unicode.GetString($bytes)
    foreach ($forbiddenPath in $forbiddenPaths) {
        Assert-True -Condition (-not $singleByteText.Contains($forbiddenPath)) `
            -Message "Personal path found in $($file.FullName)"
        Assert-True -Condition (-not $unicodeText.Contains($forbiddenPath)) `
            -Message "Personal path found in $($file.FullName)"
        Assert-True -Condition (-not $utf8Text.Contains($forbiddenPath)) `
            -Message "Personal path found in $($file.FullName)"
    }
    foreach ($forbiddenTerm in $forbiddenTerms) {
        Assert-True -Condition (-not $singleByteText.Contains($forbiddenTerm)) `
            -Message "Personal term found in $($file.FullName)"
        Assert-True -Condition (-not $unicodeText.Contains($forbiddenTerm)) `
            -Message "Personal term found in $($file.FullName)"
        Assert-True -Condition (-not $utf8Text.Contains($forbiddenTerm)) `
            -Message "Personal term found in $($file.FullName)"
    }
}

$forbiddenArtifacts = Get-ChildItem -LiteralPath $repositoryRoot -Recurse -File |
    Where-Object {
        $_.Extension -in @(
            ".pdb", ".pyc", ".pyo", ".obj", ".lib", ".exp", ".ilk",
            ".iobj", ".ipdb", ".tlog", ".log"
        )
    }
Assert-Equal -Expected 0 -Actual @($forbiddenArtifacts).Count `
    -Message "Debug or cache artifacts remain in the release"

$forbiddenDirectories = Get-ChildItem -LiteralPath $repositoryRoot -Recurse -Directory -Force |
    Where-Object {
        $_.Name -in @(".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "obj")
    }
Assert-Equal -Expected 0 -Actual @($forbiddenDirectories).Count `
    -Message "Generated directories remain in the release"

Import-Module -Name $modulePath -Force

# ---------------------------------------------------------------------------
# Catalog self-consistency. The manifest is the truth source for every count
# the documents quote, so it has to agree with itself before anything else is
# compared against it.
# ---------------------------------------------------------------------------
foreach ($catalogName in @("worker_tools", "proxy_tools", "host_visible_tools", "qa_tools")) {
    $catalog = $manifest.tool_catalogs.$catalogName
    Assert-Equal -Expected $catalog.count -Actual @($catalog.names).Count `
        -Message "compatibility-manifest.json tool_catalogs.$catalogName count disagrees with its own names array"
}
$workerToolNames = @($manifest.tool_catalogs.worker_tools.names)
$proxyToolNames = @($manifest.tool_catalogs.proxy_tools.names)
$hostVisibleToolNames = @($manifest.tool_catalogs.host_visible_tools.names)
Assert-Equal -Expected ((($workerToolNames + $proxyToolNames) | Sort-Object -Unique) -join "|") `
    -Actual (($hostVisibleToolNames | Sort-Object -Unique) -join "|") `
    -Message "tool_catalogs.host_visible_tools is not the union of worker_tools and proxy_tools"
Assert-Equal -Expected (($workerToolNames | Sort-Object) -join "|") `
    -Actual ((@($manifest.production_tools) | Sort-Object) -join "|") `
    -Message "production_tools and tool_catalogs.worker_tools.names list different tools"
Assert-Equal -Expected (($proxyToolNames | Sort-Object) -join "|") `
    -Actual ((@($manifest.runtime_tools) | Sort-Object) -join "|") `
    -Message "runtime_tools and tool_catalogs.proxy_tools.names list different tools"
foreach ($sessionlessTool in @($manifest.production_sessionless_reads)) {
    Assert-True -Condition ($workerToolNames -ccontains $sessionlessTool) `
        -Message "production_sessionless_reads names '$sessionlessTool', which is not a worker tool"
}

$pluginServerNames = @($mcp.mcpServers.PSObject.Properties.Name)
Assert-Equal -Expected 1 -Actual $pluginServerNames.Count `
    -Message "plugins\gsg-hwp\.mcp.json must declare exactly one MCP server"
$serverName = $pluginServerNames[0]
$rootMcp = Get-Content -LiteralPath (Join-Path $repositoryRoot ".mcp.json") -Raw -Encoding UTF8 |
    ConvertFrom-Json
$rootServerNames = @($rootMcp.mcpServers.PSObject.Properties.Name)
Assert-Equal -Expected 1 -Actual $rootServerNames.Count `
    -Message "The repository .mcp.json must declare exactly one MCP server"
Assert-Equal -Expected $serverName -Actual $rootServerNames[0] `
    -Message "The repository .mcp.json and plugins\gsg-hwp\.mcp.json register different MCP server names"
$pluginIdentifier = [string]$plugin.name
$marketplaceIdentifier = [string]$marketplace.name

$workflowPath = Join-Path $repositoryRoot ".github\workflows\publish-release.yml"
$workflowSource = Get-Content -LiteralPath $workflowPath -Raw -Encoding UTF8
foreach ($qaScriptName in @(
    "auto_update_qa.ps1",
    "security_module_installation_qa.ps1",
    "release_installation_qa.ps1"
)) {
    Assert-True -Condition ($workflowSource.Contains("./tests/$qaScriptName")) `
        -Message "publish-release.yml no longer runs ./tests/$qaScriptName, so the release would publish without that gate"
}

# ---------------------------------------------------------------------------
# Documentation claims. Two scanning modes are used below.
#   harvest  - "every token of this shape must be a known fact". Runs over the
#              current-state region only, but fenced blocks are kept: the
#              install and registration commands users copy live inside fences,
#              so a version pinned there is as much a claim as one in prose.
#              Release history is skipped, and CHANGELOG.md is excluded from
#              every harvest: even its newest block is written as narrative
#              about the previous release.
#   targeted - "wherever this literal appears it must carry this value". The
#              needle is precise, so these run over whole files or over the
#              current-state region with fences kept.
# The harvest covers the four current-state documents, the three root scripts a
# user runs, and docs\release-integrity.md, whose published hashes and sizes are
# additionally recomputed from the shipped files.
# ---------------------------------------------------------------------------
$distributionVersion = [string]$manifest.distribution
$pythonVersionPath = Join-Path $pluginRoot ".python-version"
Assert-True -Condition (Test-Path -LiteralPath $pythonVersionPath -PathType Leaf) `
    -Message "plugins\gsg-hwp\.python-version is missing; the documentation gate cannot resolve the pinned Python version"
$pinnedPythonVersion = (Get-Content -LiteralPath $pythonVersionPath -Raw -Encoding UTF8).Trim()
$securityModuleVersionMatch = [regex]::Match(
    [string]$manifest.file_path_checker_source,
    '\d+\.\d+\.\d+'
)
Assert-True -Condition $securityModuleVersionMatch.Success `
    -Message "compatibility-manifest.json file_path_checker_source no longer carries a pinned version"
$shippedVersions = @(
    $distributionVersion,
    [string]$manifest.source_version,
    [string]$manifest.mcp,
    [string]$manifest.native_bridge,
    $pinnedPythonVersion,
    $securityModuleVersionMatch.Value
)
$documentedApiCounts = @(
    [string]$manifest.official_api_catalog_entries,
    [string]$manifest.official_api_enabled_routes
)

$currentStateDocuments = @("README.md", "QUICKSTART-KO.md", "AGENTS.md", "CLAUDE.md")
$releaseDocuments = $currentStateDocuments + @("CHANGELOG.md")
$rootScriptNames = @("install.ps1", "uninstall.ps1", "restore-update.ps1")
$integrityDocumentName = "docs\release-integrity.md"
$documentText = @{}
foreach ($documentName in $releaseDocuments) {
    $documentPath = Join-Path $repositoryRoot $documentName
    Assert-True -Condition (Test-Path -LiteralPath $documentPath -PathType Leaf) `
        -Message "Release document is missing: $documentName"
    $documentText[$documentName] = [System.IO.File]::ReadAllText(
        $documentPath,
        [System.Text.Encoding]::UTF8
    )
}

# ---------------------------------------------------------------------------
# docs\release-integrity.md exists to state what the shipped binaries hash to.
# A hash table nobody recomputes is the one that rots silently, so every row is
# recomputed from the file it names and cross-checked against the manifest
# fields the document says it mirrors. The release archive row is the single
# exception: CI builds that archive, so neither its hash nor its size can be
# derived here and the row is pinned by a reviewed allowlist entry instead.
# ---------------------------------------------------------------------------
$integrityPath = Join-Path $repositoryRoot $integrityDocumentName
$integrityText = [System.IO.File]::ReadAllText($integrityPath, [System.Text.Encoding]::UTF8)
$integrityManifestBinaries = @(
    [pscustomobject]@{ Path = $launcher; Field = "launcher_sha256" },
    [pscustomobject]@{ Path = $eventBridge; Field = "event_bridge_sha256" },
    [pscustomobject]@{ Path = $nativeDll; Field = "native_sha256" }
)
$integrityVerifiedFiles = @()
$integrityVerifiedSizes = @()
$integrityArchiveRows = 0
foreach ($integrityRow in [regex]::Matches(
    $integrityText,
    '(?m)^\|\s*`(?<path>[^`]+)`\s*\|\s*`(?<sha256>[0-9a-fA-F]{64})`\s*\|\s*`(?<size>\d{1,3}(?:,\d{3})*)`\s*\|\s*$'
)) {
    $integrityRelative = $integrityRow.Groups['path'].Value -replace '/', '\'
    $integrityDocumentedSize = $integrityRow.Groups['size'].Value -replace ','
    if ($integrityRelative.EndsWith(".zip", [StringComparison]::OrdinalIgnoreCase)) {
        $integrityArchiveRows++
        continue
    }
    $integrityFile = Join-Path $pluginRoot $integrityRelative
    Assert-True -Condition (Test-Path -LiteralPath $integrityFile -PathType Leaf) `
        -Message ("{0} publishes a SHA-256 for '{1}', which the release does not contain" -f
            $integrityDocumentName, $integrityRelative)
    Assert-Equal -Expected (Get-FileHash -LiteralPath $integrityFile -Algorithm SHA256).Hash.ToLowerInvariant() `
        -Actual $integrityRow.Groups['sha256'].Value.ToLowerInvariant() `
        -Message ("{0} publishes a SHA-256 for {1} that the shipped file does not have" -f
            $integrityDocumentName, $integrityRelative)
    Assert-Equal -Expected ([long](Get-Item -LiteralPath $integrityFile).Length) `
        -Actual ([long]$integrityDocumentedSize) `
        -Message ("{0} publishes a byte size for {1} that the shipped file does not have" -f
            $integrityDocumentName, $integrityRelative)
    $integrityVerifiedFiles += [System.IO.Path]::GetFullPath($integrityFile)
    $integrityVerifiedSizes += $integrityDocumentedSize
}
Assert-True -Condition ($integrityArchiveRows -ge 1) `
    -Message ("{0} no longer publishes a release archive row; the table format changed and the check stopped biting" -f
        $integrityDocumentName)
foreach ($integrityBinary in $integrityManifestBinaries) {
    Assert-True -Condition (
        $integrityVerifiedFiles -contains [System.IO.Path]::GetFullPath($integrityBinary.Path)
    ) -Message (("{0} no longer publishes a checkable SHA-256 row for the file behind " +
        "compatibility-manifest.json {1} ({2})") -f
        $integrityDocumentName, $integrityBinary.Field, $integrityBinary.Path)
}
foreach ($integrityField in @(
    "launcher_sha256",
    "event_bridge_sha256",
    "native_sha256",
    "file_path_checker_sha256"
)) {
    Assert-True -Condition $integrityText.Contains([string]$manifest.$integrityField) `
        -Message ("{0} says it mirrors compatibility-manifest.json {1}, but does not state that value" -f
            $integrityDocumentName, $integrityField)
}
Assert-True -Condition $integrityText.Contains([string]$manifest.file_path_checker_source) `
    -Message ("{0} no longer names the pinned security module source '{1}' from compatibility-manifest.json" -f
        $integrityDocumentName, $manifest.file_path_checker_source)

# Every source that must not state a version or a four-figure count this release
# does not own. ExtraCounts are numbers already proven against a shipped file.
$versionScannedSources = @()
foreach ($documentName in ($currentStateDocuments + $rootScriptNames)) {
    $versionScannedSources += [pscustomobject][ordered]@{
        Name = $documentName
        Path = Join-Path $repositoryRoot $documentName
        ExtraCounts = @()
    }
}
$versionScannedSources += [pscustomobject][ordered]@{
    Name = $integrityDocumentName
    Path = $integrityPath
    ExtraCounts = $integrityVerifiedSizes
}

foreach ($scanSource in $versionScannedSources) {
    $documentName = $scanSource.Name
    foreach ($row in (Get-GsgHwpDocumentRows -Path $scanSource.Path)) {
        foreach ($versionMatch in [regex]::Matches(
            $row.Text,
            '(?<![\d.])\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?(?![\d.])'
        )) {
            $versionToken = $versionMatch.Value
            if ($shippedVersions -ccontains $versionToken) {
                continue
            }
            Assert-True -Condition (Test-GsgHwpDocumentedClaim -File $documentName `
                -Check "semver" -Token $versionToken -Line $row.Text) `
                -Message (("Stale version string '{0}' in {1}:{2}. No component this release ships uses it " +
                    "(distribution={3}, source_version={4}, mcp={5}, native_bridge={6}, python={7}, security module={8}). " +
                    "Update the file, or add a reviewed entry to the allowlist in release_installation_qa.ps1.") -f
                    $versionToken, $documentName, $row.Number, $distributionVersion,
                    $manifest.source_version, $manifest.mcp, $manifest.native_bridge,
                    $pinnedPythonVersion, $manifest.file_path_checker_source)
        }
        foreach ($countMatch in [regex]::Matches($row.Text, '\d{1,3}(?:,\d{3})+')) {
            $countToken = $countMatch.Value
            $countDigits = $countToken -replace ','
            if ($documentedApiCounts -ccontains $countDigits) {
                continue
            }
            if ($scanSource.ExtraCounts -ccontains $countDigits) {
                continue
            }
            Assert-True -Condition (Test-GsgHwpDocumentedClaim -File $documentName `
                -Check "api_count" -Token $countToken -Line $row.Text) `
                -Message (("Stale four-figure count '{0}' in {1}:{2}. The only such numbers these files may state are " +
                    "the official API catalog size ({3}) and the routed case count ({4}) from " +
                    "compatibility-manifest.json, plus byte sizes recomputed from a shipped file. " +
                    "Update the file, or add a reviewed allowlist entry.") -f
                    $countToken, $documentName, $row.Number,
                    $manifest.official_api_catalog_entries, $manifest.official_api_enabled_routes)
        }
        foreach ($toolMatch in [regex]::Matches($row.Text, '\bhwp_[a-z0-9_]+')) {
            $toolToken = $toolMatch.Value
            if ($hostVisibleToolNames -ccontains $toolToken) {
                continue
            }
            Assert-True -Condition (Test-GsgHwpDocumentedClaim -File $documentName `
                -Check "tool_name" -Token $toolToken -Line $row.Text) `
                -Message (("{0}:{1} names the tool '{2}', which is not in tool_catalogs.host_visible_tools " +
                    "({3} tools). Either the tool was renamed or removed, or the token is not a tool name and " +
                    "needs a reviewed allowlist entry.") -f
                    $documentName, $row.Number, $toolToken, $manifest.tool_catalogs.host_visible_tools.count)
        }
    }
}

$readmePath = Join-Path $repositoryRoot "README.md"
$readmeCurrentText = ((Get-GsgHwpDocumentRows -Path $readmePath -StripFences) |
    ForEach-Object { $_.Text }) -join "`n"
$readmeFacts = @(
    @{ Label = "distribution"; Expected = $distributionVersion; Pattern = '배포 버전:\s*\*\*v([^\*]+)\*\*' },
    @{ Label = "source_version"; Expected = [string]$manifest.source_version; Pattern = '원본 소스 버전:\s*`([^`]+)`' },
    @{ Label = "mcp"; Expected = [string]$manifest.mcp; Pattern = 'MCP 런타임:\s*`([^`]+)`' },
    @{ Label = "native_bridge"; Expected = [string]$manifest.native_bridge; Pattern = 'C\+\+ 네이티브 브리지:\s*`([^`]+)`' },
    @{ Label = "protocol"; Expected = [string]$manifest.protocol; Pattern = '네이티브 프로토콜:\s*`([^`]+)`' },
    @{ Label = "graph_protocol.protocol"; Expected = [string]$manifest.graph_protocol.protocol; Pattern = '그래프 전송 프로토콜:\s*`([^`]+)`' },
    @{ Label = "developer"; Expected = [string]$manifest.developer; Pattern = '개발자:\s*\*\*([^\*]+)\*\*' }
)
foreach ($readmeFact in $readmeFacts) {
    $factMatch = [regex]::Match($readmeCurrentText, $readmeFact.Pattern)
    Assert-True -Condition $factMatch.Success `
        -Message (("README.md no longer states '{0}' in its release fact block. That block must keep one " +
            "checkable line per compatibility-manifest.json field.") -f $readmeFact.Label)
    Assert-Equal -Expected $readmeFact.Expected -Actual $factMatch.Groups[1].Value.Trim() `
        -Message ("README.md release fact '{0}' disagrees with compatibility-manifest.json" -f $readmeFact.Label)
}

$readmeToolCounts = [regex]::Match(
    $readmeCurrentText,
    '업무 도구 (?<worker>[\d,]+)개 \+ 런타임 재로드 (?<proxy>[\d,]+)개 = 총 (?<total>[\d,]+)개'
)
Assert-True -Condition $readmeToolCounts.Success `
    -Message "README.md no longer states the public tool count in the checkable '업무 도구 N개 + 런타임 재로드 M개 = 총 K개' form"
Assert-Equal -Expected $manifest.tool_catalogs.worker_tools.count `
    -Actual ([int]($readmeToolCounts.Groups['worker'].Value -replace ',')) `
    -Message "README.md work tool count disagrees with tool_catalogs.worker_tools.count"
Assert-Equal -Expected $manifest.tool_catalogs.proxy_tools.count `
    -Actual ([int]($readmeToolCounts.Groups['proxy'].Value -replace ',')) `
    -Message "README.md runtime-reload tool count disagrees with tool_catalogs.proxy_tools.count"
Assert-Equal -Expected $manifest.tool_catalogs.host_visible_tools.count `
    -Actual ([int]($readmeToolCounts.Groups['total'].Value -replace ',')) `
    -Message "README.md total tool count disagrees with tool_catalogs.host_visible_tools.count"

$readmeApiClaims = @(
    @{
        Pattern = '공식 API 카탈로그:\s*(?<catalog>[\d,]+)개 중 네이티브 라우팅 (?<routed>[\d,]+)개'
        Where = "README.md release fact block"
    },
    @{
        Pattern = '(?m)^## 공식 API (?<catalog>[\d,]+)개와 현재 라우팅 제외 (?<disabled>[\d,]+)개'
        Where = "README.md official API section heading"
    },
    @{
        Pattern = '(?<catalog>[\d,]+)개 API 사례 카탈로그'
        Where = "README.md official API section body"
    },
    @{
        Pattern = '\*\*(?<routed>[\d,]+)개 사례를 라우팅\*\*'
        Where = "README.md official API section body"
    }
)
foreach ($apiClaim in $readmeApiClaims) {
    $apiMatch = [regex]::Match($readmeCurrentText, $apiClaim.Pattern)
    Assert-True -Condition $apiMatch.Success `
        -Message ("{0} no longer states the official API numbers in a checkable form" -f $apiClaim.Where)
    if ($apiMatch.Groups['catalog'].Success) {
        Assert-Equal -Expected $manifest.official_api_catalog_entries `
            -Actual ([int]($apiMatch.Groups['catalog'].Value -replace ',')) `
            -Message ("{0} disagrees with compatibility-manifest.json official_api_catalog_entries" -f $apiClaim.Where)
    }
    if ($apiMatch.Groups['routed'].Success) {
        Assert-Equal -Expected $manifest.official_api_enabled_routes `
            -Actual ([int]($apiMatch.Groups['routed'].Value -replace ',')) `
            -Message ("{0} disagrees with compatibility-manifest.json official_api_enabled_routes" -f $apiClaim.Where)
    }
    if ($apiMatch.Groups['disabled'].Success) {
        Assert-Equal -Expected @($manifest.official_api_disabled_case_ids).Count `
            -Actual ([int]($apiMatch.Groups['disabled'].Value -replace ',')) `
            -Message ("{0} disagrees with the number of official_api_disabled_case_ids entries" -f $apiClaim.Where)
    }
}
foreach ($disabledCaseId in @($manifest.official_api_disabled_case_ids)) {
    $caseParts = $disabledCaseId -split ":", 3
    Assert-Equal -Expected 3 -Actual $caseParts.Count `
        -Message "compatibility-manifest.json official_api_disabled_case_ids entry '$disabledCaseId' is not '<kind>:<number>:<api>'"
    $caseKey = "{0}{1}:{2}{0}" -f [char]0x60, $caseParts[0], $caseParts[1]
    Assert-True -Condition $readmeCurrentText.Contains($caseKey) `
        -Message "README.md no longer lists the disabled official API case $($caseParts[0]):$($caseParts[1]) in its exclusion table"
    Assert-True -Condition $readmeCurrentText.Contains($caseParts[2]) `
        -Message "README.md no longer names the disabled official API '$($caseParts[2])' in its exclusion table"
}

$readmeReleaseHeadings = [regex]::Matches(
    $documentText["README.md"],
    '(?m)^### v(?<version>\d+\.\d+\.\d+) '
)
Assert-True -Condition ($readmeReleaseHeadings.Count -ge 1) `
    -Message "README.md has no '### v<version> ...' release section; the documentation gate uses that heading to separate current state from history"
Assert-Equal -Expected $distributionVersion -Actual $readmeReleaseHeadings[0].Groups['version'].Value `
    -Message "The newest README.md release section is not the version being released"
foreach ($documentName in @("QUICKSTART-KO.md", "AGENTS.md", "CLAUDE.md")) {
    Assert-True -Condition (-not [regex]::IsMatch(
        $documentText[$documentName],
        '(?m)^#{2,3} .*\d+\.\d+\.\d+.*(변경|릴리스|이력)'
    )) -Message (("{0} gained a version-history section. This gate treats the whole file as current state; " +
        "teach Get-GsgHwpDocumentRows in release_installation_qa.ps1 about the new region before adding " +
        "history here.") -f $documentName)
}

$changelogHeadings = [regex]::Matches(
    $documentText["CHANGELOG.md"],
    '(?m)^## \[(?<version>[^\]]+)\] - (?<date>\d{4}-\d{2}-\d{2})\s*$'
)
Assert-True -Condition ($changelogHeadings.Count -ge 1) `
    -Message "CHANGELOG.md has no '## [version] - YYYY-MM-DD' heading"
Assert-Equal -Expected $distributionVersion -Actual $changelogHeadings[0].Groups['version'].Value `
    -Message "The newest CHANGELOG.md entry is not the version being released"
$changelogVersions = @($changelogHeadings | ForEach-Object { $_.Groups['version'].Value })
Assert-Equal -Expected $changelogVersions.Count `
    -Actual (@($changelogVersions | Sort-Object -Unique)).Count `
    -Message "CHANGELOG.md repeats a version heading"
$changelogCurrentText = ((Get-GsgHwpDocumentRows `
    -Path (Join-Path $repositoryRoot "CHANGELOG.md") -StripFences) |
    ForEach-Object { $_.Text }) -join "`n"
$componentClaims = @(
    @{ Label = "native_bridge"; Expected = [string]$manifest.native_bridge; Pattern = '네이티브 브리지 `([^`]+)`' },
    @{ Label = "mcp"; Expected = [string]$manifest.mcp; Pattern = 'MCP 런타임 `([^`]+)`' },
    @{ Label = "protocol"; Expected = [string]$manifest.protocol; Pattern = '프로토콜 `([^`]+)`' }
)
$componentClaimCounts = @()
$componentClaimTotal = 0
foreach ($componentClaim in $componentClaims) {
    $claimMatches = [regex]::Matches($changelogCurrentText, $componentClaim.Pattern)
    $componentClaimCounts += "{0}={1}" -f $componentClaim.Label, $claimMatches.Count
    $componentClaimTotal += $claimMatches.Count
    foreach ($claimMatch in $claimMatches) {
        Assert-Equal -Expected $componentClaim.Expected -Actual $claimMatch.Groups[1].Value `
            -Message (("The newest CHANGELOG.md entry states a '{0}' value that disagrees with " +
                "compatibility-manifest.json") -f $componentClaim.Label)
    }
}
# A scanner that matches nothing passes for free. Report what it caught and
# fail when the newest entry stops stating any component version at all, so the
# check cannot fall asleep the way an unmatched pattern silently would.
Assert-True -Condition ($componentClaimTotal -ge 1) `
    -Message ("The newest CHANGELOG.md entry states none of the component versions this check reads " +
        "(" + (($componentClaims | ForEach-Object { $_.Label }) -join ", ") + "). Either the entry " +
        "stopped naming them or the patterns in release_installation_qa.ps1 no longer match the wording.")

$identitySources = @{}
foreach ($documentName in $releaseDocuments) {
    $identitySources[$documentName] = $documentText[$documentName]
}
foreach ($scriptName in @("install.ps1", "uninstall.ps1", "restore-update.ps1")) {
    $identitySources[$scriptName] = Get-Content `
        -LiteralPath (Join-Path $repositoryRoot $scriptName) -Raw -Encoding UTF8
}
foreach ($sourceName in ($identitySources.Keys | Sort-Object)) {
    $sourceText = $identitySources[$sourceName]
    foreach ($registrationMatch in [regex]::Matches(
        $sourceText,
        'claude mcp add[^\r\n]*?--scope user\s+(?<name>[A-Za-z0-9._-]+)'
    )) {
        Assert-Equal -Expected $serverName -Actual $registrationMatch.Groups['name'].Value `
            -Message "$sourceName registers an MCP server name that plugins\gsg-hwp\.mcp.json does not declare"
    }
    foreach ($prefixMatch in [regex]::Matches($sourceText, 'mcp__(?<name>[A-Za-z0-9._-]+)__')) {
        Assert-Equal -Expected $serverName -Actual $prefixMatch.Groups['name'].Value `
            -Message "$sourceName shows an MCP tool prefix for a server name that plugins\gsg-hwp\.mcp.json does not declare"
    }
    foreach ($codexMatch in [regex]::Matches(
        $sourceText,
        'codex plugin (?:add|remove)\s+(?<plugin>[A-Za-z0-9._-]+)@(?<marketplace>[A-Za-z0-9._-]+)'
    )) {
        Assert-Equal -Expected $pluginIdentifier -Actual $codexMatch.Groups['plugin'].Value `
            -Message "$sourceName names a Codex plugin that .codex-plugin\plugin.json does not declare"
        Assert-Equal -Expected $marketplaceIdentifier `
            -Actual $codexMatch.Groups['marketplace'].Value `
            -Message "$sourceName names a Codex marketplace that .agents\plugins\marketplace.json does not declare"
    }
    foreach ($marketplaceMatch in [regex]::Matches(
        $sourceText,
        'codex plugin marketplace (?:remove|upgrade)\s+(?<name>[A-Za-z0-9._-]+)'
    )) {
        Assert-Equal -Expected $marketplaceIdentifier -Actual $marketplaceMatch.Groups['name'].Value `
            -Message "$sourceName names a Codex marketplace that .agents\plugins\marketplace.json does not declare"
    }
}
$configurationExample = [regex]::Match(
    $documentText["README.md"],
    '"mcpServers"\s*:\s*\{\s*"(?<name>[^"]+)"'
)
Assert-True -Condition $configurationExample.Success `
    -Message "README.md no longer shows a generic stdio MCP configuration example"
Assert-Equal -Expected $serverName -Actual $configurationExample.Groups['name'].Value `
    -Message "The stdio MCP configuration example in README.md registers a different server name"
foreach ($documentName in $currentStateDocuments) {
    Assert-True -Condition $documentText[$documentName].Contains("claude mcp remove $serverName") `
        -Message "$documentName does not tell the user to remove the MCP registration named '$serverName'"
}
Assert-True -Condition $identitySources["uninstall.ps1"].Contains("claude mcp remove $serverName") `
    -Message "uninstall.ps1 does not print the removal command for the MCP registration named '$serverName'"

$updateModuleSource = Get-Content -LiteralPath (
    Join-Path $pluginRoot "scripts\GsgHwp.Update.psm1"
) -Raw -Encoding UTF8
$assetTemplateMatch = [regex]::Match($updateModuleSource, '(?<prefix>[A-Za-z0-9._-]+)v\{0\}\.zip')
Assert-True -Condition $assetTemplateMatch.Success `
    -Message "GsgHwp.Update.psm1 no longer builds a '<prefix>v<version>.zip' release asset name, so the documented asset name cannot be checked"
$releaseAssetPrefix = $assetTemplateMatch.Groups['prefix'].Value
$expectedAssetPattern = '^' + [regex]::Escape($releaseAssetPrefix) + 'v(?:<[^<>]+>|' +
    [regex]::Escape($distributionVersion) + ')\.zip$'
foreach ($documentName in ($releaseDocuments + @($integrityDocumentName))) {
    foreach ($row in (Get-GsgHwpDocumentRows -Path (Join-Path $repositoryRoot $documentName))) {
        foreach ($archiveMatch in [regex]::Matches($row.Text, '[^\s`"''(),\\/]+\.zip')) {
            Assert-True -Condition ([regex]::IsMatch($archiveMatch.Value, $expectedAssetPattern)) `
                -Message (("{0}:{1} names the release archive '{2}', but publish-release.yml only ever uploads " +
                    "'{3}v<version>.zip'. Nothing in this repository builds the named file.") -f
                    $documentName, $row.Number, $archiveMatch.Value, $releaseAssetPrefix)
        }
    }
}

$installationModuleSource = Get-Content -LiteralPath $modulePath -Raw -Encoding UTF8
$moduleConstants = @{}
foreach ($constantName in @(
    "DefaultModulesKey",
    "DefaultAutomationModulesKey",
    "ModuleName",
    "SecurityModuleName"
)) {
    $constantMatch = [regex]::Match(
        $installationModuleSource,
        '\$script:' + $constantName + '\s*=\s*"(?<value>[^"]+)"'
    )
    Assert-True -Condition $constantMatch.Success `
        -Message "GsgHwp.Installation.psm1 no longer defines `$script:$constantName, so the documented registry facts cannot be checked"
    $moduleConstants[$constantName] = $constantMatch.Groups['value'].Value
}
Assert-Equal -Expected ([string]$manifest.file_path_checker_module) `
    -Actual $moduleConstants["SecurityModuleName"] `
    -Message "GsgHwp.Installation.psm1 and compatibility-manifest.json name the file path security module differently"
$registryFacts = @(
    $moduleConstants["DefaultModulesKey"],
    ($moduleConstants["DefaultModulesKey"] + "\Uses"),
    $moduleConstants["DefaultAutomationModulesKey"],
    $moduleConstants["SecurityModuleName"]
)
foreach ($sourceName in @("README.md", "AGENTS.md", "install.ps1")) {
    foreach ($registryFact in $registryFacts) {
        Assert-True -Condition $identitySources[$sourceName].Contains($registryFact) `
            -Message "$sourceName does not state the registry fact '$registryFact' that GsgHwp.Installation.psm1 actually writes"
    }
}
foreach ($sourceName in @("README.md", "install.ps1")) {
    Assert-True -Condition $identitySources[$sourceName].Contains($moduleConstants["ModuleName"]) `
        -Message "$sourceName does not name the registry value that GsgHwp.Installation.psm1 writes for the native bridge"
}

$documentedLocalAppData = "%LOCALAPPDATA%"
$documentedPaths = Get-GsgHwpPaths -PackageRoot $pluginRoot -LocalAppData $documentedLocalAppData
$documentedPrefix = $documentedLocalAppData + "\"
$documentedLayout = @(
    @{ Label = "versioned Python runtime root"; Value = $documentedPaths.RuntimeRoot },
    @{ Label = "file path security module"; Value = $documentedPaths.SecurityDll },
    @{ Label = "active installation state"; Value = $documentedPaths.ActiveState },
    @{ Label = "backup root"; Value = $documentedPaths.BackupsRoot },
    @{ Label = "downloaded package root"; Value = $documentedPaths.PackagesRoot },
    @{ Label = "automatic update state"; Value = $documentedPaths.UpdaterRoot },
    @{ Label = "native bridge root"; Value = $documentedPaths.NativeRoot }
)
foreach ($layoutEntry in $documentedLayout) {
    $relativePath = $layoutEntry.Value.Substring($documentedPrefix.Length)
    foreach ($documentName in @("README.md", "CLAUDE.md")) {
        Assert-True -Condition $documentText[$documentName].Contains($relativePath) `
            -Message ("{0} no longer documents the {1} that GsgHwp.Installation.psm1 uses: {2}" -f
                $documentName, $layoutEntry.Label, $relativePath)
    }
}
foreach ($layoutLeaf in @(
    (Split-Path -Leaf $documentedPaths.NativeDll),
    (Split-Path -Leaf $documentedPaths.RuntimeEnvironment)
)) {
    foreach ($documentName in @("README.md", "CLAUDE.md")) {
        Assert-True -Condition $documentText[$documentName].Contains($layoutLeaf) `
            -Message "$documentName no longer names '$layoutLeaf', which GsgHwp.Installation.psm1 creates"
    }
}

$updatePolicy = Get-Content -LiteralPath (Join-Path $pluginRoot "update-policy.json") `
    -Raw -Encoding UTF8 | ConvertFrom-Json
$updateIntervalHours = [int]$updatePolicy.check_interval_hours
foreach ($documentName in @("README.md", "AGENTS.md", "CLAUDE.md")) {
    $documentPath = Join-Path $repositoryRoot $documentName
    foreach ($row in (Get-GsgHwpDocumentRows -Path $documentPath -StripFences)) {
        foreach ($intervalMatch in [regex]::Matches(
            $row.Text,
            '(?:(?<hours>\d+)시간 간격|현재 \*{0,2}(?<hours>\d+)시간)'
        )) {
            Assert-Equal -Expected $updateIntervalHours `
                -Actual ([int]$intervalMatch.Groups['hours'].Value) `
                -Message (("{0}:{1} states an automatic update interval that disagrees with " +
                    "update-policy.json check_interval_hours") -f $documentName, $row.Number)
        }
    }
}

$rootEntryNames = @(Get-ChildItem -LiteralPath $repositoryRoot -Force | ForEach-Object { $_.Name })
$pluginEntryNames = @(Get-ChildItem -LiteralPath $pluginRoot -Force | ForEach-Object { $_.Name })
foreach ($documentName in ($releaseDocuments + @($integrityDocumentName))) {
    $documentPath = Join-Path $repositoryRoot $documentName
    foreach ($row in (Get-GsgHwpDocumentRows -Path $documentPath -StripFences)) {
        foreach ($linkMatch in [regex]::Matches($row.Text, '\]\((?<target>[^)]+)\)')) {
            $linkTarget = $linkMatch.Groups['target'].Value
            if ($linkTarget -match '^(https?:|mailto:|#)') {
                continue
            }
            Assert-True -Condition (Test-Path -LiteralPath (
                Join-Path $repositoryRoot ($linkTarget -replace '/', '\')
            )) -Message ("{0}:{1} links to '{2}', which does not exist in the release" -f
                $documentName, $row.Number, $linkTarget)
        }
        foreach ($codeMatch in [regex]::Matches($row.Text, '`(?<token>[^`]+)`')) {
            $codeToken = $codeMatch.Groups['token'].Value
            if ($codeToken -notmatch '[\\/]' -or $codeToken -match '[<>]') {
                continue
            }
            $normalizedToken = ($codeToken -replace '^\.[\\/]', '') -replace '/', '\'
            $firstSegment = ($normalizedToken -split '\\')[0]
            $searchRoot = $null
            if ($rootEntryNames -ccontains $firstSegment) {
                $searchRoot = $repositoryRoot
            }
            elseif ($pluginEntryNames -ccontains $firstSegment) {
                $searchRoot = $pluginRoot
            }
            if ($null -eq $searchRoot) {
                continue
            }
            Assert-True -Condition (Test-Path -LiteralPath (
                Join-Path $searchRoot $normalizedToken.TrimEnd("\")
            )) -Message ("{0}:{1} points at '{2}', which does not exist in the release" -f
                $documentName, $row.Number, $codeToken)
        }
    }
}

$readmeLines = $documentText["README.md"] -split "`r?`n"
$treeStart = -1
for ($index = 0; $index -lt $readmeLines.Count; $index++) {
    if ($readmeLines[$index] -match '^gsghwp/\s*$') {
        $treeStart = $index
        break
    }
}
Assert-True -Condition ($treeStart -ge 0) `
    -Message "README.md no longer contains the 'gsghwp/' repository structure tree"
$treeParents = @{ 0 = "" }
$treeEntryCount = 0
for ($index = $treeStart + 1; $index -lt $readmeLines.Count; $index++) {
    $treeLine = $readmeLines[$index]
    if ($treeLine -match '^\s*```') {
        break
    }
    $markerIndex = $treeLine.IndexOfAny([char[]]@([char]0x251C, [char]0x2514))
    if ($markerIndex -lt 0) {
        continue
    }
    $treeDepth = [int][math]::Floor($markerIndex / 3)
    if (-not $treeParents.ContainsKey($treeDepth)) {
        $treeDepth = 0
    }
    $treeParent = $treeParents[$treeDepth]
    $treeContent = (($treeLine.Substring($markerIndex + 1) -replace
        ('^[' + [char]0x2500 + '\s]+'), '') -split '#')[0].Trim()
    $treeNames = @($treeContent -split '\s+' | Where-Object { $_ -ne "/" -and $_ -ne "" })
    foreach ($treeName in $treeNames) {
        $treeRelative = ($treeParent + $treeName.TrimEnd("/")) -replace '/', '\'
        $treeEntryCount++
        Assert-True -Condition (Test-Path -LiteralPath (Join-Path $repositoryRoot $treeRelative)) `
            -Message ("README.md:{0} lists '{1}' in the repository structure tree, but the release does not contain it" -f
                ($index + 1), $treeRelative)
    }
    if ($treeNames.Count -eq 1 -and $treeNames[0].EndsWith("/")) {
        $treeParents[$treeDepth + 1] = $treeParent + $treeNames[0]
    }
}
Assert-True -Condition ($treeEntryCount -ge 20) `
    -Message "README.md repository structure tree was parsed but yielded almost no entries; the tree format changed and the check stopped biting"

$productSourceText = ""
foreach ($productScript in ($powerShellFiles |
    Where-Object { $_.FullName -notmatch "[\\/]tests[\\/]" })) {
    $productSourceText += [System.IO.File]::ReadAllText(
        $productScript.FullName,
        [System.Text.Encoding]::UTF8
    )
}
$claudeLines = $documentText["CLAUDE.md"] -split "`r?`n"
$stateTableStart = -1
for ($index = 0; $index -lt $claudeLines.Count; $index++) {
    if ($claudeLines[$index] -match '^## 로그와 상태 파일\s*$') {
        $stateTableStart = $index
        break
    }
}
Assert-True -Condition ($stateTableStart -ge 0) `
    -Message "CLAUDE.md no longer has a '로그와 상태 파일' section listing the runtime state files"
$stateFileNames = @()
for ($index = $stateTableStart + 1; $index -lt $claudeLines.Count; $index++) {
    if ($claudeLines[$index] -match '^## ') {
        break
    }
    foreach ($codeMatch in [regex]::Matches($claudeLines[$index], '`(?<token>[^`]+)`')) {
        $codeToken = $codeMatch.Groups['token'].Value
        if ($codeToken -notmatch '\\') {
            continue
        }
        foreach ($segment in ($codeToken -split '\\')) {
            if ([string]::IsNullOrWhiteSpace($segment) -or $segment -match '[<>%]') {
                continue
            }
            if ($stateFileNames -ccontains $segment) {
                continue
            }
            $stateFileNames += $segment
        }
    }
}
Assert-True -Condition ($stateFileNames.Count -ge 12) `
    -Message "CLAUDE.md '로그와 상태 파일' section no longer names concrete runtime paths; the check stopped biting"
foreach ($stateFileName in $stateFileNames) {
    # The segment has to appear in a shipped script as a whole path component,
    # delimited by a quote or a separator. A bare substring match would accept
    # "update" just because "update-policy.json" exists.
    Assert-True -Condition ([regex]::IsMatch(
        $productSourceText,
        '["\\]' + [regex]::Escape($stateFileName) + '["\\]'
    )) -Message ("CLAUDE.md documents the runtime path segment '{0}', but no shipped PowerShell script ever builds a path with that component" -f
        $stateFileName)
}

foreach ($allowlistEntry in $documentationClaimAllowlist) {
    Assert-True -Condition $allowlistEntry.Consumed `
        -Message (("The documentation allowlist entry for {0} ({1} '{2}') no longer matches anything. " +
            "Either the line changed and the exception needs re-review, or the exception is obsolete " +
            "and must be deleted from release_installation_qa.ps1.") -f
            $allowlistEntry.File, $allowlistEntry.Check, $allowlistEntry.Token)
}

$testId = [Guid]::NewGuid().ToString("N")
$registryRoot = "Software\GSG_HWP_ReleaseQa_$testId"
$modulesKey = "$registryRoot\Modules"
$automationModulesKey = "$registryRoot\AutomationModules"
$localAppData = Join-Path ([System.IO.Path]::GetTempPath()) "GsgHwpReleaseQa-$testId"
$paths = Get-GsgHwpPaths -PackageRoot $pluginRoot -LocalAppData $localAppData
Assert-Equal -Expected (
    Join-Path $localAppData (
        "GSG_HWP\runtime\{0}\.venv" -f $manifest.distribution
    )
) -Actual $paths.RuntimeEnvironment `
    -Message "Runtime must use a distribution-specific .venv"
$originalDll = [byte[]](10, 20, 30, 40)
$originalSecurityDll = [byte[]](50, 60, 70, 80)

try {
    New-Item -ItemType Directory -Path $paths.RuntimeVersionRoot -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $paths.RuntimeVersionRoot "qa-runtime.txt") `
        -Value "isolated" -Encoding ASCII
    New-Item -ItemType Directory -Path (Split-Path -Parent $paths.NativeDll) -Force | Out-Null
    [System.IO.File]::WriteAllBytes($paths.NativeDll, $originalDll)
    New-Item -ItemType Directory -Path (Split-Path -Parent $paths.SecuritySourceDll) -Force |
        Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $paths.SecurityDll) -Force |
        Out-Null
    [System.IO.File]::WriteAllBytes($paths.SecuritySourceDll, [byte[]](91, 92, 93, 94))
    [System.IO.File]::WriteAllBytes($paths.SecurityDll, $originalSecurityDll)

    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($modulesKey)
    try {
        $key.SetValue("한컴브릿지", "original-bridge.dll", [Microsoft.Win32.RegistryValueKind]::String)
    }
    finally {
        $key.Dispose()
    }
    $automationKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($automationModulesKey)
    try {
        $automationKey.SetValue(
            "FilePathCheckerModule",
            "original-security.dll",
            [Microsoft.Win32.RegistryValueKind]::String
        )
    }
    finally {
        $automationKey.Dispose()
    }

    $installResult = Install-GsgHwpNative -Paths $paths `
        -PackageVersion $manifest.distribution `
        -ModulesKeyPath $modulesKey -AutomationModulesKeyPath $automationModulesKey
    Assert-True -Condition $installResult.Changed -Message "Native install did not report a change"
    Assert-True -Condition (Test-Path -LiteralPath $paths.ActiveState) `
        -Message "Active installation state was not written"

    $installedKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($modulesKey)
    $installedUsesKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$modulesKey\Uses")
    $installedAutomationKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(
        $automationModulesKey
    )
    try {
        Assert-Equal -Expected $paths.NativeDll -Actual $installedKey.GetValue("한컴브릿지") `
            -Message "Native registry path was not installed"
        Assert-Equal -Expected 1 -Actual $installedUsesKey.GetValue("한컴브릿지") `
            -Message "Native registry enable flag was not installed"
        Assert-Equal -Expected $paths.SecurityDll `
            -Actual $installedAutomationKey.GetValue("FilePathCheckerModule") `
            -Message "Security registry path was not installed"
    }
    finally {
        $installedKey.Dispose()
        $installedUsesKey.Dispose()
        $installedAutomationKey.Dispose()
    }

    $restoreResult = Restore-GsgHwpNative -Paths $paths -ModulesKeyPath $modulesKey `
        -AutomationModulesKeyPath $automationModulesKey
    Assert-True -Condition $restoreResult.Restored -Message "Native restore did not run"
    Assert-Equal -Expected ($originalDll -join ",") `
        -Actual (([System.IO.File]::ReadAllBytes($paths.NativeDll)) -join ",") `
        -Message "Original DLL was not restored"
    Assert-Equal -Expected ($originalSecurityDll -join ",") `
        -Actual (([System.IO.File]::ReadAllBytes($paths.SecurityDll)) -join ",") `
        -Message "Original security DLL was not restored"

    $restoredKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($modulesKey)
    $restoredUsesKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("$modulesKey\Uses")
    $restoredAutomationKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(
        $automationModulesKey
    )
    try {
        Assert-Equal -Expected "original-bridge.dll" -Actual $restoredKey.GetValue("한컴브릿지") `
            -Message "Original registry value was not restored"
        Assert-True -Condition (
            $null -eq $restoredUsesKey -or
            $restoredUsesKey.GetValueNames() -notcontains "한컴브릿지"
        ) -Message "Registry value that was originally absent was not removed"
        Assert-Equal -Expected "original-security.dll" `
            -Actual $restoredAutomationKey.GetValue("FilePathCheckerModule") `
            -Message "Original security registry value was not restored"
    }
    finally {
        $restoredKey.Dispose()
        if ($null -ne $restoredUsesKey) {
            $restoredUsesKey.Dispose()
        }
        $restoredAutomationKey.Dispose()
    }

    Assert-True -Condition (Test-Path -LiteralPath $restoreResult.BackupFile) `
        -Message "Recovery backup was not preserved after restore"

    Remove-GsgHwpRuntime -Paths $paths
    Assert-True -Condition (-not (Test-Path -LiteralPath $paths.RuntimeVersionRoot)) `
        -Message "Versioned Python runtime was not removed"
}
finally {
    [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($registryRoot, $false)
    if (Test-Path -LiteralPath $localAppData) {
        $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        $resolvedTarget = [System.IO.Path]::GetFullPath($localAppData)
        Assert-True -Condition $resolvedTarget.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) `
            -Message "QA cleanup target escaped the temporary directory"
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
}

Write-Output ("INFO: CHANGELOG component version claims matched: " + ($componentClaimCounts -join ", "))
Write-Output ("INFO: docs\release-integrity.md rows recomputed from shipped files: {0} (release archive rows pinned by allowlist: {1})" -f
    $integrityVerifiedFiles.Count, $integrityArchiveRows)
Write-Output "PASS: release structure, privacy scan, checksums, tool catalog consistency, documentation claims (versions, counts, tool names, MCP and Codex identifiers, release asset, registry and data paths, referenced files), published integrity hashes and sizes, isolated .venv, native/security registry and DLL restore, runtime cleanup"
