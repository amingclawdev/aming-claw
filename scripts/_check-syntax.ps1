$content = [System.IO.File]::ReadAllText('C:\Users\z5866\Documents\amingclaw\aming_claw\agent\backends.py')
$lines = $content -split "`n"
# Show lines 441-449 with byte values for problematic chars
for ($i = 440; $i -le 448; $i++) {
    $line = $lines[$i]
    $hasAsciiQuote = $false
    foreach ($c in $line.ToCharArray()) {
        if ([int]$c -eq 0x22) { $hasAsciiQuote = $true }
    }
    $marker = if ($hasAsciiQuote) { " <-- ASCII quote found" } else { "" }
    Write-Host "L$($i+1):$marker $line"
}
