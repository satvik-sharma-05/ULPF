<#
.SYNOPSIS
    Builds every image this project needs and saves them to images\ for
    transfer to the air-gapped Linux VM.

.DESCRIPTION
    Run on a machine WITH INTERNET and Docker Desktop. Images are the delivery
    format, which is why this package ships no Python wheelhouse and no
    node_modules: a Docker image is a Linux image whatever host built it, and
    everything the services need - CPU-only torch, bge-m3, the reranker,
    Whisper, the Piper voice, the compiled React bundle - is baked in at build
    time. On the target VM there is nothing to install, only `docker load` and
    `docker compose up`.

    One caveat: images are architecture-specific, not OS-specific. These build
    as linux/amd64, which matches an x86_64 VM. On an ARM host you would need
    --platform linux/amd64 or the images will not run on the target.

    Ollama is PULLED, not built, and its weights are mounted from
    backend/ollama/models - see the note in docker-compose.yml.

.EXAMPLE
    .\build_images.ps1
    .\build_images.ps1 -SkipSave       # build only, no tars
    .\build_images.ps1 -KeepImages     # keep images in Docker after export
#>

param(
    [switch]$SkipSave,
    # Keep the built images in Docker after exporting them. Off by default -
    # see the note above the save loop.
    [switch]$KeepImages,
    [string]$HfToken = ""
)

$ErrorActionPreference = 'Stop'

# Native commands need their own error handling. Windows PowerShell 5.1 wraps
# every stderr line from an external executable in a NativeCommandError, and
# under $ErrorActionPreference='Stop' that is TERMINATING - so this script used
# to abort on docker's ordinary build progress, which docker writes to stderr
# by design, and report "build failed" for builds that had actually succeeded.
#
# $LASTEXITCODE is the only reliable success signal for an exe, and the checks
# below already used it. This just stops the preference from firing first.
function Invoke-Native {
    param([Parameter(Mandatory = $true)][string]$What,
          [Parameter(Mandatory = $true)][string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker @Arguments
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "FAILED: $What (docker exited $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}
$root = $PSScriptRoot
$images = Join-Path $root 'images'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker not found on PATH. Install Docker Desktop and start it."
    exit 1
}

New-Item -ItemType Directory -Force -Path $images | Out-Null

$targets = @(
    @{ Name = 'ulpf-ingestion:latest'; Context = 'backend\ingestion_pipeline'; Tar = 'ingestion.tar' },
    @{ Name = 'ulpf-chatbot:latest';   Context = 'backend\chatbot_pipeline';   Tar = 'chatbot.tar' },
    @{ Name = 'ulpf-analytics:latest'; Context = 'backend\analytics_pipeline'; Tar = 'analytics.tar' },
    @{ Name = 'ulpf-frontend:latest';  Context = 'frontend';                   Tar = 'frontend.tar' }
)

foreach ($t in $targets) {
    $ctx = Join-Path $root $t.Context
    Write-Host ""
    Write-Host "=== build $($t.Name) from $($t.Context) ===" -ForegroundColor Cyan
    # Drop any existing tag first. With Docker's containerd image store the
    # exporter can finish writing the image and THEN fail with
    # `image "...": already exists`, aborting the run after a build that
    # actually succeeded - which is how this script came to report failure
    # while every image on disk was current. Untagging first removes the
    # collision; the layers are cached either way, so it costs nothing.
    $prevEA = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & docker rmi -f $t.Name 2>$null | Out-Null } finally { $ErrorActionPreference = $prevEA }

    $buildArgs = @('build', '--platform', 'linux/amd64', '-t', $t.Name, $ctx)
    if ($HfToken -ne "") { $buildArgs += @('--build-arg', "HF_TOKEN=$HfToken") }
    Invoke-Native -What "build $($t.Name)" -Arguments $buildArgs
}

Write-Host ""
Write-Host "=== pull base images ===" -ForegroundColor Cyan
Invoke-Native -What "pull neo4j" -Arguments @('pull', '--platform', 'linux/amd64', 'neo4j:5-community')
Invoke-Native -What "pull ollama" -Arguments @('pull', '--platform', 'linux/amd64', 'ollama/ollama:latest')

if ($SkipSave) {
    Write-Host ""
    Write-Host "Built. -SkipSave set, so no tars were written." -ForegroundColor Yellow
    exit 0
}

# Save, then DROP the image. The tars and the images together are larger than
# the disk they are written to, so keeping both runs out part way through -
# and a build that fails on the last export has wasted the whole run. Dropping
# each image as its tar lands keeps the total roughly flat.
foreach ($t in $targets) {
    $out = Join-Path $images $t.Tar
    Write-Host ""
    Write-Host "=== save $($t.Name) -> images\$($t.Tar) ===" -ForegroundColor Cyan
    Invoke-Native -What 'docker save' -Arguments @('save', '-o', $out, $t.Name)
    if (-not $KeepImages) {
        # rmi is the one call whose failure is not fatal - the tar is already
        # written, and a still-tagged image only costs disk.
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & docker rmi $t.Name 2>$null | Out-Null } finally { $ErrorActionPreference = $prev }
    }
}
Invoke-Native -What 'save neo4j' -Arguments @('save', '-o', (Join-Path $images 'neo4j.tar'), 'neo4j:5-community')
Invoke-Native -What 'save ollama' -Arguments @('save', '-o', (Join-Path $images 'ollama.tar'), 'ollama/ollama:latest')

Write-Host ""
Write-Host "=== done ===" -ForegroundColor Green
Get-ChildItem $images -Filter *.tar |
    ForEach-Object { "{0,-16} {1,8:N0} MB" -f $_.Name, ($_.Length / 1MB) }
Write-Host ""
Write-Host "Copy the whole folder to the air-gapped VM, then run" -ForegroundColor Green
Write-Host "  ./load_images.sh && docker compose up -d" -ForegroundColor Green
