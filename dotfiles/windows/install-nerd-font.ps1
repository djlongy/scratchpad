<#
.SYNOPSIS
    Installs the vendored MesloLGS Nerd Font for the current Windows user. Offline.

.DESCRIPTION
    The powerline prompt and the tmux status line in this repository draw glyphs from
    a Nerd Font. Those glyphs are rendered by the TERMINAL, which runs on this Windows
    machine -- not by the remote host you SSH into. Installing the font on the remote
    host changes nothing on screen; installing it here is what makes the prompt legible.

    Everything this script needs ships beside it, in ..\vendor\fonts\meslolgs-nf. It
    makes no network call, needs no administrator rights, and installs per-user:

      files    -> $env:LOCALAPPDATA\Microsoft\Windows\Fonts
      registry -> HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts

    That is the per-user font convention Windows 10 1809+ and Windows 11 use, and the
    reason the registry values hold a full path rather than a bare filename.

    Re-running is safe: a file whose contents already match is left alone, a changed
    file is replaced in place, and registry values are rewritten only when they differ.
    Nothing is ever duplicated.

    It does NOT edit any application's settings. It prints the Windows Terminal snippet
    for you to paste, because settings.json is yours and may carry hand-written profiles.

.PARAMETER WhatIf
    Report every copy and registry write that would happen, and change nothing.

.EXAMPLE
    .\install-nerd-font.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-nerd-font.ps1

.NOTES
    Uninstall with .\uninstall-nerd-font.ps1 in this directory.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Platform guard. This script writes to HKCU and a Windows-only font directory,
# so on PowerShell 7 for Linux/macOS it must decline rather than half-run.
# ---------------------------------------------------------------------------
$isWindowsHost = if ($null -ne $PSVersionTable.Platform) { $PSVersionTable.Platform -eq 'Win32NT' } else { $true }
if (-not $isWindowsHost) {
    Write-Host 'This installer is for Windows clients. On Linux/macOS use dotfiles/install.d/30-fonts.sh instead.'
    exit 0
}

# ---------------------------------------------------------------------------
# The four vendored faces, and the name each is registered under. Windows names
# the Regular face after the family alone and suffixes the others with the style;
# "(TrueType)" is the required type marker for a .ttf.
# ---------------------------------------------------------------------------
$FontFamily = 'MesloLGS Nerd Font'
$Fonts = @(
    @{ File = 'MesloLGSNerdFont-Regular.ttf';    RegName = "$FontFamily (TrueType)" }
    @{ File = 'MesloLGSNerdFont-Bold.ttf';       RegName = "$FontFamily Bold (TrueType)" }
    @{ File = 'MesloLGSNerdFont-Italic.ttf';     RegName = "$FontFamily Italic (TrueType)" }
    @{ File = 'MesloLGSNerdFont-BoldItalic.ttf'; RegName = "$FontFamily Bold Italic (TrueType)" }
)

$VendorDir = Join-Path $PSScriptRoot '..\vendor\fonts\meslolgs-nf'
if (-not (Test-Path -LiteralPath $VendorDir)) {
    throw "Vendored fonts not found at $VendorDir -- keep this script inside the dotfiles package (it resolves the payload relative to itself)."
}
$VendorDir = (Resolve-Path -LiteralPath $VendorDir).Path

$FontDir = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Fonts'
$RegKey  = 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts'

Write-Host "==> Installing $FontFamily for $env:USERNAME (per-user, no admin, offline)"
Write-Host "    source: $VendorDir"
Write-Host "    files:  $FontDir"
Write-Host "    keys:   $RegKey"
Write-Host ''

# A fresh profile has neither of these yet.
if (-not (Test-Path -LiteralPath $FontDir)) {
    if ($PSCmdlet.ShouldProcess($FontDir, 'Create font directory')) {
        New-Item -ItemType Directory -Path $FontDir -Force | Out-Null
    }
}
if (-not (Test-Path -LiteralPath $RegKey)) {
    if ($PSCmdlet.ShouldProcess($RegKey, 'Create registry key')) {
        New-Item -Path $RegKey -Force | Out-Null
    }
}

$installedPaths = @()
$copied = 0
$registered = 0

