$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Release = Join-Path $ProjectRoot 'release'
$Work = Join-Path $ProjectRoot '.build\pyinstaller'
$Spec = Join-Path $ProjectRoot '.build\spec'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = $ProjectRoot

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Missing .venv. Create it with: python -m venv .venv'
}

New-Item -ItemType Directory -Force -Path $Release, $Work, $Spec | Out-Null

& $Python -B -m PyInstaller `
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
$ContractJson = & $Python -B -c "import hashlib,json; from standalone.assessor import PROVIDER_TRANSMISSION_CONSENT_VERSION; from standalone.harness import BASIS_REASON_CODES,COVERAGE_CONTRACT_VERSION,COVERAGE_JSON_SCHEMA,INTAKE_CONTRACT_VERSION,MANUSCRIPT_BASIS_CONTRACT_VERSION,TITLE_EVIDENCE_CONTRACT_VERSION,WHOLE_MANUSCRIPT_BASIS_STATES,HEADING_NUMBERING_STYLE_REVIEW,HEADING_NUMBERING_STYLE_REVIEW_ZH,build_adjudication_json_schema,schema_sha256,title_evidence_contract; from standalone.providers import PROVIDERS,PROVIDER_REQUEST_TRANSACTION_VERSION,PROVIDER_ERROR_DETAIL_CONTRACT_VERSION,provider_capability; from scripts.closure_state import TECHNICAL_HOLD_CONTRACT_VERSION,TECHNICAL_FAILED_STAGES,NEXT_TECHNICAL_HOLD; canon=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')); sha=lambda v:hashlib.sha256(canon(v).encode()).hexdigest(); caps={k:provider_capability(k) for k in sorted(PROVIDERS)}; title_evidence=title_evidence_contract(); intake={'version':INTAKE_CONTRACT_VERSION,'effective_text_only':True,'format_cannot_block_coverage':True,'title_evidence_contract_version':TITLE_EVIDENCE_CONTRACT_VERSION,'advisory_code':HEADING_NUMBERING_STYLE_REVIEW,'advisory_text_zh':HEADING_NUMBERING_STYLE_REVIEW_ZH}; basis={'version':MANUSCRIPT_BASIS_CONTRACT_VERSION,'coverage_contract_version':COVERAGE_CONTRACT_VERSION,'states':sorted(WHOLE_MANUSCRIPT_BASIS_STATES),'reason_codes':sorted(BASIS_REASON_CODES),'separate_request':False}; consent={'version':PROVIDER_TRANSMISSION_CONSENT_VERSION,'binding':['artifact_sha256','provider','model'],'one_run_only':True,'default_authorized':False}; technical={'version':TECHNICAL_HOLD_CONTRACT_VERSION,'failed_stages':sorted(TECHNICAL_FAILED_STAGES),'next_action':NEXT_TECHNICAL_HOLD}; provider_error={'transaction_version':PROVIDER_REQUEST_TRANSACTION_VERSION,'detail_version':PROVIDER_ERROR_DETAIL_CONTRACT_VERSION,'max_detail_chars':240}; print(json.dumps({'coverage_contract_version':COVERAGE_CONTRACT_VERSION,'coverage_schema_sha256':schema_sha256(COVERAGE_JSON_SCHEMA),'empty_candidate_adjudication_schema_sha256':schema_sha256(build_adjudication_json_schema({'root_cause_candidate_dimensions':[]})),'provider_capability_registry_sha256':sha(caps),'intake_contract_version':INTAKE_CONTRACT_VERSION,'intake_contract_sha256':sha(intake),'title_evidence_contract_version':TITLE_EVIDENCE_CONTRACT_VERSION,'title_evidence_contract_sha256':sha(title_evidence),'manuscript_basis_contract_version':MANUSCRIPT_BASIS_CONTRACT_VERSION,'manuscript_basis_contract_sha256':sha(basis),'provider_transmission_consent_contract_version':PROVIDER_TRANSMISSION_CONSENT_VERSION,'provider_transmission_consent_contract_sha256':sha(consent),'technical_hold_receipt_contract_version':TECHNICAL_HOLD_CONTRACT_VERSION,'technical_hold_receipt_contract_sha256':sha(technical),'provider_request_transaction_version':PROVIDER_REQUEST_TRANSACTION_VERSION,'provider_error_detail_contract_version':PROVIDER_ERROR_DETAIL_CONTRACT_VERSION,'provider_error_detail_contract_sha256':sha(provider_error)}))"
if ($LASTEXITCODE -ne 0) {
    throw "Technical contract hash extraction failed with exit code $LASTEXITCODE"
}
$Contracts = $ContractJson | ConvertFrom-Json
$SchemaContractJson = & $Python -B -c "import hashlib,json; from standalone.harness import ADJUDICATION_CONTRACT_VERSION,AFFIRMATIVE_STOP_CONTRACT_VERSION,CANDIDATE_BINDING_CONTRACT_VERSION,CONTRADICTION_GATE_VERSION,DYNAMIC_ADJUDICATION_SCHEMA_VERSION,SCHEMA_DEFINITION_LINT_VERSION,SCHEMA_DELIVERY_CONTRACT_VERSION,affirmative_stop_contract,candidate_binding_contract,dynamic_adjudication_schema_contract,schema_definition_lint_contract,schema_delivery_contract; from standalone.presentation_transaction import MACHINE_STATE_CONTRACT_VERSION; canon=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')); sha=lambda v:hashlib.sha256(canon(v).encode()).hexdigest(); delivery=schema_delivery_contract(); dynamic=dynamic_adjudication_schema_contract(); binding=candidate_binding_contract(); affirmative=affirmative_stop_contract(); lint=schema_definition_lint_contract(); print(json.dumps({'adjudication_contract_version':ADJUDICATION_CONTRACT_VERSION,'contradiction_gate_version':CONTRADICTION_GATE_VERSION,'machine_state_contract_version':MACHINE_STATE_CONTRACT_VERSION,'schema_delivery_contract_version':SCHEMA_DELIVERY_CONTRACT_VERSION,'schema_delivery_contract_sha256':sha(delivery),'dynamic_adjudication_schema_version':DYNAMIC_ADJUDICATION_SCHEMA_VERSION,'dynamic_adjudication_schema_contract_sha256':sha(dynamic),'candidate_binding_contract_version':CANDIDATE_BINDING_CONTRACT_VERSION,'candidate_binding_contract_sha256':sha(binding),'affirmative_stop_contract_version':AFFIRMATIVE_STOP_CONTRACT_VERSION,'affirmative_stop_contract_sha256':sha(affirmative),'schema_definition_lint_contract_version':SCHEMA_DEFINITION_LINT_VERSION,'schema_definition_lint_contract_sha256':sha(lint)}))"
if ($LASTEXITCODE -ne 0) {
    throw "Schema contract hash extraction failed with exit code $LASTEXITCODE"
}
$SchemaContracts = $SchemaContractJson | ConvertFrom-Json
$Receipt = [ordered]@{
    filename = [IO.Path]::GetFileName($Exe)
    bytes = (Get-Item -LiteralPath $Exe).Length
    sha256 = $Hash.Hash
    standalone_version = '0.6.4'
    skill_version = '0.2.1'
    presentation_transaction_version = 'mrc-presentation-transaction-1.0'
    presentation_source_contract_version = 'mrc-presentation-source-2.0'
    presentation_repair_contract_version = 'mrc-presentation-repair-2.0'
    language_contract_version = 'mrc-zh-display-language-1.0'
    interpretation_contract_version = 'mrc-public-interpretation-2.0'
    intake_contract_version = $Contracts.intake_contract_version
    intake_contract_sha256 = $Contracts.intake_contract_sha256
    title_evidence_contract_version = $Contracts.title_evidence_contract_version
    title_evidence_contract_sha256 = $Contracts.title_evidence_contract_sha256
    manuscript_basis_contract_version = $Contracts.manuscript_basis_contract_version
    manuscript_basis_contract_sha256 = $Contracts.manuscript_basis_contract_sha256
    provider_transmission_consent_contract_version = $Contracts.provider_transmission_consent_contract_version
    provider_transmission_consent_contract_sha256 = $Contracts.provider_transmission_consent_contract_sha256
    coverage_contract_version = $Contracts.coverage_contract_version
    adjudication_contract_version = $SchemaContracts.adjudication_contract_version
    contradiction_gate_version = $SchemaContracts.contradiction_gate_version
    machine_state_contract_version = $SchemaContracts.machine_state_contract_version
    machine_receipt_contract_version = 'mrc-machine-receipt-3.0'
    provider_request_transaction_version = $Contracts.provider_request_transaction_version
    provider_error_detail_contract_version = $Contracts.provider_error_detail_contract_version
    provider_error_detail_contract_sha256 = $Contracts.provider_error_detail_contract_sha256
    schema_delivery_contract_version = $SchemaContracts.schema_delivery_contract_version
    schema_delivery_contract_sha256 = $SchemaContracts.schema_delivery_contract_sha256
    dynamic_adjudication_schema_version = $SchemaContracts.dynamic_adjudication_schema_version
    dynamic_adjudication_schema_contract_sha256 = $SchemaContracts.dynamic_adjudication_schema_contract_sha256
    candidate_binding_contract_version = $SchemaContracts.candidate_binding_contract_version
    candidate_binding_contract_sha256 = $SchemaContracts.candidate_binding_contract_sha256
    affirmative_stop_contract_version = $SchemaContracts.affirmative_stop_contract_version
    affirmative_stop_contract_sha256 = $SchemaContracts.affirmative_stop_contract_sha256
    schema_definition_lint_contract_version = $SchemaContracts.schema_definition_lint_contract_version
    schema_definition_lint_contract_sha256 = $SchemaContracts.schema_definition_lint_contract_sha256
    technical_state_contract_version = 'mrc-technical-execution-state-1.0'
    technical_hold_receipt_contract_version = $Contracts.technical_hold_receipt_contract_version
    technical_hold_receipt_contract_sha256 = $Contracts.technical_hold_receipt_contract_sha256
    coverage_schema_sha256 = $Contracts.coverage_schema_sha256
    empty_candidate_adjudication_schema_sha256 = $Contracts.empty_candidate_adjudication_schema_sha256
    provider_capability_registry_sha256 = $Contracts.provider_capability_registry_sha256
    interpretation_agent_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $ProjectRoot 'standalone\AGENT.md')).Hash
    pyinstaller = '6.22.2'
    pypdf = '6.16.2'
    skill_donor_commit = 'fd30bf0daf0e8557b315491c72479b1b2598c22f'
    codex_reference_commit = 'd5caceccb1ee5bf94c081b995575ce4860e0912b'
}
$ReceiptJson = $Receipt | ConvertTo-Json
$Utf8NoBomEncoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $Release 'BUILD_RECEIPT.json'), $ReceiptJson, $Utf8NoBomEncoding)
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
