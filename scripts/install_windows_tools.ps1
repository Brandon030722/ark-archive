# Keep runtime downloads pinned to upstream files.
function Install-ArkWindowsTools([string] $Destination) {
    $uv = Join-Path $Destination 'uv.exe'
    $postgres = Join-Path $Destination 'pgsql'
    if ((Test-Path -LiteralPath $uv) -and (Test-Path -LiteralPath "$postgres\bin\pg_ctl.exe")) {
        return
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $scratch = Join-Path $Destination ([Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $scratch | Out-Null
    function Get-PinnedFile($address, $digest, $file) {
        Invoke-WebRequest -UseBasicParsing -Uri $address -OutFile $file
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $digest) {
            throw "SHA-256 mismatch for $address"
        }
    }
    try {
        if (!(Test-Path -LiteralPath $uv)) {
            Write-Host 'Preparing uv...'
            $zip = Join-Path $scratch 'uv.zip'
            Get-PinnedFile 'https://github.com/astral-sh/uv/releases/download/0.12.10/uv-x86_64-pc-windows-msvc.zip' 'f65744f94072152b1f86ba2aace4d01f1124d9a8ecb235805039e3718c36cac2' $zip
            & tar.exe -xf $zip -C $scratch
            if ($LASTEXITCODE) { throw 'Could not extract uv.' }
            Get-PinnedFile 'https://raw.githubusercontent.com/astral-sh/uv/0.12.10/LICENSE-MIT' '860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c' "$Destination\uv-LICENSE-MIT.txt"
            Get-PinnedFile 'https://raw.githubusercontent.com/astral-sh/uv/0.12.10/LICENSE-APACHE' 'c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4' "$Destination\uv-LICENSE-APACHE.txt"
            Move-Item -LiteralPath "$scratch\uv.exe" -Destination $uv
        }
        if (!(Test-Path -LiteralPath "$postgres\bin\pg_ctl.exe")) {
            Write-Host 'Preparing PostgreSQL 16.16...'
            $zip = Join-Path $scratch 'postgres.zip'
            Get-PinnedFile 'https://sbp.enterprisedb.com/getfile.jsp?fileid=1260670' '5c0d06f3d26a929ecb36dd931409f1e51a4181355794a259b90aae8bffc57a9f' $zip
            & tar.exe -xf $zip -C $scratch pgsql/bin pgsql/lib pgsql/share pgsql/server_license.txt pgsql/commandlinetools_3rd_party_licenses.txt
            if ($LASTEXITCODE) { throw 'Could not extract PostgreSQL.' }
            Move-Item -LiteralPath "$scratch\pgsql" -Destination $postgres
        }
    } finally {
        Remove-Item -LiteralPath $scratch -Force -Recurse
    }
}
