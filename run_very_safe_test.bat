@echo off
setlocal
cd /d "%~dp0"

echo Running VERY SAFE / conservative DNS benchmark...
echo Base output folder: clean_dns_very_safe
echo Actual output folder will include date/time.
echo.

py -3 dns_benchmark.py ^
  --include-current ^
  --save ^
  --out-dir clean_dns_very_safe ^
  --prefix clean_dns ^
  --workers 3 ^
  --qps 3 ^
  --probe-workers 3 ^
  --probe-qps 2 ^
  --rounds 3 ^
  --uncached 2 ^
  --timeout 2 ^
  --probe-timeout 2

echo.
echo Done. Send the newest clean_dns_very_safe_YYYY-MM-DD_HH-MM-SS folder back for analysis.
pause
