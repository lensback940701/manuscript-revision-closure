$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Release = Join-Path $ProjectRoot 'release'
$Work = Join-Path $ProjectRoot '.build\pyinstaller'
$Spec = Join-Path $ProjectRoot '.build\spec'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Missing .venv. Create it with: python -m venv .venv'
}

New-Item -ItemType Directory -Force -Path $Release, $Work, $Spec | Out-Null

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name ManuscriptRevisionClosure `
    --distpath $Release `
    --workpath $Work `
    --specpath $Spec `
    --paths $ProjectRoot `
    --collect-all pypdf `
    --add-data "$(Join-Path $ProjectRoot 'SKILL.md');." `
    --add-data "$(Join-Path $ProjectRoot 'references\hold-code-schema.md');references" `
    --add-data "$(Join-Path $ProjectRoot 'standalone\AGENT.md');standalone" `
    --add-data "$(Join-Path $ProjectRoot 'LICENSE');." `
    --add-data "$(Join-Path $ProjectRoot 'NOTICE');." `
    (Join-Path $ProjectRoot 'mrc_standalone.py')

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$Exe = Join-Path $Release 'ManuscriptRevisionClosure.exe'
$Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $Exe
$Receipt = [ordered]@{
    filename = [IO.Path]::GetFileName($Exe)
    bytes = (Get-Item -LiteralPath $Exe).Length
    sha256 = $Hash.Hash
    standalone_version = '0.6.2'
    skill_version = '0.2.1'
    presentation_transaction_version = 'mrc-presentation-transaction-1.0'
    presentation_source_contract_version = 'mrc-presentation-source-2.0'
    presentation_repair_contract_version = 'mrc-presentation-repair-2.0'
    language_contract_version = 'mrc-zh-display-language-1.0'
    interpretation_contract_version = 'mrc-public-interpretation-2.0'
    intake_contract_version = 'mrc-manuscript-intake-1.0'
    coverage_contract_version = 'mrc-whole-manuscript-coverage-1.0'
    adjudication_contract_version = 'mrc-root-cause-adjudication-1.0'
    contradiction_gate_version = 'mrc-cross-stage-contradiction-gate-1.0'
    interpretation_agent_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $ProjectRoot 'standalone\AGENT.md')).Hash
    pyinstaller = '6.22.2'
    pypdf = '6.16.2'
    skill_donor_commit = 'fd30bf0daf0e8557b315491c72479b1b2598c22f'
    codex_reference_commit = 'd5caceccb1ee5bf94c081b995575ce4860e0912b'
}
$Receipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Release 'BUILD_RECEIPT.json') -Encoding utf8NoBOM
$Hash.Hash | Set-Content -LiteralPath (Join-Path $Release 'ManuscriptRevisionClosure.exe.sha256') -Encoding ascii
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'STANDALONE.zh-CN.md') -Destination $Release -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'docs\PORTABILITY.zh-CN.md') -Destination $Release -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'docs\HARNESS_EQUIVALENCE_AUDIT.zh-CN.md') -Destination (Join-Path $Release 'HARNESS_AUDIT.zh-CN.md') -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'docs\NATIVE_PRESENTATION_TRANSACTION_AUDIT.zh-CN.md') -Destination (Join-Path $Release 'NATIVE_PRESENTATION_TRANSACTION_AUDIT.zh-CN.md') -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'standalone\AGENT.md') -Destination (Join-Path $Release 'INTERPRETATION_AGENT.md') -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'LICENSE') -Destination $Release -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'THIRD_PARTY_NOTICES.md') -Destination $Release -Force
Write-Host "Built $Exe"
Write-Host "SHA-256 $($Hash.Hash)"
