$ErrorActionPreference = "Stop"
# ASCII-only file: Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI, so any
# non-ASCII byte here would misparse and silently swallow the following line.
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName Accessibility
Add-Type -ReferencedAssemblies Accessibility @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class ArgusNative {
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X, Y; public POINT(int x, int y) { X = x; Y = y; } }

    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hwnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int length);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();

    // Background window capture: independent of focus/occlusion
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint flags);
    [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr hwnd, IntPtr hdc);
    [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleDC(IntPtr hdc);
    [DllImport("gdi32.dll")] public static extern IntPtr CreateCompatibleBitmap(IntPtr hdc, int width, int height);
    [DllImport("gdi32.dll")] public static extern IntPtr SelectObject(IntPtr hdc, IntPtr obj);
    [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr obj);
    [DllImport("gdi32.dll")] public static extern bool DeleteDC(IntPtr hdc);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, uint attribute, out int value, int size);

    // Background input: window-message injection, no global mouse/keyboard/clipboard/foreground
    [DllImport("user32.dll")] public static extern int MapWindowPoints(IntPtr from, IntPtr to, ref POINT points, uint count);
    [DllImport("user32.dll")] public static extern IntPtr ChildWindowFromPointEx(IntPtr hwndParent, POINT pt, uint flags);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern bool PostMessageW(IntPtr hwnd, uint msg, UIntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr SendMessageW(IntPtr hwnd, uint msg, UIntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint MapVirtualKey(uint vk, uint mapType);
    [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hwnd);
    [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId, ref Guid iid, out Accessibility.IAccessible acc);
}
"@ | Out-Null

[void][ArgusNative]::SetProcessDPIAware()
# Keep the desktop clean: this runner needs no visible console of its own.
$script:ConsoleHwnd = [ArgusNative]::GetConsoleWindow()
if ($script:ConsoleHwnd -ne [IntPtr]::Zero) { [void][ArgusNative]::ShowWindow($script:ConsoleHwnd, 0) }

$script:App = ""
$script:Launch = ""
$script:TargetProcessId = 0
$script:Hwnd = [IntPtr]::Zero
$script:FocusChild = [IntPtr]::Zero
$script:Rect = $null

# --- Win32 message constants ---
$script:WM_MOUSEMOVE     = 0x0200
$script:WM_LBUTTONDOWN   = 0x0201
$script:WM_LBUTTONUP     = 0x0202
$script:WM_MOUSEWHEEL    = 0x020A
$script:WM_NCHITTEST     = 0x0084
$script:WM_NCLBUTTONDOWN = 0x00A1
$script:WM_NCLBUTTONUP   = 0x00A2
$script:WM_KEYDOWN       = 0x0100
$script:WM_KEYUP         = 0x0101
$script:WM_SYSKEYDOWN    = 0x0104
$script:WM_SYSKEYUP      = 0x0105
$script:WM_CHAR          = 0x0102
$script:BM_CLICK         = 0x00F5
$script:MK_LBUTTON       = 0x0001

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

function Get-WindowClass([IntPtr]$hwnd) {
    $text = [Text.StringBuilder]::new(256)
    [void][ArgusNative]::GetClassName($hwnd, $text, $text.Capacity)
    return $text.ToString()
}

function Test-Cloaked([IntPtr]$hwnd) {
    [int]$cloaked = 0
    $hr = [ArgusNative]::DwmGetWindowAttribute($hwnd, 14, [ref]$cloaked, 4)
    return ($hr -eq 0 -and $cloaked -ne 0)
}

function Test-WindowUsable([IntPtr]$hwnd) {
    if (-not [ArgusNative]::IsWindowVisible($hwnd)) { return $false }
    if ([ArgusNative]::IsIconic($hwnd)) { return $false }
    if (Test-Cloaked $hwnd) { return $false }
    $rect = [ArgusNative+RECT]::new()
    if (-not [ArgusNative]::GetWindowRect($hwnd, [ref]$rect)) { return $false }
    return (($rect.Right - $rect.Left) -gt 0 -and ($rect.Bottom - $rect.Top) -gt 0)
}

function Find-TargetWindow {
    # With a PID lock, take the topmost usable top-level window of that process in
    # Z-order (EnumWindows enumerates top-first, so a modal dialog beats its owner);
    # otherwise fall back to title substring + largest area.
    if ($script:TargetProcessId -gt 0) {
        $script:enumBest = [IntPtr]::Zero
        $callback = [ArgusNative+EnumWindowsProc] {
            param([IntPtr]$hwnd, [IntPtr]$unused)
            [uint32]$windowProcessId = 0
            [void][ArgusNative]::GetWindowThreadProcessId($hwnd, [ref]$windowProcessId)
            if ($windowProcessId -eq $script:TargetProcessId -and (Test-WindowUsable $hwnd)) {
                $script:enumBest = $hwnd
                return $false
            }
            return $true
        }
        [void][ArgusNative]::EnumWindows($callback, [IntPtr]::Zero)
        if ($script:enumBest -ne [IntPtr]::Zero) { return $script:enumBest }
    }
    $needle = $script:App
    $script:enumBest = [IntPtr]::Zero
    $script:enumBestArea = 0L
    $callback = [ArgusNative+EnumWindowsProc] {
        param([IntPtr]$hwnd, [IntPtr]$unused)
        if (-not (Test-WindowUsable $hwnd)) { return $true }
        $title = Get-WindowTitle $hwnd
        if ($title.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $true }
        $rect = [ArgusNative+RECT]::new()
        if (-not [ArgusNative]::GetWindowRect($hwnd, [ref]$rect)) { return $true }
        $area = [int64]($rect.Right - $rect.Left) * [int64]($rect.Bottom - $rect.Top)
        if ($area -gt $script:enumBestArea) {
            $script:enumBest = $hwnd
            $script:enumBestArea = $area
        }
        return $true
    }
    [void][ArgusNative]::EnumWindows($callback, [IntPtr]::Zero)
    return $script:enumBest
}

function Find-EditChild([IntPtr]$top) {
    $script:enumEdit = [IntPtr]::Zero
    $callback = [ArgusNative+EnumWindowsProc] {
        param([IntPtr]$hwnd, [IntPtr]$unused)
        if ($script:enumEdit -eq [IntPtr]::Zero -and (Get-WindowClass $hwnd) -eq "Edit") {
            $script:enumEdit = $hwnd
        }
        return $true
    }
    [void][ArgusNative]::EnumChildWindows($top, $callback, [IntPtr]::Zero)
    return $script:enumEdit
}

function Count-ProcessTopLevels([int]$processId) {
    $script:enumCount = 0
    $callback = [ArgusNative+EnumWindowsProc] {
        param([IntPtr]$hwnd, [IntPtr]$unused)
        [uint32]$windowProcessId = 0
        [void][ArgusNative]::GetWindowThreadProcessId($hwnd, [ref]$windowProcessId)
        if ($windowProcessId -eq $processId -and (Test-WindowUsable $hwnd)) { $script:enumCount += 1 }
        return $true
    }
    [void][ArgusNative]::EnumWindows($callback, [IntPtr]::Zero)
    return $script:enumCount
}

function Refresh-TargetWindow {
    $hwnd = Find-TargetWindow
    if ($hwnd -eq [IntPtr]::Zero) { return $false }
    if ([ArgusNative]::IsIconic($hwnd)) { [void][ArgusNative]::ShowWindow($hwnd, 8) }  # SW_SHOWNA: restore without stealing focus
    if ($hwnd -ne $script:Hwnd) { $script:FocusChild = [IntPtr]::Zero }
    $rect = [ArgusNative+RECT]::new()
    if (-not [ArgusNative]::GetWindowRect($hwnd, [ref]$rect)) {
        throw "Could not read target window bounds."
    }
    $script:Hwnd = $hwnd
    $script:Rect = $rect
    return $true
}

function Test-Uniform([Drawing.Bitmap]$bmp) {
    # PrintWindow failure shows up as a single-color image (all black/transparent);
    # real window content (title bar etc.) differs within the first pixels, so the
    # early-exit scan is cheap.
    $bounds = [Drawing.Rectangle]::new(0, 0, $bmp.Width, $bmp.Height)
    $data = $bmp.LockBits($bounds, [Drawing.Imaging.ImageLockMode]::ReadOnly, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        $count = $data.Height * $data.Stride
        $bytes = New-Object byte[] $count
        [Runtime.InteropServices.Marshal]::Copy($data.Scan0, $bytes, 0, $count)
        $b0 = $bytes[0]; $g0 = $bytes[1]; $r0 = $bytes[2]; $a0 = $bytes[3]
        for ($i = 4; $i -lt $count; $i += 4) {
            if ($bytes[$i] -ne $b0 -or $bytes[$i + 1] -ne $g0 -or $bytes[$i + 2] -ne $r0 -or $bytes[$i + 3] -ne $a0) {
                return $false
            }
        }
        return $true
    } finally {
        $bmp.UnlockBits($data)
    }
}

function Capture-Screen([int]$x, [int]$y, [int]$w, [int]$h) {
    $bitmap = [Drawing.Bitmap]::new($w, $h, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try { $graphics.CopyFromScreen($x, $y, 0, 0, [Drawing.Size]::new($w, $h)) } finally { $graphics.Dispose() }
    return $bitmap
}

function Capture-Window([IntPtr]$hwnd, $rect) {
    $w = [int]($rect.Right - $rect.Left)
    $h = [int]($rect.Bottom - $rect.Top)
    if ($w -le 0 -or $h -le 0) { throw "Bad target window rect." }
    $hdc = [ArgusNative]::GetDC($hwnd)
    $mem = [ArgusNative]::CreateCompatibleDC($hdc)
    $hbm = [ArgusNative]::CreateCompatibleBitmap($hdc, $w, $h)
    $old = [ArgusNative]::SelectObject($mem, $hbm)
    try {
        $ok = [ArgusNative]::PrintWindow($hwnd, $mem, 2)   # PW_RENDERFULLCONTENT
        if ($ok) {
            $bitmap = [Drawing.Image]::FromHbitmap($hbm)
            if (-not (Test-Uniform $bitmap)) { return $bitmap }
            $bitmap.Dispose()
        }
        return (Capture-Screen $rect.Left $rect.Top $w $h)
    } finally {
        [void][ArgusNative]::SelectObject($mem, $old)
        [void][ArgusNative]::DeleteObject($hbm)
        [void][ArgusNative]::DeleteDC($mem)
        [void][ArgusNative]::ReleaseDC($hwnd, $hdc)
    }
}

function Make-LParam([int]$x, [int]$y) {
    return [IntPtr]((([int64]($y -band 0xFFFF)) -shl 32) -bor ([int64]($x -band 0xFFFF)))
}

function Resolve-ChildAt([IntPtr]$hwnd, [int]$screenX, [int]$screenY) {
    # Screen point -> hwnd client space -> drill down to the deepest child window;
    # returns that child plus the point in the child's client coordinates.
    $pt = [ArgusNative+POINT]::new($screenX, $screenY)
    [void][ArgusNative]::MapWindowPoints([IntPtr]::Zero, $hwnd, [ref]$pt, 1)
    $parent = $hwnd
    while ($true) {
        $child = [ArgusNative]::ChildWindowFromPointEx($parent, $pt, 7)  # SKIPINVISIBLE|SKIPDISABLED|SKIPTRANSPARENT
        if ($child -eq [IntPtr]::Zero -or $child -eq $parent) { break }
        [void][ArgusNative]::MapWindowPoints($parent, $child, [ref]$pt, 1)
        $parent = $child
    }
    return @{ Hwnd = $parent; X = $pt.X; Y = $pt.Y }
}

function Try-UIAClick([int]$screenX, [int]$screenY) {
    # DirectUI surfaces (file dialog nav tree / item view) have no real child
    # HWNDs, so posted mouse messages never reach their inner elements. UIA
    # works cross-process and does not require foreground focus.
    try {
        $el = [System.Windows.Automation.AutomationElement]::FromPoint(
            [System.Windows.Point]::new([double]$screenX, [double]$screenY))
        if ($null -eq $el) { return $false }
        if ($script:TargetProcessId -gt 0 -and $el.Current.ProcessId -ne $script:TargetProcessId) {
            return $false   # something else covers the dialog at this point
        }
        $pat = $null
        if ($el.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pat)) {
            $pat.Select()
            return $true
        }
        if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pat)) {
            $pat.Invoke()
            return $true
        }
    } catch { }
    return $false
}

