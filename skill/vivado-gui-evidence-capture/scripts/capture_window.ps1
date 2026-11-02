<#
.SYNOPSIS
    采集指定窗口的真实截图（DPI 感知，窗口级裁剪）。
.EXAMPLE
    .\capture_window.ps1 -OutPath impl.png -TitleMatch Vivado
#>
param(
    [Parameter(Mandatory = $true)][string]$OutPath,
    [string]$TitleMatch = "Vivado"
)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinCap {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[WinCap]::SetProcessDPIAware() | Out-Null
$proc = Get-Process | Where-Object { $_.MainWindowTitle -like "*$TitleMatch*" } | Select-Object -First 1
if (-not $proc) { throw "window not found: $TitleMatch" }
$h = $proc.MainWindowHandle
[WinCap]::ShowWindow($h, 3) | Out-Null
[WinCap]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Seconds 2
$r = New-Object WinCap+RECT
[WinCap]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left
$hh = $r.Bottom - $r.Top
$bmp = New-Object System.Drawing.Bitmap($w, $hh)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved $OutPath ($w x $hh)"
