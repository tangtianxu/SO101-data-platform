param(
 [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$Nonce,
 [switch]$SmokeTest,
 [switch]$CameraUiTest
)
$ErrorActionPreference='Stop'
$platformRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'ui-environment.ps1')
$receiptDir=Join-Path $platformRoot 'state\launch_receipts'
$receiptPath=Join-Path $receiptDir ($Nonce+'.json')
if(Test-Path -LiteralPath $receiptPath){throw 'Launch receipt already exists; use a fresh nonce'}
New-Item -ItemType Directory -Path $receiptDir -Force | Out-Null
try{
 $identityJson=& (Get-So101UiPython $platformRoot) -s -B -X utf8 (Join-Path $platformRoot 'app_identity.py')
 if($LASTEXITCODE -ne 0){throw 'Cannot calculate the desktop source build'}
 $identity=$identityJson|ConvertFrom-Json
 $self=Get-CimInstance Win32_Process -Filter ('ProcessId='+$PID)
 $parent=Get-CimInstance Win32_Process -Filter ('ProcessId='+$self.ParentProcessId)
 if($parent.Name -ne 'explorer.exe'){throw 'This verification launch must come from the actual Explorer desktop process'}
 $formal=Join-Path $platformRoot 'launch.cmd'
 $record=[ordered]@{ok=$true;nonce=$Nonce;expected_build=$identity.build;version=$identity.version;project=$identity.project;formal_entry=$formal;
  launcher_pid=$PID;desktop_parent_pid=$parent.ProcessId;desktop_parent_name=$parent.Name;started=[DateTimeOffset]::Now.ToString('o');hardware_accessed=$false;offline=$true}
 # Receipt is committed first so the native window can verify its actual launch chain.
 [IO.File]::WriteAllText($receiptPath,($record|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
 $arguments='/d /c call "'+$formal+'" --launch-nonce '+$Nonce
 if($SmokeTest){$arguments+=' --smoke-test'}
 if($CameraUiTest){$arguments+=' --camera-ui-test'}
 Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList $arguments -WorkingDirectory $platformRoot -WindowStyle Hidden | Out-Null
}catch{
 [IO.File]::WriteAllText($receiptPath,(@{ok=$false;nonce=$Nonce;error=$_.Exception.Message}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
 throw
}
