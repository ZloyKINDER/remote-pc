$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $root "ffmpeg.exe"
if (Test-Path $destination) {
    Write-Host "ffmpeg.exe уже есть: $destination"
    exit 0
}

$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("remotepc-ffmpeg-" + [System.Guid]::NewGuid().ToString("N"))
$zip = Join-Path $temp "ffmpeg.zip"

try {
    New-Item -ItemType Directory -Force -Path $temp | Out-Null
    Write-Host "Скачиваю FFmpeg essentials..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $temp -Force
    $exe = Get-ChildItem -Path $temp -Recurse -Filter "ffmpeg.exe" |
        Select-Object -First 1
    if (-not $exe) {
        throw "ffmpeg.exe не найден в архиве"
    }
    Copy-Item -Path $exe.FullName -Destination $destination -Force
    Write-Host "Готово: $destination"
}
finally {
    if (Test-Path $temp) {
        Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
    }
}
