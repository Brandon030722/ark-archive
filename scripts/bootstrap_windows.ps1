param(
    [ValidateRange(1,65535)][int] $Port = 8000,
    [switch] $NoBrowser,
    [switch] $Stop
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

$local = Join-Path (Get-Location).Path '.local'
$stopFile = Join-Path $local 'windows.stop'
$lockFile = Join-Path $local 'windows.lock'
if ($Stop) {
    if (Test-Path -LiteralPath $lockFile) {
        [IO.File]::WriteAllText($stopFile, '')
    }
    return
}

if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'This installer requires the x64 edition of Windows.'
}

New-Item -ItemType Directory -Path $local -Force | Out-Null
if ((Get-Item -LiteralPath $local -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'The .local folder must be an ordinary folder on the local drive.'
}
try {
    $lock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None')
} catch [IO.IOException] {
    throw "Ark Archive is already starting or running. Open http://127.0.0.1:$Port/ in your browser."
}

try {
    # Restrict inherited access to the local database and its password. DPAPI
    # additionally binds the workbench master key to the signed-in Windows user.
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($owner)
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $owner, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    Set-Acl -LiteralPath $local -AclObject $acl
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue

    $tools = Join-Path (Get-Location).Path 'vendor\windows'
    if (!(Test-Path -LiteralPath "$tools\uv.exe") -or !(Test-Path -LiteralPath "$tools\pgsql\bin\pg_ctl.exe")) {
        $tools = Join-Path $local 'tools'
        . "$PSScriptRoot\install_windows_tools.ps1"
        Install-ArkWindowsTools $tools
    }
    $env:ARK_WINDOWS_POSTGRES = "$tools\pgsql"
    $env:ARK_CREDENTIAL_MASTER_KEY_PATH = "$local\credentials\master.key"
    $env:UV_PYTHON_INSTALL_DIR = "$local\python"
    $env:UV_CACHE_DIR = "$local\uv-cache"
    $env:UV_MANAGED_PYTHON = '1'
    $env:PYTHONUTF8 = '1'

    $stamp = Join-Path $local 'uv-lock-hash.txt'
    $hash = (Get-FileHash -LiteralPath 'uv.lock' -Algorithm SHA256).Hash
    if (!(Test-Path -LiteralPath '.venv\Scripts\python.exe') -or
        !(Test-Path -LiteralPath $stamp) -or (Get-Content -LiteralPath $stamp -Raw).Trim() -ne $hash) {
        Write-Host 'Installing Python and dependencies...'
        & "$tools\uv.exe" sync --frozen --no-dev --python 3.12
        if ($LASTEXITCODE) { throw 'Python setup failed. Check internet access and retry.' }
        Set-Content -LiteralPath $stamp -Value $hash -Encoding Ascii
    }
    $launch = @('-m', 'scripts.launch_windows', '--port', $Port)
    if ($NoBrowser) { $launch += '--no-browser' }
    & '.venv\Scripts\python.exe' @launch
    if ($LASTEXITCODE) { throw 'Ark Archive could not start. See the error above and .local\postgres\server.log.' }
} finally {
    $lock.Dispose()
}
