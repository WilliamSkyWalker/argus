# Exercise the actual lifecycle policy with synthetic process/window inventories.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../../saygo/platforms/windows_runner.ps1"
function Assert($condition, $message) { if (-not $condition) { throw $message } }
function Get-Process {
    param($Name, $Id, $ErrorAction)
    if ($Id) { return [pscustomobject]@{Id=$Id; ProcessName='Example'; MainWindowTitle='Example'} }
    if ($script:running) { return [pscustomobject]@{Id=42; ProcessName='Example'; MainWindowTitle='Example'} }
}
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$PassThru)
    $script:starts++
    $script:running=$true
    $script:windowHandle=101
    return [pscustomobject]@{Id=42}
}
function Get-AppWindows($processIds) {
    if ($script:running -and $script:hasWindow) {
        return @(@{handle=$script:windowHandle; process_id=42; title='Example'; visible=$true; area=100; title_match=$true},
            @{handle=999; process_id=42; title='TrayHelper'; visible=$true; area=10000; title_match=$false})
    }
    return @()
}
$request=[pscustomobject]@{app='Example';process_name='Example';launch='example.exe';new_window=$false;new_window_args=@()}
$script:running=$true; $script:hasWindow=$true; $script:windowHandle=100; $script:starts=0
$result=Resolve-DesktopApp $request
Assert ($script:starts -eq 0 -and -not $result.launched -and $result.window_handle -eq 100) 'Existing main window must be reused instead of a larger tray helper'
$script:running=$false
$result=Resolve-DesktopApp $request
Assert ($script:starts -eq 1 -and $result.launched) 'Absent process must launch once'
$script:starts=0; $script:windowHandle=100
$request.new_window=$true; $request.new_window_args=@('--new-window')
$result=Resolve-DesktopApp $request
Assert ($script:starts -eq 1 -and $result.window_handle -eq 101 -and $result.new_window) 'Explicit new window must be verified'
$script:starts=0; $request.new_window_args=@()
$rejected=$false
try { Resolve-DesktopApp $request | Out-Null } catch { $rejected=$true }
Assert ($rejected -and $script:starts -eq 0) 'Unsupported new-window mode must not launch'
$request.new_window=$false; $script:hasWindow=$false
$result=Resolve-DesktopApp $request
Assert ($result.status -eq 'waiting_for_human' -and $script:starts -eq 0) 'Hidden running app must request human help without relaunch'
Assert ((Make-LParam 10 20).ToInt64() -eq 1310730) 'Mouse coordinates must use 16-bit packing'
Write-Output 'Desktop lifecycle policy: 6 cases passed'
