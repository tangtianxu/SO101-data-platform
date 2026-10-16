param([switch]$SmokeTest,[switch]$CameraUiTest)
$ErrorActionPreference='Stop'
$platformRoot='E:\robot\projects\so101_data_platform'
$nonce=[Guid]::NewGuid().ToString('N')
$task=Join-Path $platformRoot 'scripts\desktop-launch.ps1'
$result=Join-Path $platformRoot ('state\launch_receipts\'+$nonce+'.json')
if(!(Test-Path -LiteralPath $task)){throw 'Desktop launcher is missing'}
$location=0;$root=0;$desktopHwnd=0
$shell=New-Object -ComObject Shell.Application
$desktop=$shell.Windows().FindWindowSW([ref]$location,[ref]$root,8,[ref]$desktopHwnd,1)
if($null -eq $desktop.Document.Application){throw 'The actual Explorer desktop shell is unavailable; no alternate launcher was used'}
$arguments='-NoProfile -NonInteractive -WindowStyle Hidden -File "'+$task+'" -Nonce '+$nonce
if($SmokeTest){$arguments+=' -SmokeTest'}
if($CameraUiTest){$arguments+=' -CameraUiTest'}
$desktop.Document.Application.ShellExecute((Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'),$arguments,$platformRoot,'open',0)
$deadline=[DateTime]::UtcNow.AddSeconds(30)
while(!(Test-Path -LiteralPath $result)){
 if([DateTime]::UtcNow -ge $deadline){throw ('No fresh desktop receipt yet. Inspect before retrying: '+$result)}
 Start-Sleep -Milliseconds 150
}
$receipt=Get-Content -LiteralPath $result -Raw -Encoding UTF8|ConvertFrom-Json
if(!$receipt.ok){throw $receipt.error}
$receipt
