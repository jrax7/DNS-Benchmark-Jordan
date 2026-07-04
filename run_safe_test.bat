@echo off
setlocal
cd /d "%~dp0"

echo Running VALIDATED SAFE rate-limited DNS benchmark...
echo Base output folder: clean_dns_safe
echo Actual output folder will include date/time, for example clean_dns_safe_2026-07-03_12-08-44
echo.

py -3 dns_benchmark.py ^
  --include-current ^
  --save ^
  --out-dir clean_dns_safe ^
  --prefix clean_dns ^
  --workers 5 ^
  --qps 5 ^
  --probe-workers 5 ^
  --probe-qps 3 ^
  --rounds 3 ^
  --uncached 2 ^
  --timeout 2 ^
  --probe-timeout 2

echo.
echo Done. Send the newest clean_dns_safe_YYYY-MM-DD_HH-MM-SS folder back for analysis.
pause
