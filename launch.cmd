@echo off
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "TEMP=E:\robot\temp"
set "TMP=E:\robot\temp"
start "" "E:\robot\envs\so101_ui\Scripts\pythonw.exe" -s -B -X utf8 "E:\robot\projects\so101_data_platform\desktop.py" --offline %*
