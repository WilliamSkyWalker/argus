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
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT point);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
        public int dx,dy; public uint mouseData,dwFlags,time; public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk,wScan; public uint dwFlags,time; public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Explicit)] public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUTUNION data; }
    [DllImport("user32.dll", SetLastError=true)] static extern uint SendInput(uint count, INPUT[] inputs, int size);
    public static void Key(ushort key, bool up, bool unicode) {
        INPUT input = new INPUT(); input.type=1;
        input.data.ki.wVk=unicode ? (ushort)0 : key;
        input.data.ki.wScan=unicode ? key : (ushort)0;
        input.data.ki.dwFlags=(up ? 2u : 0u) | (unicode ? 4u : 0u);
        if (SendInput(1, new INPUT[]{input}, Marshal.SizeOf(typeof(INPUT))) != 1)
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "SendInput failed");
    }
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X, Y; public POINT(int x, int y) { X = x; Y = y; } }

    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool IsChild(IntPtr parent, IntPtr child);
    [StructLayout(LayoutKind.Sequential)]
    public struct GUITHREADINFO {
        public int cbSize; public uint flags;
        public IntPtr hwndActive, hwndFocus, hwndCapture, hwndMenuOwner, hwndMoveSize, hwndCaret;
        public RECT rcCaret;
    }
    [DllImport("user32.dll")] public static extern bool GetGUIThreadInfo(uint threadId, ref GUITHREADINFO info);
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

$script:Foreground = $false
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
    if ($value.ok) {
        if (-not $value.data) { $value.data = @{} }
        $value.data.input_binding = $null
        if ($script:FocusChild -ne [IntPtr]::Zero -and [ArgusNative]::IsWindow($script:FocusChild)) {
            $value.data.input_binding = @{window=$script:Hwnd.ToInt64(); target=$script:FocusChild.ToInt64();
                process_id=$script:TargetProcessId; class_name=(Get-WindowClass $script:FocusChild)}
        }
    }
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
    # Exclude non-activating, tool and click-through shadow/overlay windows.
    if (([ArgusNative]::GetWindowLong($hwnd,-20) -band 0x080000A0) -ne 0) { return $false }
    if (-not [ArgusNative]::IsWindowVisible($hwnd)) { return $false }
    if ([ArgusNative]::IsIconic($hwnd)) { return $false }
    if (Test-Cloaked $hwnd) { return $false }
    $rect = [ArgusNative+RECT]::new()
    if (-not [ArgusNative]::GetWindowRect($hwnd, [ref]$rect)) { return $false }
    return (($rect.Right - $rect.Left) -gt 1 -and ($rect.Bottom - $rect.Top) -gt 1)
}

