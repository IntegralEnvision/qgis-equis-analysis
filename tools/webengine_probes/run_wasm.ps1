# The WebAssembly probe: build the page, run it in a fresh QGIS, wait for results.json.
#
#   tools\webengine_probes\run_wasm.ps1 -Parquet "data\<facility>\matrix=WS\fraction=N\part-0000.parquet"
#   python tools\webengine_probes\summarise.py $env:TEMP\webengine_probes\wasm
#   node tools\webengine_probes\node_kriging.mjs $env:TEMP\webengine_probes\wasm\page\points.json
#
# Needs `npm ci` in web\ (esbuild and @duckdb/duckdb-wasm come from there)
# and, for the kriging stages, a wasm-pack --target web build of kriging-rs
# (-KrigingPkg; default ..\kriging-rs\npm\kriging-rs-wasm\pkg). Run from
# PowerShell, not Git Bash; see run_gpu.ps1.
param(
    [Parameter(Mandatory = $true)][string]$Parquet,
    [string]$KrigingPkg = "",
    [string]$Out = (Join-Path $env:TEMP "webengine_probes\wasm"),
    [string]$Qgis = ""
)
$Repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
if (-not $Qgis) {
    $Qgis = Get-ChildItem "C:\Program Files\QGIS *\bin\qgis.bat" -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Directory.Parent.Name -replace "^QGIS ", "") } |
        Select-Object -Last 1 -ExpandProperty FullName
}
if (-not $Qgis) { throw "no QGIS found; pass -Qgis <path to qgis.bat>" }
$Page = Join-Path $Out "page"
New-Item -ItemType Directory -Force $Page | Out-Null
Remove-Item (Join-Path $Out "results.json") -ErrorAction SilentlyContinue

node (Join-Path $PSScriptRoot "build_wasm_page.mjs") $Page
if ($LASTEXITCODE -ne 0) { throw "page build failed" }

$env:PROBE_OUT = $Out
$env:PROBE_PARQUET = (Resolve-Path $Parquet).Path
if ($KrigingPkg) { $env:PROBE_KRIGING_PKG = (Resolve-Path $KrigingPkg).Path }
Push-Location $Repo
try {
    & $Qgis --profiles-path (Join-Path $Out "profiles") --profile wasmprobe `
        --nologo --noversioncheck --code "tools\webengine_probes\wasm_probe.py"
} finally {
    Pop-Location
}
# Three page loads, DuckDB-WASM and a 512 x 512 kriging grid: minutes, not seconds.
$Result = Join-Path $Out "results.json"
for ($i = 0; $i -lt 180 -and -not (Test-Path $Result); $i++) { Start-Sleep -Seconds 5 }
Start-Sleep -Seconds 2
if (Test-Path $Result) { "wrote $Result" } else { "NO RESULT after 15 minutes; see $(Join-Path $Out 'log.txt')" }
