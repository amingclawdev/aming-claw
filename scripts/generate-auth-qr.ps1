param(
    [string]$OutputPath = ".\shared-volume\codex-tasks\state\auth-otp-qr.png"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$statePath = ".\shared-volume\codex-tasks\state\auth_totp.json"
if (-not (Test-Path $statePath)) {
    throw "auth state not found: $statePath. Run /auth_init first."
}

$state = Get-Content $statePath -Raw | ConvertFrom-Json
$secret = [string]$state.secret_b32
$issuer = [string]$state.issuer
$account = [string]$state.account_name
$period = [int]$state.period_sec
$digits = [int]$state.digits

if (-not $secret) {
    throw "secret_b32 missing in $statePath"
}
if (-not $issuer) { $issuer = "aming-claw" }
if (-not $account) { $account = "telegram-ops" }
if ($period -le 0) { $period = 60 }
if ($digits -le 0) { $digits = 6 }

$label = [uri]::EscapeDataString("$issuer`:$account")
$issuerEsc = [uri]::EscapeDataString($issuer)
$secretEsc = [uri]::EscapeDataString($secret)
$uri = "otpauth://totp/${label}?secret=${secretEsc}&issuer=${issuerEsc}&period=${period}&digits=${digits}"

$out = Resolve-Path (Split-Path -Parent $OutputPath) -ErrorAction SilentlyContinue
if (-not $out) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
}
$fullOut = (Resolve-Path $OutputPath -ErrorAction SilentlyContinue)
if (-not $fullOut) {
    $fullOut = Join-Path (Get-Location).Path $OutputPath.TrimStart(".\")
}

$tmpPy = Join-Path ([System.IO.Path]::GetTempPath()) ("gen_auth_qr_" + [guid]::NewGuid().ToString("N") + ".py")
$py = @'
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--uri", required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args()

try:
    import qrcode
except Exception:
    raise SystemExit("missing python package: qrcode[pil]. install with: python -m pip install qrcode[pil]")

out = Path(args.out)
img = qrcode.make(args.uri)
out.parent.mkdir(parents=True, exist_ok=True)
img.save(out)
print(str(out))
'@

Set-Content -Path $tmpPy -Value $py -Encoding UTF8
try {
    & python $tmpPy --uri "$uri" --out "$fullOut"
}
finally {
    Remove-Item -Path $tmpPy -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "otpauth_uri:"
Write-Host $uri
Write-Host ""
Write-Host "QR saved to:"
Write-Host $fullOut