function Select-TreeItemAt([IntPtr]$tree, [int]$screenX, [int]$screenY) {
    # Shell namespace tree (file dialog / Explorer nav pane, class
    # NamespaceTreeControl hosting a SysTreeView32). Posted mouse clicks only
    # move the TreeView caret: NM_CLICK carries no coordinates, so the host
    # hit-tests with GetMessagePos() (the REAL cursor) and never navigates.
    # UIA shows the tree as an empty Pane, but the host's MSAA provider exposes
    # every node with its screen rect and a "Navigate" default action. Still a
    # pure coordinate click: pick the node whose rect contains the point.
    if ((Get-WindowClass ([ArgusNative]::GetParent($tree))) -ne "NamespaceTreeControl") { return $false }
    try {
        $iid = [Guid]"618736E0-3C3D-11CF-810C-00AA00389B71"   # IID_IAccessible
        $acc = $null
        if ([ArgusNative]::AccessibleObjectFromWindow($tree, [uint32]4294967292, [ref]$iid, [ref]$acc) -ne 0) { return $false }  # OBJID_CLIENT
        $tr = [ArgusNative+RECT]::new()
        [void][ArgusNative]::GetWindowRect($tree, [ref]$tr)
        $count = [int]$acc.accChildCount
        for ($i = 1; $i -le $count; $i++) {
            $x = 0; $y = 0; $w = 0; $h = 0
            try { $acc.accLocation([ref]$x, [ref]$y, [ref]$w, [ref]$h, $i) } catch { continue }
            if ($w -le 0 -or $h -le 0) { continue }   # collapsed / scrolled out
            # accLocation covers only the label; a click anywhere on that row
            # of the pane (icon, indent, trailing space) selects the same node.
            if ($screenY -ge $y -and $screenY -lt ($y + $h) -and $screenX -ge $tr.Left -and $screenX -lt $tr.Right) {
                try { $acc.accSelect(2, $i) } catch { }                     # SELFLAG_TAKESELECTION
                try { $acc.accDoDefaultAction($i) } catch { return $false }  # "Navigate"
                return $true
            }
        }
    } catch { }
    return $false
}

