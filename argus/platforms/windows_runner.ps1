$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class ArgusNative {
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hwnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int length);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, int data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@ | Out-Null

[void][ArgusNative]::SetProcessDPIAware()

$script:App = ""
$script:Launch = ""
$script:TargetProcessId = 0
$script:Hwnd = [IntPtr]::Zero
$script:Rect = $null

function Write-Response([object]$value) {
    [Console]::Out.WriteLine(($value | ConvertTo-Json -Compress -Depth 6))
    [Console]::Out.Flush()
}

function Get-WindowTitle([IntPtr]$hwnd) {
    $length = [ArgusNative]::GetWindowTextLength($hwnd)
    if ($length -le 0) { return "" }
    $text = [Text.StringBuilder]::new($length + 1)
    [void][ArgusNative]::GetWindowText($hwnd, $text, $text.Capacity)
    return $text.ToString()
}

function Find-TargetWindow {
    $needle = $script:App
    $best = [IntPtr]::Zero
    $bestArea = 0L
    $callback = [ArgusNative+EnumWindowsProc] {
        param([IntPtr]$hwnd, [IntPtr]$unused)
        if (-not [ArgusNative]::IsWindowVisible($hwnd)) { return $true }
        if ($script:TargetProcessId -gt 0) {
            [uint32]$windowProcessId = 0
            [void][ArgusNative]::GetWindowThreadProcessId($hwnd, [ref]$windowProcessId)
            if ($windowProcessId -ne $script:TargetProcessId) { return $true }
        }
        $title = Get-WindowTitle $hwnd
        if ($title.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $true }
        $rect = [ArgusNative+RECT]::new()
        if (-not [ArgusNative]::GetWindowRect($hwnd, [ref]$rect)) { return $true }
        $area = [int64]($rect.Right - $rect.Left) * [int64]($rect.Bottom - $rect.Top)
        if ($area -gt $bestArea) {
            $script:enumBest = $hwnd
            $script:enumBestArea = $area
        }
        return $true
    }
    $script:enumBest = [IntPtr]::Zero
    $script:enumBestArea = 0L
    [void][ArgusNative]::EnumWindows($callback, [IntPtr]::Zero)
    return $script:enumBest
}

function Refresh-TargetWindow {
    $hwnd = Find-TargetWindow
    if ($hwnd -eq [IntPtr]::Zero) {
        throw "Target window '$($script:App)' was not found."
    }
    if ([ArgusNative]::IsIconic($hwnd)) { [void][ArgusNative]::ShowWindow($hwnd, 9) }
    [void][ArgusNative]::SetForegroundWindow($hwnd)
    Start-Sleep -Milliseconds 150
    $rect = [ArgusNative+RECT]::new()
    if (-not [ArgusNative]::GetWindowRect($hwnd, [ref]$rect)) {
        throw "Could not read target window bounds."
    }
    $script:Hwnd = $hwnd
    $script:Rect = $rect
}

function Invoke-Key([byte]$vk, [bool]$down) {
    $flag = if ($down) { 0 } else { 2 }
    [ArgusNative]::keybd_event($vk, 0, $flag, [UIntPtr]::Zero)
}

function Press-Key([byte]$vk) {
    Invoke-Key $vk $true
    Invoke-Key $vk $false
}

