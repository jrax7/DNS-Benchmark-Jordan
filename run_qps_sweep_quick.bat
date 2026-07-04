@echo off
setlocal
cd /d "%~dp0"

echo Running quick QPS sweep: 3, 5, 7, 10, 15, 20 qps
echo Each run creates its own timestamped folder.
echo This is for diagnosing where DNS failures start.
echo.

for %%Q in (3 5 7 10 15 20) do (
  echo ================================
  echo Running quick sweep at %%Q qps
  echo ================================
  py -3 dns_benchmark.py ^
    --quick ^
    --include-current ^
    --save ^
    --out-dir sweep_qps_%%Q ^
    --prefix qps_%%Q ^
    --workers 10 ^
    --qps %%Q ^
    --probe-workers 5 ^
    --probe-qps 3 ^
    --rounds 2 ^
    --uncached 1 ^
    --timeout 2 ^
    --probe-timeout 2
  echo.
)

echo Done. Send the sweep_qps_*_YYYY-MM-DD_HH-MM-SS folders back if you want me to identify the safe QPS threshold.
pause
