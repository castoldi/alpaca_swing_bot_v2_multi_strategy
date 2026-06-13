<#
.SYNOPSIS
    Inspect or bump the build version, and list the build/tag history.

.DESCRIPTION
    The canonical semantic version lives in ./VERSION. Every commit is auto-tagged
    by scripts/git-hooks/post-commit as  v<version>+build<N>-<datetime>.

.EXAMPLE
    pwsh scripts\version.ps1                 # show current version, build #, latest tag
    pwsh scripts\version.ps1 -Bump patch     # 0.1.0 -> 0.1.1 (+ CHANGELOG stub)
    pwsh scripts\version.ps1 -Bump minor     # 0.1.1 -> 0.2.0
    pwsh scripts\version.ps1 -Builds         # list all build tags, newest first
#>
[CmdletBinding()]
param(
    [ValidateSet('major', 'minor', 'patch')]
    [string]$Bump,
    [switch]$Builds
)

$ErrorActionPreference = 'Stop'
$Root        = Split-Path -Parent $PSScriptRoot
$VersionFile = Join-Path $Root 'VERSION'
$Changelog   = Join-Path $Root 'CHANGELOG.md'

function Get-Version {
    if (-not (Test-Path $VersionFile)) { return '0.0.0' }
    return (Get-Content $VersionFile -Raw).Trim()
}

function Get-BuildNumber {
    try { return [int](git -C $Root rev-list --count HEAD 2>$null) } catch { return 0 }
}

if ($Builds) {
    $tags = git -C $Root tag --list 'v*+build*' --sort=-creatordate
    if (-not $tags) { Write-Host "No build tags yet."; return }
    Write-Host "Build tags (newest first):" -ForegroundColor Cyan
    foreach ($t in $tags) {
        $subject = git -C $Root tag -l $t --format='%(contents:subject)'
        Write-Host ("  {0,-40} {1}" -f $t, $subject)
    }
    return
}

if ($Bump) {
    $cur = Get-Version
    if ($cur -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION '$cur' is not MAJOR.MINOR.PATCH" }
    $maj, $min, $pat = $cur.Split('.') | ForEach-Object { [int]$_ }
    switch ($Bump) {
        'major' { $maj++; $min = 0; $pat = 0 }
        'minor' { $min++; $pat = 0 }
        'patch' { $pat++ }
    }
    $new = "$maj.$min.$pat"
    Set-Content -Path $VersionFile -Value $new -Encoding utf8
    Write-Host "VERSION: $cur -> $new" -ForegroundColor Green

    # Drop a dated section stub into CHANGELOG just under [Unreleased].
    if (Test-Path $Changelog) {
        $today = (Get-Date).ToString('yyyy-MM-dd')
        $stub  = "## [$new] - $today`n`n### Added`n`n### Fixed`n`n### Changed`n"
        $text  = Get-Content $Changelog -Raw
        if ($text -match '(?ms)(## \[Unreleased\].*?\n)(?=## \[)') {
            $text = $text -replace '(?ms)(## \[Unreleased\].*?\n)(?=## \[)', "`$1`n$stub`n"
        } else {
            $text = $text.TrimEnd() + "`n`n$stub"
        }
        Set-Content -Path $Changelog -Value $text -Encoding utf8
        Write-Host "CHANGELOG.md: added [$new] section — fill it in, then commit." -ForegroundColor Green
    }
    Write-Host "Next commit will tag as: v$new+build$((Get-BuildNumber) + 1)-<datetime>"
    return
}

# default: show status
$ver   = Get-Version
$build = Get-BuildNumber
$latest = git -C $Root describe --tags --abbrev=0 --match 'v*+build*' 2>$null
Write-Host "=== Build version ===" -ForegroundColor Cyan
Write-Host "  VERSION file   : $ver"
Write-Host "  commit count   : $build  (current build number)"
Write-Host "  latest tag     : $(if ($latest) { $latest } else { '(none yet)' })"
Write-Host "  next commit tag: v$ver+build$($build + 1)-<datetime>"
