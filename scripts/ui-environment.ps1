function Get-So101UiPython([string]$PlatformRoot) {
    $candidate=if($env:SO101_UI_PYTHON){$env:SO101_UI_PYTHON}else{Join-Path $PlatformRoot '.venv-ui\Scripts\python.exe'}
    if((Split-Path -Leaf $candidate) -eq 'pythonw.exe'){$candidate=Join-Path (Split-Path -Parent $candidate) 'python.exe'}
    if(!(Test-Path -LiteralPath $candidate -PathType Leaf)){throw 'Run setup-ui.cmd or configure SO101_UI_PYTHON first'}
    return $candidate
}
