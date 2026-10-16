param(
 [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$Nonce
)
$ErrorActionPreference='Stop'
$platformRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $platformRoot 'scripts\ui-environment.ps1')
$receiptDir=Join-Path $platformRoot 'state\shortcut_receipts'
$receiptPath=Join-Path $receiptDir ($Nonce+'.json')
if(Test-Path -LiteralPath $receiptPath){throw 'Receipt exists; use a fresh nonce'}
New-Item -ItemType Directory -Path $receiptDir -Force | Out-Null
try{
 $self=Get-CimInstance Win32_Process -Filter ('ProcessId='+$PID)
 $parent=Get-CimInstance Win32_Process -Filter ('ProcessId='+$self.ParentProcessId)
 if($parent.Name -ne 'explorer.exe'){throw 'Install through the actual Explorer desktop, not an alternate profile'}
 $label='SO101 Data Platform (Portable)'
 $formal=Join-Path $platformRoot 'launch.cmd'
 $launcher=Join-Path $env:WINDIR 'System32\cmd.exe'
 $launcherArguments='/d /c call "'+$formal+'"'
 $icon=Join-Path $platformRoot 'assets\so101.ico'
 if(!(Test-Path -LiteralPath $formal -PathType Leaf)){throw 'Canonical launcher is missing'}
 if(!(Test-Path -LiteralPath $icon -PathType Leaf)){throw 'Application icon is missing'}
 $desktopPath=[Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
 if(!$desktopPath -or !(Test-Path -LiteralPath $desktopPath -PathType Container)){throw 'The real Desktop folder is unavailable'}
 $programsPath=[Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
 if(!$programsPath -or !(Test-Path -LiteralPath $programsPath -PathType Container)){throw 'The real Start Menu Programs folder is unavailable'}
 $destinations=@(
  @{kind='desktop';path=(Join-Path $desktopPath ($label+'.lnk'))},
  @{kind='start_menu';path=(Join-Path $programsPath ($label+'.lnk'))}
 )
 $wsh=New-Object -ComObject WScript.Shell
 $shortcuts=@()
 foreach($destination in $destinations){
  $shortcutPath=$destination.path
  $backup=$null
  if(Test-Path -LiteralPath $shortcutPath){
   $old=$wsh.CreateShortcut($shortcutPath)
   if(![string]::Equals($old.TargetPath,$launcher,[StringComparison]::OrdinalIgnoreCase) -or $old.Arguments -ne $launcherArguments){
    throw 'A shortcut with this name points elsewhere; refusing to overwrite it'
   }
  }
  $link=$wsh.CreateShortcut($shortcutPath)
  $link.TargetPath=$launcher
  $link.Arguments=$launcherArguments
  $link.WorkingDirectory=$platformRoot
  $link.IconLocation=$icon+',0'
  $link.Description='SO101 Data Platform - portable project launcher, opens offline'
  $link.WindowStyle=7
  $link.Save()
  $saved=$wsh.CreateShortcut($shortcutPath)
  if(![string]::Equals($saved.TargetPath,$launcher,[StringComparison]::OrdinalIgnoreCase) -or $saved.Arguments -ne $launcherArguments){throw 'Saved shortcut does not call the canonical entry'}
  if(![string]::Equals($saved.IconLocation,($icon+',0'),[StringComparison]::OrdinalIgnoreCase)){throw 'Saved shortcut icon mismatch'}
  if(![string]::Equals($saved.WorkingDirectory,$platformRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Saved shortcut working directory mismatch'}
  $shortcuts+=@{kind=$destination.kind;shortcut=$shortcutPath;target=$saved.TargetPath;arguments=$saved.Arguments;
   working_directory=$saved.WorkingDirectory;icon=$saved.IconLocation;
   bytes=(Get-Item -LiteralPath $shortcutPath).Length;previous_backup=$backup}
 }
 $identityJson=& (Get-So101UiPython $platformRoot) -s -B -X utf8 (Join-Path $platformRoot 'app_identity.py')
 if($LASTEXITCODE -ne 0){throw 'Cannot verify the canonical build'}
 $identity=$identityJson|ConvertFrom-Json
 $record=[ordered]@{
  ok=$true;nonce=$Nonce;created=[DateTimeOffset]::Now.ToString('o');
  desktop_parent_name=$parent.Name;desktop_parent_pid=$parent.ProcessId;desktop=$desktopPath;
  programs=$programsPath;shortcuts=$shortcuts;formal_entry=$formal;
  target_sha256=(Get-FileHash -LiteralPath $formal -Algorithm SHA256).Hash;
  icon_sha256=(Get-FileHash -LiteralPath $icon -Algorithm SHA256).Hash;
  build=$identity.build;project=$identity.project;
  hardware_accessed=$false;application_started=$false;default_offline=$true
 }
 [IO.File]::WriteAllText($receiptPath,($record|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
}catch{
 [IO.File]::WriteAllText($receiptPath,(@{ok=$false;nonce=$Nonce;error=$_.Exception.Message}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
 throw
}
