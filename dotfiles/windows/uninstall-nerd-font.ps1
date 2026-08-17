<#
.SYNOPSIS
    Undoes install-nerd-font.ps1: removes the per-user MesloLGS Nerd Font registration
    and the font files it copied.

.DESCRIPTION
    Removes exactly what the installer created, and nothing else:

      the four HKCU font registry values that name the vendored faces, and only
      when the value still points into the per-user font directory, and
      the four .ttf files under $env:LOCALAPPDATA\Microsoft\Windows\Fonts.

    A machine-wide install of the same family (C:\Windows\Fonts, HKLM) is left alone --
    this script never had the rights to create one and does not assume ownership of it.

    Terminal settings are not touched. If a profile still names the font, Windows falls
    back to a default face; edit the profile yourself if you want the old font back.

.PARAMETER WhatIf
    Report every removal that would happen, and change nothing.

.EXAMPLE
    .\uninstall-nerd-font.ps1
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'

$isWindowsHost = if ($null -ne $PSVersionTable.Platform) { $PSVersionTable.Platform -eq 'Win32NT' } else { $true }
if (-not $isWindowsHost) {
    Write-Host 'This uninstaller is for Windows clients. On Linux/macOS use dotfiles/uninstall.d/30-fonts.sh instead.'
    exit 0
}

$FontFamily = 'MesloLGS Nerd Font'
$Fonts = @(
    @{ File = 'MesloLGSNerdFont-Regular.ttf';    RegName = "$FontFamily (TrueType)" }
    @{ File = 'MesloLGSNerdFont-Bold.ttf';       RegName = "$FontFamily Bold (TrueType)" }
    @{ File = 'MesloLGSNerdFont-Italic.ttf';     RegName = "$FontFamily Italic (TrueType)" }
    @{ File = 'MesloLGSNerdFont-BoldItalic.ttf'; RegName = "$FontFamily Bold Italic (TrueType)" }
)

$FontDir = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Fonts'
$RegKey  = 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts'

Write-Host "==> Removing $FontFamily for $env:USERNAME"
Write-Host ''

$removedFiles = 0
$removedKeys = 0

foreach ($font in $Fonts) {
    $dest = Join-Path $FontDir $font.File

    # Deregister before deleting, so nothing is left pointing at a missing file if
    # the delete fails because the font is loaded.
    if (Test-Path -LiteralPath $RegKey) {
        # GetValue rather than Get-ItemProperty -Name: a missing value is the normal
        # case here, and Get-ItemProperty records an error for it even when silenced.
        $current = (Get-Item -LiteralPath $RegKey).GetValue($font.RegName)
        if ($null -eq $current) {
            Write-Host "SKIP  $($font.RegName) not registered"
        }
        elseif ($current -ne $dest) {
            Write-Host "SKIP  $($font.RegName) points at $current, not the per-user copy -- leaving it"
        }
        else {
            if ($PSCmdlet.ShouldProcess("$RegKey\$($font.RegName)", 'Remove registry value')) {
                Remove-ItemProperty -LiteralPath $RegKey -Name $font.RegName -Force
            }
            Write-Host "UNREG $($font.RegName)"
            $removedKeys++
        }
    }

    if (Test-Path -LiteralPath $dest) {
        if ($PSCmdlet.ShouldProcess($dest, 'Remove font file')) {
            try {
                Remove-Item -LiteralPath $dest -Force
            }
            catch {
                # A loaded font file stays locked until the application releases it.
                Write-Host "KEEP  $dest is in use -- close every terminal using it and re-run"
                continue
            }
        }
        Write-Host "RM    $($font.File)"
        $removedFiles++
    }
}

Write-Host ''
Write-Host "==> Done. $removedFiles file(s) removed, $removedKeys registry value(s) removed."
Write-Host 'Applications already running keep the font loaded until they are restarted.'
