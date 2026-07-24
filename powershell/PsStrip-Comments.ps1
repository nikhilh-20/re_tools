param(
    [Parameter(Mandatory)][string]$InputFile,
    [Parameter(Mandatory)][string]$OutputFile,
    [switch]$IncludeTrailing,
    [string]$KeepPattern
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_PsDeobLib.ps1"
try {
    $stats = Invoke-PsStripComments `
        -InputPath       $InputFile `
        -OutputPath      $OutputFile `
        -IncludeTrailing $IncludeTrailing.IsPresent `
        -KeepPattern     $KeepPattern
    $stats | ConvertTo-Json -Compress
} catch {
    "ERROR: $($_.Exception.Message)" | Out-File -FilePath $OutputFile -Encoding UTF8 -NoNewline
}