function Resolve-DesktopApp($request) {
    # Process existence, window existence and login readiness are separate states.
    $app = [string]$request.app
    $script:PreferredTitle = $app
    $launch = [string]$request.launch
    $processName = [string]$request.process_name
    if (-not $processName -and $launch) {
        $processName = [IO.Path]::GetFileNameWithoutExtension($launch)
    }
    if (-not $processName) {
        $processName = $app
        # Display names can differ from executable names (localized applications).
        $installed = @(Get-StartApps -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $app })
        if ($installed.Count -eq 1 -and $installed[0].AppID -match '\.exe$') {
            $processName = [IO.Path]::GetFileNameWithoutExtension([string]$installed[0].AppID)
            if (-not $launch) {
                $shell = New-Object -ComObject WScript.Shell
                $roots = @([Environment]::GetFolderPath('StartMenu'), [Environment]::GetFolderPath('CommonStartMenu'))
                $links = @($roots | ForEach-Object { Get-ChildItem $_ -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue } |
                    Where-Object { $_.BaseName -eq $app })
                $paths = @($links | ForEach-Object { $shell.CreateShortcut($_.FullName).TargetPath } |
                    Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)
                if ($paths.Count -eq 1) { $launch = $paths[0] }
            }
        }
    }
    $existing = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -eq $processName -or ($_.MainWindowTitle -and $_.MainWindowTitle.IndexOf($app,[StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
    if ($request.process_id) {
        $existing = @($existing | Where-Object { $_.Id -eq [int]$request.process_id })
        if ($existing.Count -eq 0) { throw 'Bound application process exited; reconnect explicitly.' }
    }
    $script:CandidatePids = @($existing | ForEach-Object { $_.Id })
    $before = @(Get-AppWindows $script:CandidatePids)
    $launched = $false
    $newWindow = [bool]$request.new_window
    if ($newWindow -and (@($request.new_window_args).Count -eq 0 -or -not $launch)) {
        throw 'New-window mode requires an application executable and explicit supported new-window arguments.'
    }
    if ($existing.Count -eq 0 -or $newWindow) {
        if (-not $launch) { throw 'Application is not running; supply an executable with --launch.' }
        if ($newWindow) { $child = Start-Process -FilePath $launch -ArgumentList @($request.new_window_args) -PassThru }
        else { $child = Start-Process -FilePath $launch -PassThru }
        $launched = $true
        $script:CandidatePids += $child.Id
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    $window = $null
    do {
        if (-not $request.process_id) {
            $script:CandidatePids = @($script:CandidatePids + @(Get-Process -Name $processName -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) | Select-Object -Unique)
        }
        $windows = @(Get-AppWindows $script:CandidatePids)
        if ($request.window_id) {
            $windows = @($windows | Where-Object { [string]$_.handle -eq [string]$request.window_id -and $_.visible })
            if ($windows.Count -ne 1) { throw 'Selected window is unavailable or not an eligible window of the bound process; diagnose again.' }
        }
        if ($newWindow) { $windows = @($windows | Where-Object { $_.handle -notin @($before | ForEach-Object { $_.handle }) }) }
        $window = $windows | Sort-Object @{Expression={ $_.visible };Descending=$true}, @{Expression={ $_.title_match };Descending=$true}, @{Expression={ $_.area };Descending=$true} | Select-Object -First 1
        if ($window) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $window) {
        # Never launch again just because a running process has no usable window.
        return @{ status='waiting_for_human'; reason='window_unavailable'; launched=$launched;
            instructions='Restore an existing application window manually, then reconnect. No automatic relaunch was attempted.' }
    }
    $handle = [IntPtr][long]$window.handle
    $restored = -not $window.visible -or [ArgusNative]::IsIconic($handle)
    if ($restored) { [void][ArgusNative]::ShowWindow($handle, 4); Start-Sleep -Milliseconds 200 }
    $script:Hwnd = $handle
    $script:PrimaryWindow = $handle
    $script:TargetProcessId = [int]$window.process_id
    return @{ status='connected'; process_id=$window.process_id; process_name=(Get-Process -Id $window.process_id).ProcessName;
        window_handle=$window.handle; title=$window.title; launched=$launched; restored=$restored;
        new_window=$newWindow; launch=$launch }
}

function Get-AppWindows($processIds, [bool]$includeExcluded = $false) {
    $script:AppWindowRows = [Collections.Generic.List[object]]::new()
    $script:AppWindowPids = @($processIds)
    $script:IncludeExcludedWindows = $includeExcluded
    $callback = [ArgusNative+EnumWindowsProc] {
        param([IntPtr]$hwnd, [IntPtr]$unused)
        [uint32]$ownerId = 0
        [void][ArgusNative]::GetWindowThreadProcessId($hwnd, [ref]$ownerId)
        if ($ownerId -notin $script:AppWindowPids) { return $true }
        $reasons = @()
        $style = [ArgusNative]::GetWindowLong($hwnd,-20)
        if (($style -band 0x08000080) -ne 0) { $reasons += 'tool_or_noactivate' }
        if (($style -band 0x20) -ne 0) { $reasons += 'transparent_overlay' }
        if (-not [ArgusNative]::IsWindowVisible($hwnd) -and -not [ArgusNative]::IsIconic($hwnd)) {
            $reasons += 'hidden_non_minimized'
        }
        $title = Get-WindowTitle $hwnd
        if (-not $title) { $reasons += 'empty_title' }
        if (Test-Cloaked $hwnd) { $reasons += 'cloaked' }
        $rect = [ArgusNative+RECT]::new()
        if (-not [ArgusNative]::GetWindowRect($hwnd,[ref]$rect)) { return $true }
        # GDI+/GPU helpers can have a matching application title but only a
        # 1x1 surface. Never restore or bind these as interactive windows.
        if (($rect.Right-$rect.Left) -le 1 -or ($rect.Bottom-$rect.Top) -le 1) { $reasons += 'tiny_surface' }
        if ($reasons.Count -and -not $script:IncludeExcludedWindows) { return $true }
        $area = [long]($rect.Right-$rect.Left)*($rect.Bottom-$rect.Top)
        $script:AppWindowRows.Add(@{handle=$hwnd.ToInt64(); process_id=$ownerId; title=$title;
            class_name=(Get-WindowClass $hwnd); excluded_reasons=$reasons;
            bounds=@($rect.Left,$rect.Top,($rect.Right-$rect.Left),($rect.Bottom-$rect.Top));
            owner=[ArgusNative]::GetWindow($hwnd,4).ToInt64();
            visible=[ArgusNative]::IsWindowVisible($hwnd); area=$area;
            title_match=($title.IndexOf($script:PreferredTitle,[StringComparison]::OrdinalIgnoreCase) -ge 0)})
        return $true
    }
    [void][ArgusNative]::EnumWindows($callback,[IntPtr]::Zero)
    return $script:AppWindowRows.ToArray()
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
                # Main window or an owned dialog only; a process may also own
                # tray/notification/helper windows with unrelated captions.
                $owner = $hwnd
                for ($depth=0; $depth -lt 16 -and $owner -ne [IntPtr]::Zero -and $owner -ne $script:PrimaryWindow; $depth++) {
                    $owner = [ArgusNative]::GetWindow($owner,4)
                }
                if ($owner -ne $script:PrimaryWindow) { return $true }
                $script:enumBest = $hwnd
                return $false
            }
            return $true
        }
        [void][ArgusNative]::EnumWindows($callback, [IntPtr]::Zero)
        return $script:enumBest
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
    if ($script:ExpectedWindow) {
        $expected = $script:ExpectedWindow
        $actualProcess = [uint32]0
        [void][ArgusNative]::GetWindowThreadProcessId($hwnd, [ref]$actualProcess)
        $bounds = @($rect.Left, $rect.Top, ($rect.Right-$rect.Left), ($rect.Bottom-$rect.Top))
        if ([string]$expected.window_id -ne [string]$hwnd.ToInt64() -or
            [int]$expected.process_id -ne [int]$actualProcess -or
            (($expected.window_bounds -join ',') -ne ($bounds -join ','))) {
            throw 'Observed window identity or bounds changed; observe again before input.'
        }
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

function Require-Foreground {
    if (-not (Refresh-TargetWindow)) { throw 'Target window is unavailable.' }
    if ([ArgusNative]::GetForegroundWindow() -ne $script:Hwnd) {
        [void][ArgusNative]::ShowWindow($script:Hwnd,9)
        [void][ArgusNative]::SetForegroundWindow($script:Hwnd)
        Start-Sleep -Milliseconds 150
    }
    Assert-Foreground
}

function Assert-Foreground {
    if ([ArgusNative]::GetForegroundWindow() -ne $script:Hwnd) {
        throw 'Target window is not foreground; no global input was sent.'
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
        throw 'Background capture unavailable: PrintWindow failed or returned a uniform image. Screen capture fallback is disabled.'
    } finally {
        [void][ArgusNative]::SelectObject($mem, $old)
        [void][ArgusNative]::DeleteObject($hbm)
        [void][ArgusNative]::DeleteDC($mem)
        [void][ArgusNative]::ReleaseDC($hwnd, $hdc)
    }
}

function Get-InputTarget {
    $target = $script:FocusChild
    if ($target -ne [IntPtr]::Zero -and [ArgusNative]::IsWindow($target) -and
        ($target -eq $script:Hwnd -or [ArgusNative]::IsChild($script:Hwnd,$target))) { return $target }
    throw 'No target input focus; tap the intended field first.'
}

function Make-LParam([int]$x, [int]$y) {
    return [IntPtr]((([int64]($y -band 0xFFFF)) -shl 16) -bor ([int64]($x -band 0xFFFF)))
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
        $script:ExpectedWindow = $request.expected_window
        if ($script:ExpectedWindow -and -not (Refresh-TargetWindow)) {
            throw 'Observed window disappeared; refusing input.'
        }
        switch ($request.command) {
            "diagnose" {
                Write-Response @{id=$request.id; ok=$true; data=@{
                    platform='windows'; process_id=$script:TargetProcessId;
                    selected_window=[string]$script:Hwnd.ToInt64(); primary_window=[string]$script:PrimaryWindow.ToInt64();
                    foreground_window=[string][ArgusNative]::GetForegroundWindow().ToInt64();
                    capture_mode=$(if ($script:Foreground) {'foreground'} else {'background'});
                    windows=@(Get-AppWindows @($script:TargetProcessId) $true);
                    recovery_actions=@('reobserve','select_window');
                    foreground_requires_explicit_authorization=$true;
                    note='Window facts do not establish capture support or business success. Reobserve after recovery; never replay uncertain input.'
                }}
            }
            "setup" {
                $script:Foreground = [bool]$request.foreground
                $script:App = [string]$request.app
                $script:Launch = [string]$request.launch
                if ([string]::IsNullOrWhiteSpace($script:App)) { throw "WIN_APP is required." }
                $connection = Resolve-DesktopApp $request
                if ($connection.status -eq 'waiting_for_human') {
                    Write-Response @{id=$request.id; ok=$true; data=@{connection=$connection}}
                    break
                }
                if (-not (Refresh-TargetWindow)) { throw "Target window could not be restored." }
                $binding = $request.input_binding
                if ($binding -and [long]$binding.window -eq $script:Hwnd.ToInt64() -and [int]$binding.process_id -eq $script:TargetProcessId) {
                    $target = [IntPtr][long]$binding.target
                    if ([ArgusNative]::IsWindow($target) -and ($target -eq $script:Hwnd -or [ArgusNative]::IsChild($script:Hwnd,$target)) -and
                        (Get-WindowClass $target) -eq $binding.class_name) { $script:FocusChild = $target }
                }
                Write-Response @{ id=$request.id; ok=$true; data=@{
                    width=$script:Rect.Right-$script:Rect.Left
                    height=$script:Rect.Bottom-$script:Rect.Top
                    title=(Get-WindowTitle $script:Hwnd)
                    desktop_path=[Environment]::GetFolderPath('Desktop')
                    connection=$connection
                }}
            }
            "screenshot" {
                if (-not (Refresh-TargetWindow)) { throw 'Target window is unavailable; refusing to capture another application.' }
                if ($script:Foreground) {
                    Require-Foreground
                    $bitmap = Capture-Screen $script:Rect.Left $script:Rect.Top ($script:Rect.Right-$script:Rect.Left) ($script:Rect.Bottom-$script:Rect.Top)
                    Assert-Foreground
                } else { $bitmap = Capture-Window $script:Hwnd $script:Rect }
                $stream = [IO.MemoryStream]::new()
                try {
                    $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
                    Write-Response @{ id=$request.id; ok=$true; data=@{
                        png=[Convert]::ToBase64String($stream.ToArray())
                        width=$bitmap.Width; height=$bitmap.Height
                        window_id=[string]$script:Hwnd.ToInt64(); process_id=$script:TargetProcessId
                        window_bounds=@($script:Rect.Left,$script:Rect.Top,($script:Rect.Right-$script:Rect.Left),($script:Rect.Bottom-$script:Rect.Top))
                    }}
                } finally {
                    $stream.Dispose()
                    $bitmap.Dispose()
                }
            }
            "tap" {
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                if ($script:Foreground) {
                    Require-Foreground
                    if ($request.x -lt 0 -or $request.y -lt 0 -or $request.x -ge ($script:Rect.Right-$script:Rect.Left) -or $request.y -ge ($script:Rect.Bottom-$script:Rect.Top)) {
                        throw 'Tap is outside the target window.'
                    }
                    $point = [ArgusNative+POINT]::new(($script:Rect.Left+[int]$request.x),($script:Rect.Top+[int]$request.y))
                    $hitWindow = [ArgusNative]::WindowFromPoint($point)
                    if ($hitWindow -ne $script:Hwnd -and -not [ArgusNative]::IsChild($script:Hwnd,$hitWindow)) { throw 'Target point is covered by another window.' }
                    Assert-Foreground
                    [void][ArgusNative]::SetCursorPos(($script:Rect.Left+[int]$request.x),($script:Rect.Top+[int]$request.y))
                    [ArgusNative]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
                    [ArgusNative]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
                    Start-Sleep -Milliseconds 100
                    Write-Response @{id=$request.id;ok=$true}
                    break
                }
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
                    if ($cls -match '^(Edit$|WindowsForms\d+\.EDIT\.)') {
                        # Mouse-down makes Edit call SetFocus and activate its parent.
                        # Place the caret by the requested coordinate without OS focus.
                        $pos = [long][ArgusNative]::SendMessageW($hit.Hwnd, 0x00D7, [UIntPtr]::Zero, $lp)
                        $index = $pos -band 0xFFFF
                        [void][ArgusNative]::SendMessageW($hit.Hwnd, 0x00B1, [UIntPtr]([uint64]$index), [IntPtr]$index)
                    } elseif ($cls -match '^(Button$|WindowsForms\d+\.BUTTON\.)') {
                        # The handle comes from target-local hit testing, never FromPoint.
                        # WinForms button mouse-up checks the physical cursor position.
                        $iid = [Guid]"618736E0-3C3D-11CF-810C-00AA00389B71"
                        $acc = $null
                        if ([ArgusNative]::AccessibleObjectFromWindow($hit.Hwnd, [uint32]4294967292, [ref]$iid, [ref]$acc) -ne 0) {
                            throw 'Background button invocation unavailable.'
                        }
                        $acc.accDoDefaultAction(0)
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
                $wp = [UIntPtr]([uint64]((([int64]([int64]$delta)) -band 0xFFFF) -shl 16))
                [void][ArgusNative]::PostMessageW($hit.Hwnd, $script:WM_MOUSEWHEEL, $wp, (Make-LParam ($script:Rect.Left + $cx) ($script:Rect.Top + $cy)))
                Write-Response @{ id=$request.id; ok=$true }
            }
            "input" {
                if (-not (Refresh-TargetWindow)) { throw "Target window '$($script:App)' was not found." }
                if ($script:Foreground) {
                    Require-Foreground
                    foreach ($ch in [char[]][string]$request.text) {
                        Assert-Foreground
                        [ArgusNative]::Key([uint16]$ch,$false,$true)
                        [ArgusNative]::Key([uint16]$ch,$true,$true)
                    }
                    Write-Response @{id=$request.id;ok=$true}
                    break
                }
                $target = Get-InputTarget
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
                if ($script:Foreground) {
                    if (-not $vkTable.ContainsKey($mainName) -and $mainName -notmatch '^[a-z0-9]$') { throw "Unsupported key '$name'." }
                    $mainVk = if ($vkTable.ContainsKey($mainName)) { $vkTable[$mainName] } else { [int][char]::ToUpperInvariant($mainName[0]) }
                    Require-Foreground
                    if ($modNames.Count -eq 1 -and $modNames[0] -in @('ctrl','control') -and $mainName -eq 'a') {
                        [Windows.Forms.SendKeys]::SendWait('^a')
                        Write-Response @{id=$request.id;ok=$true}
                        break
                    }
                    $pressed = @()
                    try {
                        foreach ($m in $modNames) {
                            Assert-Foreground
                            [ArgusNative]::Key($modTable[$m],$false,$false)
                            $pressed += $modTable[$m]
                        }
                        Assert-Foreground
                        [ArgusNative]::Key($mainVk,$false,$false)
                        [ArgusNative]::Key($mainVk,$true,$false)
                    } finally {
                        [array]::Reverse($pressed)
                        foreach ($vk in $pressed) { [ArgusNative]::Key($vk,$true,$false) }
                    }
                    Write-Response @{id=$request.id;ok=$true}
                    break
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
                    $keyTarget = $script:Hwnd
                    if ($modNames.Count -eq 1 -and $modNames[0] -eq "ctrl" -and $mainName -eq "a") {
                        # Edit detects Ctrl+A through GetKeyState, which posted keys
                        # never update; EM_SETSEL selects all without key state.
                        # Prefer the Edit the last tap landed on (a dialog has
                        # several: search box, address bar, file name...).
                        $keyTarget = Get-InputTarget
                        if ((Get-WindowClass $keyTarget) -notmatch '^(Edit$|RichEdit|WindowsForms\d+\.(EDIT|RichEdit))') { throw 'Background select-all requires an Edit or RichEdit control.' }
                        [void][ArgusNative]::PostMessageW($keyTarget, 0x00B1, [UIntPtr]::Zero, [IntPtr](-1))
                        Write-Response @{ id=$request.id; ok=$true }
                        break
                    }
                    throw "Background shortcut '$name' is unsupported; use a visual click or a supported control operation."
                } elseif ($vkTable.ContainsKey($mainName)) {
                    $target = Get-InputTarget
                    Post-Key $target $vkTable[$mainName] $false $false
                    Post-Key $target $vkTable[$mainName] $true $false
                } elseif ($mainName.Length -eq 1) {
                    $target = Get-InputTarget
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