function Press-TitleBarButtonAt([IntPtr]$hwnd, [int]$screenX, [int]$screenY, [int]$ncBottom) {
    # Standard caption buttons (min/max/close) via the MSAA title-bar object.
    # Works for any window with a system caption regardless of focus, and does
    # not depend on WM_NCHITTEST (Win11 Notepad answers HTNOWHERE to sent
    # hit-tests, so posted NC clicks never reach its close button).
    # MSAA reports the classic caption height only; custom title bars (Win11
    # Notepad) draw the buttons taller, so match X against the button rect and
    # accept any Y from the rect top down to the client area top (ncBottom).
    try {
        $iid = [Guid]"618736E0-3C3D-11CF-810C-00AA00389B71"   # IID_IAccessible
        $acc = $null
        if ([ArgusNative]::AccessibleObjectFromWindow($hwnd, [uint32]4294967294, [ref]$iid, [ref]$acc) -ne 0) { return $false }  # OBJID_TITLEBAR
        $count = [int]$acc.accChildCount
        for ($i = 1; $i -le $count; $i++) {
            $x = 0; $y = 0; $w = 0; $h = 0
            try { $acc.accLocation([ref]$x, [ref]$y, [ref]$w, [ref]$h, $i) } catch { continue }
            if ($w -le 0 -or $h -le 0) { continue }
            $bottom = [Math]::Max($y + $h, $ncBottom)
            if ($screenX -ge $x -and $screenX -lt ($x + $w) -and $screenY -ge $y -and $screenY -lt $bottom) {
                try { $acc.accDoDefaultAction($i) } catch { return $false }   # "Press"
                return $true
            }
        }
    } catch { }
    return $false
}

