$ErrorActionPreference = "Stop"

$ProjectId = "aming-claw"
$BaseUrl = "http://localhost:40000"
$PollSeconds = 15
$Root = "C:\Users\z5866\Documents\amingclaw\aming_claw"
$LogDir = Join-Path $Root "docs\dev\observer\logs"
$LogPath = Join-Path $LogDir "observer-docs-loop.log"
$StatePath = Join-Path $LogDir "observer-docs-loop-state.json"

$KnownTaskIds = @(
    "task-1775055137-41ee2f",
    "task-1775055139-a30793",
    "task-1775055488-ca6257",
    "task-1775055857-50d9b0",
    "task-1775056070-e3ddfe",
    "task-1775056164-749560",
    "task-1775056308-231772",
    "task-1775056421-3c4c31",
    "task-1775062649-6c437d",
    "task-1775062671-c986b3",
    "task-1775062807-37e337",
    "task-1775063570-afa691"
)

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogPath -Value "[$timestamp] $Message"
}

function Get-Json {
    param([string]$Url)
    Invoke-RestMethod -Uri $Url -Method GET -TimeoutSec 10
}

function Post-Json {
    param([string]$Url, [hashtable]$Body)
    $json = $Body | ConvertTo-Json -Compress
    Invoke-RestMethod -Uri $Url -Method POST -ContentType "application/json" -Body $json -TimeoutSec 10
}

function Get-TasksByStatus {
    param([string]$Status)
    $url = "$BaseUrl/api/task/$ProjectId/list?status=$Status&limit=100"
    $resp = Get-Json -Url $url
    if ($null -eq $resp.tasks) { return @() }
    return @($resp.tasks)
}

function Is-RelevantTask {
    param($Task)
    if ($KnownTaskIds -contains $Task.task_id) { return $true }
    $prompt = [string]$Task.prompt
    if ($prompt -match "docs architecture migration") { return $true }
    if ($prompt -match "waived_status_gate_semantics_repair") { return $true }
    if ($prompt -match "Run tests for task-1775062671-c986b3") { return $true }
    if ($prompt -match "max turns") { return $true }
    return $false
}

function Is-AutoReleaseCandidate {
    param($Task)
    if (-not (Is-RelevantTask -Task $Task)) { return $false }
    if ($Task.type -eq "gatekeeper") { return $false }
    return @("pm", "dev", "test", "qa") -contains [string]$Task.type
}

function Get-StateSnapshot {
    $claimed = Get-TasksByStatus -Status "claimed"
    $queued = Get-TasksByStatus -Status "queued"
    $hold = Get-TasksByStatus -Status "observer_hold"
    $health = Get-Json -Url "$BaseUrl/api/health"
    $version = Get-Json -Url "$BaseUrl/api/version-check/$ProjectId"

    $allRelevant = @($claimed + $queued + $hold | Where-Object { Is-RelevantTask -Task $_ })
    $summary = @()
    foreach ($task in $allRelevant) {
        $summary += [pscustomobject]@{
            task_id = [string]$task.task_id
            type = [string]$task.type
            status = [string]$task.status
        }
    }

    return [pscustomobject]@{
        health = [string]$health.status
        version_ok = [bool]$version.ok
        relevant = $summary
    }
}

Write-Log "observer loop started for $ProjectId against $BaseUrl"

while ($true) {
    try {
        $hold = Get-TasksByStatus -Status "observer_hold"
        foreach ($task in $hold) {
            if (Is-AutoReleaseCandidate -Task $task) {
                $releaseResp = Post-Json -Url "$BaseUrl/api/task/$ProjectId/release" -Body @{ task_id = [string]$task.task_id }
                Write-Log ("released observer_hold task {0} ({1}) -> {2}" -f $task.task_id, $task.type, $releaseResp.status)
            }
        }

        $snapshot = Get-StateSnapshot
        $snapshotJson = $snapshot | ConvertTo-Json -Depth 6 -Compress

        $previousJson = ""
        if (Test-Path $StatePath) {
            $previousJson = Get-Content -Path $StatePath -Raw
        }

        if ($snapshotJson -ne $previousJson) {
            Set-Content -Path $StatePath -Value $snapshotJson
            Write-Log ("state changed: {0}" -f $snapshotJson)
        }
    }
    catch {
        Write-Log ("loop error: {0}" -f $_.Exception.Message)
    }

    Start-Sleep -Seconds $PollSeconds
}
