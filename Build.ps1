[CmdletBinding()]
param([string] $Package)

$ErrorActionPreference = 'Stop'
$output = Join-Path $PSScriptRoot 'dist'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    throw 'This build requires the Windows x64 .NET Framework C# compiler.'
}
$version = '1.0.3405.78'
$expectedHash = 'D035807B2AABA871E8C014759626F566E96934E6CE6F0587056EE81D5228C373'
if (-not $Package) {
    $Package = Join-Path $env:TEMP "CodexWorkbench.WebView2.$version.zip"
    if (-not (Test-Path -LiteralPath $Package)) {
        $url = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$version/microsoft.web.webview2.$version.nupkg"
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30 -OutFile $Package
    }
}
if ((Get-FileHash -LiteralPath $Package).Hash -ne $expectedHash) {
    throw 'The downloaded WebView2 package hash does not match the pinned version.'
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$null = New-Item -ItemType Directory -Force -Path $output
$archive = [IO.Compression.ZipFile]::OpenRead($Package)
try {
    $files = @{
        'lib/net462/Microsoft.Web.WebView2.Core.dll' = 'Microsoft.Web.WebView2.Core.dll'
        'lib/net462/Microsoft.Web.WebView2.WinForms.dll' = 'Microsoft.Web.WebView2.WinForms.dll'
        'runtimes/win-x64/native/WebView2Loader.dll' = 'WebView2Loader.dll'
        'LICENSE.txt' = 'WebView2-LICENSE.txt'
        'NOTICE.txt' = 'WebView2-NOTICE.txt'
    }
    foreach ($asset in $files.GetEnumerator()) {
        $entry = $archive.GetEntry($asset.Key)
        $target = Join-Path $output $asset.Value
        if (Test-Path -LiteralPath $target) {
            $stream = $entry.Open()
            try { $assetHash = (Get-FileHash -InputStream $stream).Hash }
            finally { $stream.Dispose() }
            if ((Get-FileHash -LiteralPath $target).Hash -eq $assetHash) { continue }
        }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
    }
}
finally {
    $archive.Dispose()
}

$program = Join-Path $output 'Relay Desk Preview.exe'
$arguments = @(
    '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
    "/out:$program", '/r:System.Drawing.dll', '/r:System.Windows.Forms.dll', '/r:System.Web.Extensions.dll',
    "/r:$(Join-Path $output 'Microsoft.Web.WebView2.Core.dll')",
    "/r:$(Join-Path $output 'Microsoft.Web.WebView2.WinForms.dll')",
    (Join-Path $PSScriptRoot 'Workbench.cs'), (Join-Path $PSScriptRoot 'HubBridge.cs')
)
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw "Windows Workbench compilation failed ($LASTEXITCODE)." }
foreach ($asset in @('hub_control.py', 'web/index.html', 'web/style.css', 'web/app.js', 'web/relay-logo.png',
        'account_hub/account_service.py', 'account_hub/codex_rpc.py', 'account_hub/profiles.py', 'account_hub/quota.py')) {
    $target = Join-Path $output $asset
    $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target)
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $asset) -Destination $target -Force
}
Get-Item -LiteralPath $program
