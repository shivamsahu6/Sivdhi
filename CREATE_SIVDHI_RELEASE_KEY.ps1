$ErrorActionPreference = 'Stop'

$Alias = 'sivdhi-release'
$KeyDirectory = Join-Path $env:USERPROFILE 'Documents\SivdhiSigning'
$KeyStore = Join-Path $KeyDirectory 'sivdhi-release.p12'

$keytoolCommand = Get-Command keytool.exe -ErrorAction SilentlyContinue
if (-not $keytoolCommand) {
    $keytoolCommand = Get-Command keytool -ErrorAction SilentlyContinue
}

if (-not $keytoolCommand -and $env:JAVA_HOME) {
    $candidate = Join-Path $env:JAVA_HOME 'bin\keytool.exe'
    if (Test-Path -LiteralPath $candidate) {
        $keytoolPath = $candidate
    }
}
elseif ($keytoolCommand) {
    $keytoolPath = $keytoolCommand.Source
}

if (-not $keytoolPath) {
    throw 'Java keytool was not found. Install a JDK (Temurin JDK 17 is suitable), set JAVA_HOME, reopen PowerShell, and run this script again.'
}

New-Item -ItemType Directory -Path $KeyDirectory -Force | Out-Null

if (Test-Path -LiteralPath $KeyStore) {
    throw "Keystore already exists at $KeyStore. This script will never overwrite an existing release key. Keep using the original key for every future update."
}

Write-Host 'Creating the permanent Sivdhi Android release key.' -ForegroundColor Cyan
Write-Host 'IMPORTANT: Use a strong password and store it safely.' -ForegroundColor Yellow
Write-Host 'When keytool asks for the private-key password, press ENTER to use the same password as the keystore (required for this PKCS12 setup).' -ForegroundColor Yellow
Write-Host 'Do not close this window until key generation and clipboard copy complete.' -ForegroundColor Yellow

& $keytoolPath -genkeypair -v `
    -keystore $KeyStore `
    -storetype PKCS12 `
    -alias $Alias `
    -keyalg RSA `
    -keysize 4096 `
    -validity 10000 `
    -dname 'CN=Sivdhi Apps, O=Sivdhi, C=IN'

if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $KeyStore)) {
    throw 'keytool did not create the keystore successfully. No GitHub secret was created.'
}

# Validate the keystore structure without printing any password or private-key data.
& $keytoolPath -list -keystore $KeyStore -storetype PKCS12 -alias $Alias
if ($LASTEXITCODE -ne 0) {
    throw 'Keystore validation failed. Keep the file private and inspect the keytool output.'
}

$base64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($KeyStore))
Set-Clipboard -Value $base64

Write-Host ''
Write-Host 'Release keystore created and validated.' -ForegroundColor Green
Write-Host "Private keystore file: $KeyStore" -ForegroundColor Green
Write-Host 'Base64 keystore content copied to clipboard. Paste it only into GitHub Actions secret SIVDHI_RELEASE_KEYSTORE_BASE64.' -ForegroundColor Green
Write-Host 'Create these additional GitHub Actions secrets manually:' -ForegroundColor Cyan
Write-Host '  SIVDHI_RELEASE_STORE_PASSWORD  = the keystore password you entered'
Write-Host "  SIVDHI_RELEASE_KEY_ALIAS       = $Alias"
Write-Host '  SIVDHI_RELEASE_KEY_PASSWORD    = the same password (PKCS12)'
Write-Host ''
Write-Host 'Never upload the .p12 file or its password to Git, email, chat, or this repository.' -ForegroundColor Red
