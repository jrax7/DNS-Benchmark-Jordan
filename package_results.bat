@echo off
setlocal
cd /d "%~dp0"

echo Packaging timestamped DNS result folders...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$stamp=Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'; $dest='dns_results_to_send_'+$stamp+'.zip'; $items=Get-ChildItem -Directory | Where-Object { $_.Name -like 'clean_dns_*' -or $_.Name -like 'sweep_qps_*' }; if (-not $items) { Write-Host 'No result folders found.'; exit 1 }; Compress-Archive -Force -Path ($items.FullName) -DestinationPath $dest; Write-Host 'Created' $dest"

echo.
echo Done.
pause