function Make-KeyLParam([int]$vk, [bool]$up, [bool]$alt) {
    $scan = [ArgusNative]::MapVirtualKey([uint32]$vk, 0)
    [int64]$lp = 1                                   # repeat count
    $lp = $lp -bor (([int64]$scan -band 0xFF) -shl 16)
    if (@(0x25, 0x26, 0x27, 0x28, 0x23, 0x24, 0x21, 0x22, 0x2E, 0x2D) -contains $vk) {
        $lp = $lp -bor (1 -shl 24)                   # extended key
    }
    if ($alt) { $lp = $lp -bor (1 -shl 29) }          # context code (Alt)
    if ($up) { $lp = $lp -bor (1 -shl 30) -bor (1 -shl 31) }
    return [IntPtr]$lp
}

function Post-Key([IntPtr]$hwnd, [int]$vk, [bool]$up, [bool]$alt) {
    $msg = if ($alt) { if ($up) { $script:WM_SYSKEYUP } else { $script:WM_SYSKEYDOWN } }
           else { if ($up) { $script:WM_KEYUP } else { $script:WM_KEYDOWN } }
    [void][ArgusNative]::PostMessageW($hwnd, [uint32]$msg, [UIntPtr]([uint64]$vk), (Make-KeyLParam $vk $up $alt))
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
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                Write-Response @{ id=$request.id; ok=$true; data=@{
                    width=$script:Rect.Right-$script:Rect.Left
                    height=$script:Rect.Bottom-$script:Rect.Top
                    title=(Get-WindowTitle $script:Hwnd)
                    desktop_path=[Environment]::GetFolderPath('Desktop')
                }}
            }
            "screenshot" {
                if (Refresh-TargetWindow) {
                    $bitmap = Capture-Window $script:Hwnd $script:Rect
                } else {
                    # Target window gone (e.g. final close-verification step):
                    # capture the virtual screen so the agent sees the desktop.
                    $script:Hwnd = [IntPtr]::Zero
                    $script:FocusChild = [IntPtr]::Zero
                    $vs = [Windows.Forms.SystemInformation]::VirtualScreen
                    $bitmap = Capture-Screen $vs.X $vs.Y $vs.Width $vs.Height
                }
                $stream = [IO.MemoryStream]::new()
                try {
                    $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
                    Write-Response @{ id=$request.id; ok=$true; data=@{
                        png=[Convert]::ToBase64String($stream.ToArray())
                        width=$bitmap.Width; height=$bitmap.Height
                    }}
                } finally {
                    $stream.Dispose()
                    $bitmap.Dispose()
                }
            }
            "tap" {
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                $sx = [int]$script:Rect.Left + [int]$request.x
                $sy = [int]$script:Rect.Top + [int]$request.y
                $pt = [ArgusNative+POINT]::new($sx, $sy)
                [void][ArgusNative]::MapWindowPoints([IntPtr]::Zero, $script:Hwnd, [ref]$pt, 1)
                $cr = [ArgusNative+RECT]::new()
                [void][ArgusNative]::GetClientRect($script:Hwnd, [ref]$cr)
                $inClient = ($pt.X -ge 0 -and $pt.Y -ge 0 -and $pt.X -lt $cr.Right -and $pt.Y -lt $cr.Bottom)
                if ($inClient) {
                    $hit = Resolve-ChildAt $script:Hwnd $sx $sy
                    $script:FocusChild = $hit.Hwnd
                    $lp = Make-LParam $hit.X $hit.Y
                    $cls = Get-WindowClass $hit.Hwnd
                    if ($cls -eq "Button") {
                        [void][ArgusNative]::SendMessageW($hit.Hwnd, $script:BM_CLICK, [UIntPtr]::Zero, [IntPtr]::Zero)
                    } elseif ($cls -eq "SysTreeView32" -and (Select-TreeItemAt $hit.Hwnd $sx $sy)) {
                        # shell nav tree node navigated via MSAA default action (background-safe)
                    } elseif ($cls -eq "DirectUIHWND" -and (Try-UIAClick $sx $sy)) {
                        # UIA handled it (nav tree item, dialog button, list item)
                    } else {
                        [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_MOUSEMOVE, [UIntPtr]::Zero, $lp)
                        [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_LBUTTONDOWN, [UIntPtr]([uint64]$script:MK_LBUTTON), $lp)
                        [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_LBUTTONUP, [UIntPtr]::Zero, $lp)
                    }
                } else {
                    # Non-client area (title bar / close button). Caption buttons
                    # go through the MSAA title-bar object; anything else asks
                    # WM_NCHITTEST for the hit code and posts NC clicks with it.
                    $sp = Make-LParam $sx $sy
                    $co = [ArgusNative+POINT]::new(0, 0)
                    [void][ArgusNative]::MapWindowPoints($script:Hwnd, [IntPtr]::Zero, [ref]$co, 1)
                    if (Press-TitleBarButtonAt $script:Hwnd $sx $sy $co.Y) {
                        $code = -1   # handled
                    } else {
                        # SendMessageW returns IntPtr; go through int64 before uint64.
                        $code = [int64][ArgusNative]::SendMessageW($script:Hwnd, $script:WM_NCHITTEST, [UIntPtr]::Zero, $sp)
                    }
                    # 10..17 = HTLEFT..HTBOTTOMRIGHT: posting those starts a modal
                    # resize drag (out-of-range model taps land on the border).
                    if ($code -eq 20) {
                        # HTCLOSE: posted NC clicks are ignored by Win11 Notepad's
                        # custom title bar; SC_CLOSE always works in background.
                        [void][ArgusNative]::PostMessageW($script:Hwnd, 0x0112, [UIntPtr]([uint64]0xF060), [IntPtr]::Zero)
                    } elseif ($code -gt 0 -and -not ($code -ge 10 -and $code -le 17)) {
                        $wp = [UIntPtr]([uint64]$code)
                        [void][ArgusNative]::PostMessageW($script:Hwnd, $script:WM_NCLBUTTONDOWN, $wp, $sp)
                        [void][ArgusNative]::PostMessageW($script:Hwnd, $script:WM_NCLBUTTONUP, $wp, $sp)
                    }
                    $script:FocusChild = [IntPtr]::Zero
                }
                Write-Response @{ id=$request.id; ok=$true }
            }
            "swipe" {
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                $startX = [int]$script:Rect.Left + [int]$request.x1
                $startY = [int]$script:Rect.Top + [int]$request.y1
                $endX = [int]$script:Rect.Left + [int]$request.x2
                $endY = [int]$script:Rect.Top + [int]$request.y2
                $hit = Resolve-ChildAt $script:Hwnd $startX $startY
                $script:FocusChild = $hit.Hwnd
                $pt = [ArgusNative+POINT]::new($startX, $startY)
                [void][ArgusNative]::MapWindowPoints([IntPtr]::Zero, $hit.Hwnd, [ref]$pt, 1)
                [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_MOUSEMOVE, [UIntPtr]::Zero, (Make-LParam $pt.X $pt.Y))
                [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_LBUTTONDOWN, [UIntPtr]([uint64]$script:MK_LBUTTON), (Make-LParam $pt.X $pt.Y))
                for ($i = 1; $i -le 12; $i++) {
                    $mx = [int]($startX + (($endX - $startX) * $i / 12))
                    $my = [int]($startY + (($endY - $startY) * $i / 12))
                    $mpt = [ArgusNative+POINT]::new($mx, $my)
                    [void][ArgusNative]::MapWindowPoints([IntPtr]::Zero, $hit.Hwnd, [ref]$mpt, 1)
                    [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_MOUSEMOVE, [UIntPtr]([uint64]$script:MK_LBUTTON), (Make-LParam $mpt.X $mpt.Y))
                    Start-Sleep -Milliseconds 25
                }
                $upt = [ArgusNative+POINT]::new($endX, $endY)
                [void][ArgusNative]::MapWindowPoints([IntPtr]::Zero, $hit.Hwnd, [ref]$upt, 1)
                [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_LBUTTONUP, [UIntPtr]::Zero, (Make-LParam $upt.X $upt.Y))
                Write-Response @{ id=$request.id; ok=$true }
            }
            "scroll" {
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                $cx = [int](($script:Rect.Right - $script:Rect.Left) / 2)
                $cy = [int](($script:Rect.Bottom - $script:Rect.Top) / 2)
                $hit = Resolve-ChildAt $script:Hwnd ($script:Rect.Left + $cx) ($script:Rect.Top + $cy)
                [int16]$delta = if ([string]$request.direction -eq "up") { 600 } else { -600 }
                $wp = [UIntPtr]([uint64]((([int64]([uint16]$delta)) -band 0xFFFF) -shl 16))
                [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_MOUSEWHEEL, $wp, (Make-LParam ($script:Rect.Left + $cx) ($script:Rect.Top + $cy)))
                Write-Response @{ id=$request.id; ok=$true }
            }
            "input" {
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                $target = $script:FocusChild
                if ($target -eq [IntPtr]::Zero -or -not [ArgusNative]::IsWindow($target)) {
                    $cx = [int](($script:Rect.Right - $script:Rect.Left) / 2)
                    $cy = [int](($script:Rect.Bottom - $script:Rect.Top) / 2)
                    $target = (Resolve-ChildAt $script:Hwnd ($script:Rect.Left + $cx) ($script:Rect.Top + $cy)).Hwnd
                    $script:FocusChild = $target
                }
                # Text only lands in Edit controls; a stale FocusChild (e.g. a
                # Button hit by the last tap) would silently swallow it.
                if ((Get-WindowClass $target) -ne "Edit") {
                    $ed = Find-EditChild $script:Hwnd
                    if ($ed -ne [IntPtr]::Zero) { $target = $ed; $script:FocusChild = $ed }
                }
                foreach ($ch in [char[]][string]$request.text) {
                    [void][ArgusNative]::PostMessageW($target, $script:WM_CHAR, [UIntPtr]([uint64][char]$ch), [IntPtr]::Zero)
                }
                Write-Response @{ id=$request.id; ok=$true }
            }
            "key" {
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                $vkTable = @{
                    enter=0x0D; return=0x0D; tab=0x09; escape=0x1B; esc=0x1B
                    backspace=0x08; delete=0x2E; space=0x20; left=0x25
                    up=0x26; right=0x27; down=0x28; home=0x24; end=0x23
                    pageup=0x21; pagedown=0x22
                }
                $modTable = @{ ctrl=0x11; control=0x11; shift=0x10; alt=0x12; win=0x5B }
                $name = ([string]$request.key).ToLowerInvariant()
                $parts = @($name -split '\+' | Where-Object { $_ -ne '' })
                if ($parts.Count -eq 0) { throw "Unsupported key '$name'." }
                $mainName = $parts[-1]
                # NB: $parts[0..-1] would return the WHOLE array in PowerShell,
                # so single keys must not slice at all.
                $modNames = @()
                if ($parts.Count -gt 1) { $modNames = @($parts[0..($parts.Count - 2)]) }
                foreach ($m in $modNames) {
                    if (-not $modTable.ContainsKey($m)) { throw "Unsupported modifier '$m'." }
                }
                $hasAlt = $modNames -contains "alt"

                if ($modNames.Count -gt 0) {
                    # Combo keys: the message loop's TranslateAccelerator matches
                    # them straight from the thread queue.
                    if (-not $vkTable.ContainsKey($mainName) -and $mainName.Length -ne 1) {
                        throw "Unsupported key '$name'."
                    }
                    $mainVk = if ($vkTable.ContainsKey($mainName)) { $vkTable[$mainName] } else { [int][char]::ToUpperInvariant($mainName[0]) }
                    # Combos always go to the TOP-LEVEL window: accel tables read
                    # the thread queue (same queue for children), and posting to an
                    # Edit child would make TranslateMessage synthesize WM_CHAR
                    # (GetKeyState never sees the posted modifier) -- that is how a
                    # stray 's' leaked into the document on ctrl+s.
                    $hwnd = $script:Hwnd
                    if ($modNames.Count -eq 1 -and $modNames[0] -eq "ctrl" -and $mainName -eq "a") {
                        # Edit detects Ctrl+A through GetKeyState, which posted keys
                        # never update; EM_SETSEL selects all without key state.
                        # Prefer the Edit the last tap landed on (a dialog has
                        # several: search box, address bar, file name...).
                        if ($script:FocusChild -ne [IntPtr]::Zero -and [ArgusNative]::IsWindow($script:FocusChild) -and (Get-WindowClass $script:FocusChild) -eq "Edit") {
                            $hwnd = $script:FocusChild
                        } elseif ((Get-WindowClass $hwnd) -ne "Edit") {
                            $ed = Find-EditChild $script:Hwnd
                            if ($ed -ne [IntPtr]::Zero) { $hwnd = $ed; $script:FocusChild = $ed }
                        }
                        [void][ArgusNative]::PostMessageW($hwnd, 0x00B1, [UIntPtr]::Zero, [IntPtr](-1))
                        Write-Response @{ id=$request.id; ok=$true }
                        break
                    }
                    $topCountBefore = if ($script:TargetProcessId -gt 0) { Count-ProcessTopLevels $script:TargetProcessId } else { -1 }
                    foreach ($m in $modNames) { Post-Key $hwnd $modTable[$m] $false $hasAlt }
                    Post-Key $hwnd $mainVk $false $hasAlt
                    Post-Key $hwnd $mainVk $true $hasAlt
                    for ($i = $modNames.Count - 1; $i -ge 0; $i--) { Post-Key $hwnd $modTable[$modNames[$i]] $true $hasAlt }
                    # Classic Notepad resolves its accel table via GetKeyState, which
                    # posted messages never update, so Ctrl+S silently no-ops there.
                    # If no new top-level window (Save As dialog) appears, fall back
                    # to WM_COMMAND with the menu id.
                    if ($topCountBefore -ge 0 -and $mainVk -eq 0x53 -and ($modNames -contains "ctrl")) {
                        for ($t = 0; $t -lt 5; $t++) {
                            Start-Sleep -Milliseconds 180
                            if ((Count-ProcessTopLevels $script:TargetProcessId) -gt $topCountBefore) { break }
                            if ($t -eq 2 -or $t -eq 4) {
                                $cls = Get-WindowClass $script:Hwnd
                                if ($cls -eq "Notepad") {
                                    # 4 = File>Save (opens Save As when untitled), 5 = File>Save As.
                                    # PostMessage, never SendMessage: Notepad runs the modal
                                    # dialog inside the WM_COMMAND handler, so a synchronous
                                    # send would block this thread until the dialog closes.
                                    $cmdId = if ($t -eq 2) { 4 } else { 5 }
                                    $wp = [UIntPtr]([uint64](($cmdId -band 0xFFFF) -bor (1 -shl 16)))
                                    [void][ArgusNative]::PostMessageW($script:Hwnd, 0x0111, $wp, [IntPtr]::Zero)
                                }
                            }
                        }
                    }
                } elseif ($vkTable.ContainsKey($mainName)) {
                    $target = if ($script:FocusChild -ne [IntPtr]::Zero -and [ArgusNative]::IsWindow($script:FocusChild)) { $script:FocusChild } else { $script:Hwnd }
                    Post-Key $target $vkTable[$mainName] $false $false
                    Post-Key $target $vkTable[$mainName] $true $false
                } elseif ($mainName.Length -eq 1) {
                    $target = if ($script:FocusChild -ne [IntPtr]::Zero -and [ArgusNative]::IsWindow($script:FocusChild)) { $script:FocusChild } else { $script:Hwnd }
                    [void][ArgusNative]::PostMessageW($target, $script:WM_CHAR, [UIntPtr]([uint32][char]$mainName[0]), [IntPtr]::Zero)
                } else {
                    throw "Unsupported key '$name'."
                }
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
