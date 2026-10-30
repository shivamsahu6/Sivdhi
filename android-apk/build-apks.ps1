$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Gradle = Get-Command gradle -ErrorAction SilentlyContinue
if (-not $Gradle) {
    throw "Gradle command not found. Use the GitHub Actions workflow in README_HINDI_EN.md, or install Android Studio/Gradle first."
}
Push-Location $Project
try {
    gradle --no-daemon :app:assembleCustomerDebug :app:assembleWorkpartnerDebug
    $out = Join-Path $Project "dist"
    New-Item -ItemType Directory -Path $out -Force | Out-Null
    Copy-Item "app\build\outputs\apk\customer\debug\app-customer-debug.apk" "$out\Sivdhi-Customer.apk" -Force
    Copy-Item "app\build\outputs\apk\workpartner\debug\app-workpartner-debug.apk" "$out\Sivdhi-Worker-Partner.apk" -Force
    Get-FileHash "$out\*.apk" -Algorithm SHA256
} finally { Pop-Location }
