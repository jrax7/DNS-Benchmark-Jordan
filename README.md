# DNS Benchmark Toolkit

A rate-limited DNS resolver benchmark that measures **real** query latency, worst-case latency, failure rate, and consistency — instead of relying on marketing claims ("fastest DNS!") or single-ping tests that don't reflect real browsing behavior.

Originally built to answer a simple local question — *which DNS resolver is actually fastest and most reliable from this network in Amman, Jordan* — it works for any location and network. Point it at your own ISP/router and it benchmarks your local path, not someone else's.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Who it's for](#who-its-for)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Command-line reference](#command-line-reference)
- [Servers tested](#servers-tested)
- [How the metrics are calculated](#how-the-metrics-are-calculated)
- [Output files](#output-files)
- [Repository structure](#repository-structure)
- [Adding your own candidate resolvers](#adding-your-own-candidate-resolvers)
- [Limitations & disclaimer](#limitations--disclaimer)
- [License](#license)

---

## Why this exists

Most "which DNS is fastest" advice online is either outdated, based on a single `ping`, or measured from a data center far from a normal home/office network. None of that tells you what *you'll* actually experience.

Early local testing with this project also surfaced a subtler problem: **hammering every resolver in parallel with no rate limit made unrelated public DNS providers appear to fail together**, while the local ISP/router DNS stayed healthy. That wasn't the resolvers' fault — it was local UDP/53 rate policing (router/CPE/ISP) reacting to the burst of simultaneous queries, producing a false "everything is broken except my ISP" result.

This toolkit exists to fix that specific failure mode and produce trustworthy, reproducible numbers:

- A **strict global QPS limiter** (separate from concurrency) so queries leave the machine at a controlled rate.
- **Timestamped output folders** so repeated runs never overwrite each other, making it easy to compare results across times of day.
- A **composite scoring model** that penalizes silent packet loss heavily, so a resolver that "looks fast" because it drops hard queries doesn't win.

## Who it's for

- Anyone choosing a DNS resolver for daily use and wanting real, local, reproducible numbers instead of anecdotes.
- People troubleshooting flaky DNS behavior on their own network who want to know whether it's the resolver, the ISP path, or local rate limiting.
- Anyone in a region (like Jordan) where local/ISP-adjacent DNS candidates exist but aren't officially documented, and want to evaluate them safely and skeptically alongside known-good public resolvers (Cloudflare, Google, Quad9, etc).
- Not intended for hammering resolvers you don't have permission to load-test at high query rates — see [Limitations](#limitations--disclaimer).

## How it works

### 1. Server list loading

`dns_benchmark.py` imports `get_servers()` and `CATEGORY_INFO` from `dns_servers.py`, which contains:

- Active public DNS resolvers (Cloudflare, Google, Quad9, AdGuard, OpenDNS, ControlD, Verisign, Neustar, SafeDNS, CleanBrowsing, Yandex)
- Jordan-specific candidate resolvers, sourced from public DNS-monitoring data rather than official ISP documentation
- Archived/dead resolvers, skipped by default and only re-tested with `--include-archived`
- Category metadata (`CATEGORY_INFO`) describing whether each category filters content and whether it's appropriate for daily/work use

If `--include-current` is passed, the script also auto-discovers your current system/router DNS (e.g. `System / ISP DNS (192.168.1.1)`) and includes it as a baseline for comparison. If `--extra <file>` is passed, additional candidate IPs are merged in from that file. Everything is deduplicated by IP afterward so no resolver appears twice under different names.

### 2. Probe phase

Before the full benchmark, each resolver gets a single quick liveness query, governed by `--probe-workers`, `--probe-qps`, and `--probe-timeout`. Resolvers that answer are marked alive and proceed to the full test; resolvers that don't are marked `DEAD` and skipped, so the full run isn't wasted waiting on unreachable IPs. `--probe-qps` matters because even a short probe burst across many servers can look like a DNS flood to a router.

### 3. Full query phase

The full test builds a job queue where each job is `server + domain + test type`, and **interleaves jobs across all live servers** rather than finishing one server before starting the next — so one slow resolver can't block the whole benchmark. This phase is governed by `--workers`, `--qps`, `--rounds`, `--uncached`, and `--timeout`.

The important distinction:
- `--workers` = how many queries may be in flight at once (concurrency).
- `--qps` = how many queries are allowed to *start*, per second, globally, across every resolver combined (rate).

A small worker count can still generate a large number of new UDP/53 flows per second if replies come back quickly — concurrency and rate are not the same thing, which is why this build enforces `--qps` as a real limiter rather than relying on `--workers` alone.

### 4. Rate limiter behavior

The limiter is a **strict spacing limiter**, not a bursty token bucket. `--qps 5` means query starts are spaced roughly every `1 / 5 = 0.2s = 200ms`, globally, across all resolvers combined — not per-resolver.

### 5. Domains tested

| Type | Examples | Purpose |
|---|---|---|
| Normal | `google.com`, `github.com`, `wikipedia.org` | Cache-hot, everyday browsing behavior |
| Regional | `zain.jo`, `orange.jo`, `jo.gov` | Local/regional content behavior |
| Long-tail | `archive.org`, `debian.org`, `pypi.org` | Approximates first-visit / cold-cache latency |
| NXDOMAIN synthetic | `dnsbench-<random>.example.com` | Correctness/liveness check — resolver should answer `NXDOMAIN` promptly |

### 6. Success vs. failure

| Response | Meaning | Counted as success? |
|---|---|---|
| `NOERROR` | Valid answer returned | ✅ |
| `NXDOMAIN` | Correctly reported non-existent domain | ✅ |
| `SERVFAIL` | Resolver failed internally | ❌ |
| `REFUSED` | Resolver refused to answer | ❌ |
| Timeout | No reply in time | ❌ |
| Malformed/short response | Bad response | ❌ |

---

## Installation

Requires **Python 3.8+**.

```bash
git clone <your-repo-url>
cd dns-benchmark-toolkit
pip install -r requirements.txt
```

The script will also attempt to auto-install `rich` and `matplotlib` if missing, but `pip install -r requirements.txt` is the reliable path, especially in constrained/managed Python environments.

## Quick start

### Windows — double-click launchers

| Script | Equivalent to | Use when |
|---|---|---|
| `run_safe_test.bat` | `--qps 5` (see below) | Default recommended first run |
| `run_very_safe_test.bat` | `--qps 3` | The safe test still shows unexplained WAN DNS failures |
| `run_qps_sweep_quick.bat` | Sweeps `--qps 3 5 7 10 15 20` | You want to find exactly where your router starts dropping DNS under load |
| `package_results.bat` | Zips all `clean_dns_*` / `sweep_qps_*` folders | You're ready to share/archive results |

### Manual / cross-platform (Linux, macOS, or Windows without the launchers)

**Recommended safe full run:**
```bash
python dns_benchmark.py --include-current --save --out-dir clean_dns_safe --prefix clean_dns \
  --workers 5 --qps 5 --probe-workers 5 --probe-qps 3 --rounds 3 --uncached 2 --timeout 2 --probe-timeout 2
```

**Very safe / conservative run:**
```bash
python dns_benchmark.py --include-current --save --out-dir clean_dns_very_safe --prefix clean_dns \
  --workers 3 --qps 3 --probe-workers 3 --probe-qps 2 --rounds 3 --uncached 2 --timeout 2 --probe-timeout 2
```

**Higher-rate comparison run** (only trust this if it doesn't trigger synchronized public-DNS failures):
```bash
python dns_benchmark.py --include-current --save --out-dir clean_dns_qps10 --prefix clean_dns \
  --workers 10 --qps 10 --probe-workers 10 --probe-qps 5 --rounds 3 --uncached 1 --timeout 2 --probe-timeout 2
```

**Disable timestamped folders intentionally** (only if you want to overwrite/reuse the same output folder):
```bash
python dns_benchmark.py --include-current --save --out-dir clean_dns_fixed --no-timestamp-output
```

**Recommended order of operations:**
1. Run the safe test.
2. If many public resolvers fail together, drop to the very-safe test.
3. If you want to pinpoint the exact failure threshold, run the QPS sweep.
4. Package results and share/archive as needed.

---

## Command-line reference

| Flag | Default | Purpose |
|---|---:|---|
| `--rounds N` | `3` | Query rounds per domain per server |
| `--repeat N` | `1` | Repeat the full query phase N times and merge results (steadier averages) |
| `--workers N` | `10` | In-flight query concurrency — **not** rate |
| `--qps N` | `10` | Strict global outbound query-rate cap; use `5` or lower for safe local testing |
| `--probe-workers N` | `10` | Probe-phase concurrency |
| `--probe-qps N` | `5` | Probe-phase query-rate cap |
| `--timeout SEC` | `2.0` | Full-test per-query timeout |
| `--probe-timeout SEC` | `2.0` | Probe timeout |
| `--uncached N` | `2` | Extra long-tail/NXDOMAIN queries per round |
| `--min-success PCT` | `95.0` | Minimum success rate to be marked "recommended" |
| `--include-current` | off | Also test your current system/router DNS |
| `--include-archived` | off | Re-test archived/dead servers from `dns_servers.py` |
| `--extra FILE` | — | Load additional candidate IPs from a text file |
| `--quick` | off | Faster subset test (10 servers, 2 rounds, 8 domains) |
| `--ping` | off | Also measure ICMP ping (doesn't affect the composite score) |
| `--dnssec` | off | Also check DNSSEC validation behavior |
| `--weight-avg` | `0.45` | Composite score weight for average latency |
| `--weight-p95` | `0.20` | Composite score weight for P95 latency |
| `--weight-fail` | `0.20` | Composite score weight for failure rate |
| `--weight-stdev` | `0.15` | Composite score weight for jitter/stdev |
| `--save` | off | Save CSV + TXT report (charts and log always save) |
| `--out-dir NAME` | `.` | Base output folder; timestamp is appended by default |
| `--no-timestamp-output` | off | Disable timestamped output folder |
| `--prefix NAME` | `dns` | Filename prefix for chart images |
| `--top N` | `0` (all) | Show only top N results in the terminal |

Run `python dns_benchmark.py --help` at any time for the authoritative, in-tool version of this list.

---

## Servers tested

Active resolvers live in `dns_servers.py` under `SERVERS`, grouped by category:

| Category | Filters | Daily use | Examples |
|---|---|---|---|
| **Global** | None | Recommended | Cloudflare `1.1.1.1`/`1.0.0.1`, Google `8.8.8.8`/`8.8.4.4`, Quad9 `9.9.9.9`, AdGuard, OpenDNS, ControlD, Verisign, Neustar, SafeDNS |
| **Global Security** | Malware/phishing domains only | Recommended | Cloudflare Malware `1.1.1.2`/`1.0.0.2`, Quad9 ECS/Unsecured variants, CleanBrowsing Security |
| **Global Family** | Adult content + malware | Only if that filtering is wanted | Cloudflare Family `1.1.1.3`/`1.0.0.3`, OpenDNS FamilyShield, AdGuard Family, CleanBrowsing Family |
| **Regional** | Provider-dependent, unverified | Case-by-case | Yandex DNS |
| **Jordan Candidate** | Unknown | Caution — re-test on different days before trusting | JUNet/BAU, JUNet/AHU Gateway, Orange/JT candidates |
| **System / ISP** | Whatever your ISP currently does | Your baseline | Auto-discovered via `--include-current` |

A larger pool of previously-dead or unverified Jordan candidates lives in `ARCHIVED_SERVERS` and is skipped by default to keep normal runs fast — re-test them anytime with `--include-archived`.

> **Jordan Candidate resolvers are not officially documented public services.** They were identified via public DNS-monitoring data, may be ISP-internal-only, rate-limited, or not intended for public use. Treat a fast Jordan Candidate result as *promising, not proven* — confirm it stays green across more than one run, on a different day/time, before using it as your primary resolver.

---

## How the metrics are calculated

| Metric | Formula | What it tells you |
|---|---|---|
| **Avg latency** | `sum(successful latencies) / count(successful)` | Typical reply time. Failed queries are excluded here (see Fail%) — lower is better. |
| **P95 latency** | 95th percentile of successful latencies | The "bad moment" you'll actually notice — 95% of replies were at or under this. A resolver can have a low average but a high P95 if it occasionally stalls. |
| **Jitter (stdev)** | Standard deviation of successful latencies | Consistency. Low = steady and predictable; high = erratic even if the average looks fine. Matters most for calls/gaming. |
| **Fail%** | `failed queries / total queries × 100` | Reliability. A fast resolver with a high failure rate should not be trusted — it may just be dropping the queries that would've been slow. |
| **Composite score** | see below | Lower-is-better single ranking number combining all four signals |

### Composite score formula

```text
score = avg_ms   × weight_avg
      + p95_ms   × weight_p95
      + fail_pct × 8 × weight_fail
      + stdev_ms × weight_stdev
```

Default weights: `avg=0.45`, `p95=0.20`, `fail=0.20`, `stdev=0.15`. The `fail_pct × 8` multiplier makes reliability matter strongly — a fast resolver that silently drops packets should not outrank a slightly slower, stable one.

Tune weights via `--weight-avg`, `--weight-p95`, `--weight-fail`, `--weight-stdev`. For gaming/calls, increase `--weight-stdev` or `--weight-p95`; for pure page-load speed, increase `--weight-avg`.

### Status vs. tier

| Status | Meaning |
|---|---|
| `PASS` | Answered reliably enough |
| `DEGRADED` | Answered, but failed too many queries to trust |
| `FAIL` | Alive at probe time, but failed every full-test query |
| `DEAD` | Never answered the probe — not benchmarked |

| Chart color | Meaning |
|---|---|
| 🟢 Green | Fast and reliable |
| 🟡 Yellow | Acceptable but slower or slightly imperfect |
| 🔴 Red | Poor, unreliable, or too slow |

The daily/work-safe recommendation list excludes Family-filtered DNS by default (since it deliberately blocks adult content and can interfere with normal browsing/work tools) and treats Jordan Candidates as experimental rather than default-recommended, regardless of raw score.

---

## Output files

Each timestamped run folder (e.g. `clean_dns_safe_2026-07-03_12-08-44/`) contains:

| File | Meaning |
|---|---|
| `dns_benchmark_report.txt` | Human-readable ranked report and recommended setups |
| `dns_benchmark_results.csv` | Full table, one resolver per row |
| `dns_timeline.csv` | Per-second success/failure timeline, useful for spotting rate-limit waves |
| `dns_benchmark.log` | Full per-query log (every success and failure) |
| `clean_dns_avg_latency.png` | Average latency chart |
| `clean_dns_p95_latency.png` | P95/worst-case latency chart |
| `clean_dns_composite_score.png` | Daily/work-safe composite ranking chart |
| `clean_dns_ping_latency.png` | Only generated with `--ping` |

None of these are committed to the repository — see `.gitignore` and [Repository structure](#repository-structure) below.

---

## Repository structure

```
.
├── dns_benchmark.py                  # Main benchmark engine
├── dns_servers.py                    # Resolver list + category metadata
├── jordan_dns_candidates_extra.txt   # Optional file for adding more candidate IPs
├── run_safe_test.bat                 # Windows launcher: --qps 5
├── run_very_safe_test.bat            # Windows launcher: --qps 3
├── run_qps_sweep_quick.bat           # Windows launcher: QPS threshold sweep
├── package_results.bat               # Windows launcher: zips result folders
├── requirements.txt                  # rich, matplotlib
├── .gitignore                        # Excludes all generated run output
├── LICENSE
└── README.md
```

Everything above is source/tooling and belongs in version control. Everything a *run* produces — timestamped `clean_dns_*`/`sweep_qps_*` folders, `.zip` packages, loose `.csv`/`.txt`/`.log`/`.png` files — is generated output, already excluded by `.gitignore`, and shouldn't be committed. If you want to showcase example output in the repo (e.g. for this README), copy a small representative set into a separate `examples/` or `docs/` folder rather than committing raw run output at the repo root.

---

## Adding your own candidate resolvers

Add IPs to `jordan_dns_candidates_extra.txt` (or your own file) using any of these formats:

```text
1.2.3.4
1.2.3.4 Provider Name
1.2.3.4 | Provider Name | Jordan Candidate
```

Then run:

```bash
python dns_benchmark.py --extra jordan_dns_candidates_extra.txt --include-current --save --qps 5
```

---

## Limitations & disclaimer

- This tool sends real DNS queries to third-party resolvers. Keep `--qps` conservative (5 or lower is a reasonable default) and avoid running it against resolvers or networks you don't have permission to query at volume.
- Speed varies by time of day and network conditions — treat any single run as a data point, not a verdict. Use `--repeat 2` or `3` (spaced apart), or re-run at a different hour, for a steadier picture.
- "Jordan Candidate" resolvers are unverified, third-party-sourced, and may be ISP-internal or unintended for public use. A single fast/green result is not sufficient grounds to adopt one as a daily resolver.
- This tool collects no telemetry and sends no data anywhere except the DNS queries themselves, to the resolvers you configure it to test.

## License

MIT — see `LICENSE`. (Add a `LICENSE` file with the standard MIT text before publishing if one isn't already present.)
