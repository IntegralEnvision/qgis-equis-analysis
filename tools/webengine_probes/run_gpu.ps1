# One GPU configuration, in a fresh QGIS: Chromium reads its flags once per process.
#
#   tools\webengine_probes\run_gpu.ps1 -Tag baseline
#   tools\webengine_probes\run_gpu.ps1 -Tag swcomp -Flags "--use-gl=angle --use-angle=d3d11 --disable-gpu-compositing" -Fps
#   # Through the profile's custom environment variables instead of the process's:
#   tools\webengine_probes\run_gpu.ps1 -Tag setup -QgisProfile gpuenv -WriteSetting "--use-gl=angle ..."
#   tools\webengine_probes\run_gpu.ps1 -Tag fromprofile -QgisProfile gpuenv
#   python tools\webengine_probes\summarise.py $env:TEMP\webengine_probes
#
# Run from PowerShell, not Git Bash: launched through Git Bash's `cmd /c`,
# every render process died with 0xC0000135 (STATUS_DLL_NOT_FOUND) on
# 2026-10-29 while the same launch from PowerShell worked.
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$Flags = "",
    [switch]$Fps,
    [string]$WriteSetting = "",
    [string]$QgisProfile = "gpuprobe",
    [string]$Out = (Join-Path $env:TEMP "webengine_probes"),
    [string]$Qgis = ""
)
$Repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
if (-not $Qgis) {
    $Qgis = Get-ChildItem "C:\Program Files\QGIS *\bin\qgis.bat" -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Directory.Parent.Name -replace "^QGIS ", "") } |
        Select-Object -Last 1 -ExpandProperty FullName
}
if (-not $Qgis) { throw "no QGIS found; pass -Qgis <path to qgis.bat>" }
New-Item -ItemType Directory -Force $Out | Out-Null
$Result = Join-Path $Out "gpu_$Tag.json"
Remove-Item $Result -ErrorAction SilentlyContinue

# Nothing from an earlier configuration may leak into this one.
foreach ($n in "QTWEBENGINE_CHROMIUM_FLAGS", "PROBE_FPS", "PROBE_WRITE_SETTING") {
    Remove-Item "env:$n" -ErrorAction SilentlyContinue
}
$env:PROBE_OUT = $Out
$env:PROBE_TAG = $Tag
if ($Flags) { $env:QTWEBENGINE_CHROMIUM_FLAGS = $Flags }
if ($Fps) { $env:PROBE_FPS = "1" }
if ($WriteSetting) { $env:PROBE_WRITE_SETTING = $WriteSetting }

# The probe finds its page relative to the working directory (--code gives it
# no __file__), so QGIS starts in the repository root.
Push-Location $Repo
try {
    & $Qgis --profiles-path (Join-Path $Out "profiles") --profile $QgisProfile `
        --nologo --noversioncheck --code "tools\webengine_probes\gpu_probe.py"
} finally {
    Pop-Location
}
for ($i = 0; $i -lt 60 -and -not (Test-Path $Result); $i++) { Start-Sleep -Seconds 5 }
Start-Sleep -Seconds 3
if (Test-Path $Result) { "${Tag}: $Result" } else { "${Tag}: NO RESULT after 5 minutes" }