foreach ($font in $Fonts) {
    $src = Join-Path $VendorDir $font.File
    if (-not (Test-Path -LiteralPath $src)) {
        throw "Missing vendored font file: $src"
    }
    $dest = Join-Path $FontDir $font.File
    $installedPaths += $dest

    # Compare by content, so a re-run after a font version bump replaces the file
    # while an unchanged re-run touches nothing.
    $needsCopy = $true
    if (Test-Path -LiteralPath $dest) {
        $srcHash  = (Get-FileHash -LiteralPath $src  -Algorithm SHA256).Hash
        $destHash = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
        $needsCopy = ($srcHash -ne $destHash)
    }

    if ($needsCopy) {
        if ($PSCmdlet.ShouldProcess($dest, 'Copy font file')) {
            try {
                Copy-Item -LiteralPath $src -Destination $dest -Force
            }
            catch {
                # A font file already loaded by a running application is locked.
                throw "Could not write $dest -- close every terminal and application using $FontFamily, then re-run. ($($_.Exception.Message))"
            }
        }
        Write-Host "COPY  $($font.File)"
        $copied++
    }
    else {
        Write-Host "SAME  $($font.File) (already current)"
    }

    # The value data is the full path: per-user fonts are not in C:\Windows\Fonts,
    # so a bare filename would not resolve.
    #
    # Read through the RegistryKey object rather than Get-ItemProperty -Name: on a
    # value that does not exist yet, Get-ItemProperty still records an error even
    # with -ErrorAction SilentlyContinue, so a first run would leave four entries
    # in the caller's $Error. GetValue simply returns $null.
    $current = (Get-Item -LiteralPath $RegKey).GetValue($font.RegName)
    if ($current -ne $dest) {
        if ($PSCmdlet.ShouldProcess("$RegKey\$($font.RegName)", 'Set registry value')) {
            New-ItemProperty -LiteralPath $RegKey -Name $font.RegName -Value $dest -PropertyType String -Force | Out-Null
        }
        Write-Host "REG   $($font.RegName)"
        $registered++
    }
    else {
        Write-Host "SAME  $($font.RegName) (already registered)"
    }
}

# ---------------------------------------------------------------------------
# Load the faces into the running session and tell open applications to re-read
# their font list, so the font appears without a sign-out. Best effort only:
# the registry entries above are what make the install survive a reboot.
# ---------------------------------------------------------------------------
if (-not $WhatIfPreference) {
    try {
        if (-not ('Win32.FontApi' -as [type])) {
            Add-Type -Name FontApi -Namespace Win32 -MemberDefinition @'
[DllImport("gdi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern int AddFontResourceW(string lpFileName);

[DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
        }
        foreach ($path in $installedPaths) {
            [void][Win32.FontApi]::AddFontResourceW($path)
        }
        $result = [UIntPtr]::Zero
        # HWND_BROADCAST = 0xffff, WM_FONTCHANGE = 0x001D, SMTO_ABORTIFHUNG = 0x0002
        [void][Win32.FontApi]::SendMessageTimeout([IntPtr]0xffff, 0x001D, [IntPtr]::Zero, [IntPtr]::Zero, 0x0002, 1000, [ref]$result)
        Write-Host ''
        Write-Host 'LOAD  faces registered with the running session'
    }
    catch {
        Write-Host ''
        Write-Host "NOTE  could not notify running applications ($($_.Exception.Message)); sign out and back in to pick the font up"
    }
}

Write-Host ''
Write-Host "==> Done. $copied file(s) written, $registered registry value(s) set."
Write-Host ''
Write-Host '----------------------------------------------------------------------'
Write-Host 'Point your terminal at the font. Nothing below is applied automatically.'
Write-Host '----------------------------------------------------------------------'
Write-Host ''
Write-Host 'Windows Terminal -- Settings -> "Open JSON file", then merge this into'
Write-Host 'the top-level "profiles" object so it applies to every profile:'
Write-Host ''
Write-Host '    "profiles":'
Write-Host '    {'
Write-Host '        "defaults":'
Write-Host '        {'
Write-Host '            "font":'
Write-Host '            {'
Write-Host "                `"face`": `"$FontFamily`""
Write-Host '            }'
Write-Host '        }'
Write-Host '    }'
Write-Host ''
Write-Host 'The same thing without JSON: Settings -> Defaults -> Appearance -> Font face.'
Write-Host ''
Write-Host "PuTTY -- Window -> Appearance -> Font -> Change..., pick `"$FontFamily`", and save the session."
Write-Host ''