while (($line = [Console]::In.ReadLine()) -ne $null) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $request = $null
    try {
        $request = $line | ConvertFrom-Json
        switch ($request.command) {
            "setup" {
                $script:App = [string]$request.app
                $script:Launch = [string]$request.launch
                if ([string]::IsNullOrWhiteSpace($script:App)) { throw "WIN_APP is required." }
                if (-not [string]::IsNullOrWhiteSpace($script:Launch)) {
                    $launched = Start-Process -FilePath $script:Launch -PassThru
                    $script:TargetProcessId = $launched.Id
                }
                $deadline = [DateTime]::UtcNow.AddSeconds(12)
                do {
                    $script:Hwnd = Find-TargetWindow
                    if ($script:Hwnd -ne [IntPtr]::Zero) { break }
                    Start-Sleep -Milliseconds 300
                } while ([DateTime]::UtcNow -lt $deadline)
                Refresh-TargetWindow
                Write-Response @{ id=$request.id; ok=$true; data=@{
                    width=$script:Rect.Right-$script:Rect.Left
                    height=$script:Rect.Bottom-$script:Rect.Top
                    title=(Get-WindowTitle $script:Hwnd)
                }}
            }
            "screenshot" {
                Refresh-TargetWindow
                $width = $script:Rect.Right - $script:Rect.Left
                $height = $script:Rect.Bottom - $script:Rect.Top
                $bitmap = [Drawing.Bitmap]::new($width, $height, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
                $graphics = [Drawing.Graphics]::FromImage($bitmap)
                $stream = [IO.MemoryStream]::new()
                try {
                    $graphics.CopyFromScreen($script:Rect.Left, $script:Rect.Top, 0, 0, $bitmap.Size)
                    $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
                    Write-Response @{ id=$request.id; ok=$true; data=@{
                        png=[Convert]::ToBase64String($stream.ToArray())
                        width=$width; height=$height
                    }}
                } finally {
                    $stream.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
                }
            }
            "tap" {
                Refresh-TargetWindow
                $globalX = [int]$script:Rect.Left + [int]$request.x
                $globalY = [int]$script:Rect.Top + [int]$request.y
                [void][ArgusNative]::SetCursorPos($globalX, $globalY)
                [ArgusNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
                [ArgusNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
                Write-Response @{ id=$request.id; ok=$true }
            }
            "swipe" {
                Refresh-TargetWindow
                $startX = [int]$script:Rect.Left + [int]$request.x1
                $startY = [int]$script:Rect.Top + [int]$request.y1
                $endX = [int]$script:Rect.Left + [int]$request.x2
                $endY = [int]$script:Rect.Top + [int]$request.y2
                [void][ArgusNative]::SetCursorPos($startX, $startY)
                [ArgusNative]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
                for ($i = 1; $i -le 12; $i++) {
                    $x = [int]($startX + (($endX - $startX) * $i / 12))
                    $y = [int]($startY + (($endY - $startY) * $i / 12))
                    [void][ArgusNative]::SetCursorPos($x, $y)
                    Start-Sleep -Milliseconds 25
                }
                [ArgusNative]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
                Write-Response @{ id=$request.id; ok=$true }
            }
            "scroll" {
                Refresh-TargetWindow
                $delta = if ([string]$request.direction -eq "up") { 600 } else { -600 }
                [ArgusNative]::mouse_event(2048, 0, 0, $delta, [UIntPtr]::Zero)
                Write-Response @{ id=$request.id; ok=$true }
            }
            "input" {
                Refresh-TargetWindow
                [Windows.Forms.Clipboard]::SetText([string]$request.text)
                Start-Sleep -Milliseconds 100
                [Windows.Forms.SendKeys]::SendWait("^v")
                Start-Sleep -Milliseconds 100
                Write-Response @{ id=$request.id; ok=$true }
            }
            "key" {
                Refresh-TargetWindow
                $keys = @{
                    enter=0x0D; return=0x0D; tab=0x09; escape=0x1B; esc=0x1B
                    backspace=0x08; delete=0x2E; space=0x20; left=0x25
                    up=0x26; right=0x27; down=0x28; home=0x24; end=0x23
                    pageup=0x21; pagedown=0x22
                }
                $name = ([string]$request.key).ToLowerInvariant()
                if (-not $keys.ContainsKey($name)) { throw "Unsupported key '$name'." }
                Press-Key ([byte]$keys[$name])
                Write-Response @{ id=$request.id; ok=$true }
            }
            "open" {
                Start-Process -FilePath ([string]$request.target) | Out-Null
                Write-Response @{ id=$request.id; ok=$true }
            }
            "teardown" {
                Write-Response @{ id=$request.id; ok=$true }
                exit 0
            }
            default { throw "Unknown command '$($request.command)'." }
        }
    } catch {
        $id = if ($null -ne $request) { $request.id } else { $null }
        Write-Response @{ id=$id; ok=$false; error=$_.Exception.Message }
    }
}
