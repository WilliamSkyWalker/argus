param([string]$StateFile)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class FixtureNative {
 [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
 [DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
 public struct POINT { public int X,Y; }
}
'@
[void][FixtureNative]::SetProcessDPIAware()
[Windows.Forms.Application]::SetUnhandledExceptionMode([Windows.Forms.UnhandledExceptionMode]::ThrowException)
$form=New-Object Windows.Forms.Form
$form.Text='Argus Background Fixture'
$form.StartPosition='Manual'; $form.Location=New-Object Drawing.Point(120,120)
$form.ClientSize=New-Object Drawing.Size(600,380)
$form.BackColor=[Drawing.Color]::LightBlue
$edit=New-Object Windows.Forms.TextBox
$edit.Location=New-Object Drawing.Point(40,40);$edit.Size=New-Object Drawing.Size(320,30)
$form.Controls.Add($edit)
$other=New-Object Windows.Forms.TextBox
$other.Location=New-Object Drawing.Point(40,90);$other.Size=New-Object Drawing.Size(320,30)
$form.Controls.Add($other)
$button=New-Object Windows.Forms.Button
$button.Text='Increment';$button.Location=New-Object Drawing.Point(40,150);$button.Size=New-Object Drawing.Size(180,40)
$form.Controls.Add($button)
$script:activations=0;$form.Add_Activated({$script:activations++});
$script:clicks=0; $script:point=''
$button.Add_Click({$script:clicks++})
$panel=New-Object Windows.Forms.Panel
$panel.BackColor=[Drawing.Color]::Green
$panel.Location=New-Object Drawing.Point(40,230);$panel.Size=New-Object Drawing.Size(200,80)
$panel.Add_MouseDown({param($sender,$event) $script:point="$($event.X),$($event.Y)"})
$form.Controls.Add($panel)
$cover=New-Object Windows.Forms.Form
$cover.Text='Argus Foreground Cover'
$cover.StartPosition='Manual'; $cover.Location=$form.Location
$cover.Size=New-Object Drawing.Size(680,470);$cover.BackColor=[Drawing.Color]::Red;$cover.TopMost=$true
$cover.Add_FormClosed({$form.Close()})
$timer=New-Object Windows.Forms.Timer
$timer.Interval=100
$timer.Add_Tick({
  if (Test-Path ($StateFile+'.stop')) { $timer.Stop();$cover.Close();return }
  $cursor=[FixtureNative+POINT]::new();[void][FixtureNative]::GetCursorPos([ref]$cursor)
  $ep=$edit.PointToScreen([Drawing.Point]::new(15,12));$op=$other.PointToScreen([Drawing.Point]::new(15,12))
  $bp=$button.PointToScreen([Drawing.Point]::new(80,20));$pp=$panel.PointToScreen([Drawing.Point]::new(37,29))
  $data=@{pid=$PID;target=$form.Handle.ToInt64();cover=$cover.Handle.ToInt64();foreground=[FixtureNative]::GetForegroundWindow().ToInt64();
    activations=$script:activations;cursor=@($cursor.X,$cursor.Y);clipboard=[FixtureNative]::GetClipboardSequenceNumber();first=$edit.Text;second=$other.Text;clicks=$script:clicks;point=$script:point;
    edit=@(( $ep.X - $form.Left ),( $ep.Y - $form.Top ));other=@(( $op.X - $form.Left ),( $op.Y - $form.Top ));button=@(( $bp.X - $form.Left ),( $bp.Y - $form.Top ));panel=@(( $pp.X - $form.Left ),( $pp.Y - $form.Top ))}
  [IO.File]::WriteAllText($StateFile,($data|ConvertTo-Json -Compress))
})
$form.Add_Shown({$cover.Show();$timer.Start()})
[Windows.Forms.Application]::Run($form)
