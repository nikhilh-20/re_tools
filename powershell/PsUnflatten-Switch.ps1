param(
    [Parameter(Mandatory)][string]$InputFile,
    [Parameter(Mandatory)][string]$OutputFile,
    [int]$MaxSteps = 5000
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_PsDeobLib.ps1"
try {
    $stats = Invoke-PsUnflattenSwitch `
        -InputPath  $InputFile `
        -OutputPath $OutputFile `
        -MaxSteps   $MaxSteps
    $stats | ConvertTo-Json -Compress -Depth 5
} catch {
    "ERROR: $($_.Exception.Message)" | Out-File -FilePath $OutputFile -Encoding UTF8 -NoNewline
}
