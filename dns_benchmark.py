#!/usr/bin/env python3
"""
dns_benchmark.py
═══════════════════════════════════════════════════════════════════════
Fast DNS benchmark engine for Amman, Jordan. Reads server list from
dns_servers.py (single combined file — no more separate Jordan file).

WHY THIS IS MUCH FASTER THAN THE OLD SCRIPT:
  Old design: one thread per SERVER, and each thread ran ALL of that
  server's queries (rounds x domains) back-to-back, serially, with a
  sleep between each. A single dead/filtered Jordan candidate could
  burn (rounds x domains x timeout) seconds all by itself, and with
  only --threads 8 workers, ~20 dead servers alone could add 10+
  minutes of pure dead time.

  New design, two phases:
    1. PROBE  — one quick query per server, all servers in parallel
                (default 60 workers). Dead/filtered servers are
                identified in ~1-2 seconds total, not minutes, and
                are skipped for the full test (logged as DEAD).
    2. FULL TEST — every remaining query for every live server is
                flattened into ONE big job queue (not grouped by
                server) and drained by a large thread pool. Slow
                servers no longer block a whole worker thread per
                server; work is interleaved across all servers.

PING:
  Off by default. ICMP ping is unreliable as a DNS-health signal
  (many resolvers firewall ICMP but answer UDP/53 fine — that's
  exactly what was happening with several Jordan candidates last
  run) and it adds real wall-clock time (one blocking subprocess
  call per server). Pass --ping to re-enable it as a *secondary*,
  clearly-labeled metric; it never affects the composite score.

CACHE-MISS TESTING (--uncached):
  Each round adds N extra queries beyond the real domain list, split
  into two different test types:
    "longtail" - real, resolvable-but-unpopular domains (see
                 DOMAINS_LONG_TAIL) that are unlikely to be pre-warmed
                 in any resolver's cache. This is the best proxy for
                 "first visit to a site" latency, i.e. genuine
                 recursive-lookup speed rather than a cache hit.
    "nxdomain" - synthetic guaranteed-nonexistent subdomains. Mainly a
                 liveness/correctness signal (does the resolver answer
                 NXDOMAIN correctly), not a meaningful speed signal,
                 since most resolvers can reject these almost
                 instantly from local knowledge.
  Both count toward avg/p95/fail like any other query.

DNSSEC (--dnssec):
  Optional, off by default (one extra query per server). Checks
  whether each resolver actually validates DNSSEC signatures, using
  the well-known dnssec-failed.org test domain: a validating resolver
  MUST return SERVFAIL for it, a non-validating one will happily
  return an answer since it never checks the signature. Shown as a
  separate column; never affects the composite score.

COMPOSITE SCORE WEIGHTS:
  Lower score = better. Blends average latency, P95 (worst-case)
  latency, failure-rate penalty, and now a consistency/jitter penalty
  (stdev of successful query latency) so a fast-but-erratic resolver
  no longer automatically outranks a slightly slower but rock-steady
  one. All four weights are tunable on the CLI: --weight-avg,
  --weight-p95, --weight-fail, --weight-stdev (defaults: 0.45 / 0.20 /
  0.20 / 0.15). Bias toward pure speed, toward reliability, or toward
  steadiness (e.g. for calls/gaming) without editing source.

NOTE ON --workers AND --qps (read this before raising either):
  --workers controls how many queries may be in flight at the same time.
  It DOES NOT directly control how many DNS queries per second leave your
  PC. Fast replies can let even a modest worker count churn many new UDP
  flows per second through one home router/NAT device.

  --qps is the real global outbound query-rate cap. This build adds a
  strict start-time limiter: each DNS query waits its turn before sending,
  so lowering --qps genuinely lowers sustained UDP/53 rate. Use this to
  avoid router/CPE/ISP DNS-flood or anti-amplification policing.

  If many independent WAN resolvers fail together while the LAN-local
  resolver stays healthy, treat the run as locally rate-limited and lower
  --qps first (try 5-10) before trusting fail%/scores.

OUTPUTS (separate files, not one combined image):
  Each run is saved in its own timestamped output folder by default, e.g.
  clean_dns_safe_2026-07-03_12-08-44, so repeated runs do not overwrite each other.

  dns_avg_latency.png        - bar chart, green/yellow/red by speed
  dns_p95_latency.png        - bar chart, green/yellow/red by speed
  dns_ping_latency.png       - bar chart (only if --ping used)
  dns_composite_score.png    - top recommended servers
  dns_benchmark_results.csv  - full data, one row per server
  dns_timeline.csv           - per-second ok/fail timeline for rate-limit diagnosis
  dns_benchmark_report.txt   - human-readable report
  dns_benchmark.log          - full run log: every query result,
                                every dead-server detection, every
                                exception caught, run health summary

INSTALL:
  pip install rich matplotlib

USAGE:
  python dns_benchmark.py                       # safe full run, no ping, qps-limited
  python dns_benchmark.py --quick --qps 5        # conservative fast subset
  python dns_benchmark.py --qps 15               # raise real query rate carefully
  python dns_benchmark.py --ping                 # also collect ICMP
  python dns_benchmark.py --extra custom_dns.txt # add more servers
  python dns_benchmark.py --rounds 3             # query rounds/domain
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import logging
import os
import platform
import random
import re
import secrets
import socket
import statistics
import struct
import subprocess
import threading
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ── local module: combined server list ──────────────────────────────
from dns_servers import get_servers, CATEGORY_INFO


# ══════════════════════════════════════════════════════════════════════
#  AUTO-INSTALL DEPENDENCIES
# ══════════════════════════════════════════════════════════════════════

def _ensure_deps():
    missing = []
    for pkg, imp in [("rich", "rich"), ("matplotlib", "matplotlib")]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[setup] Installing: {', '.join(missing)} ...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
            stdout=subprocess.DEVNULL,
        )
        print("[setup] Done — restarting script...\n")
        os.execv(sys.executable, [sys.executable] + sys.argv)

_ensure_deps()

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, MofNCompleteColumn
from rich.table import Table

console = Console()


# ══════════════════════════════════════════════════════════════════════
#  LOGGING  (separate from the rich terminal UI)
# ══════════════════════════════════════════════════════════════════════

def setup_logging(path: str) -> logging.Logger:
    logger = logging.getLogger("dnsbench")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(fh)
    return logger


# ══════════════════════════════════════════════════════════════════════
#  DOMAIN TEST POOLS
# ══════════════════════════════════════════════════════════════════════

DOMAINS_GLOBAL = [
    "google.com", "youtube.com", "facebook.com", "instagram.com",
    "twitter.com", "amazon.com", "netflix.com", "cloudflare.com",
    "microsoft.com", "reddit.com", "wikipedia.org", "github.com",
    "linkedin.com", "apple.com", "stackoverflow.com",
]
DOMAINS_REGIONAL = [
    "zain.jo", "orange.jo", "umniah.com", "jo.gov",
    "whatsapp.com", "akamai.net", "fastly.com",
]
ALL_DOMAINS = DOMAINS_GLOBAL + DOMAINS_REGIONAL

# Long-tail but REAL domains for genuine cache-miss testing. Unlike the
# top-15 global sites above (which are hot in virtually every resolver's
# cache and therefore mostly measure "how fast is your cache hit path"),
# these are real, resolvable domains that are unlikely to be pre-warmed
# in a resolver's cache at the moment of the query. That forces an
# actual (or more recent) recursive lookup, which is a much better proxy
# for "how does this resolver behave on a domain I haven't visited yet"
# than a guaranteed-NXDOMAIN synthetic name. Rotated randomly per round
# so no single domain gets hammered into every resolver's cache either.
DOMAINS_LONG_TAIL = [
    "archive.org", "sourceforge.net", "debian.org", "freebsd.org",
    "eff.org", "torproject.org", "openstreetmap.org", "gnu.org",
    "readthedocs.org", "npmjs.com", "pypi.org", "crates.io",
    "hackerone.com", "codeberg.org", "gitea.com", "w3.org",
]

RCODE_NAMES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
QTYPE_A = 1
# EDNS0 OPT pseudo-record type, used for the DNSSEC-OK probe.
QTYPE_OPT = 41
DNSSEC_TEST_DOMAIN = "dnssec-failed.org"  # intentionally has a broken RRSIG


# ══════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class QueryResult:
    provider: str
    ip: str
    domain: str
    test_type: str
    ok: bool
    elapsed_ms: Optional[float]
    rcode: Optional[int]
    error: str
    started_at: float = 0.0
    ended_at: float = 0.0


@dataclass
class ServerStats:
    provider: str
    ip: str
    category: str
    alive: bool                 # passed the probe phase
    status: str                  # "PASS" | "DEGRADED" | "FAIL" | "DEAD" — explicit, unambiguous
    queries_total: int
    queries_ok: int
    avg_ms: Optional[float]
    median_ms: Optional[float]
    min_ms: Optional[float]
    max_ms: Optional[float]
    p95_ms: Optional[float]
    stdev_ms: float
    fail_pct: float
    ping_avg_ms: Optional[float]
    ping_loss_pct: Optional[float]
    ping_reachable: Optional[bool]
    dnssec_validates: Optional[bool]   # None = not tested / inconclusive
    score: float
    recommended: bool
    tier: str                   # "green" | "yellow" | "red"
    filters: str                 # what this category filters, for daily-use guidance
    daily_use: str                # short verdict: Recommended / Caution / etc.
    rcodes: str
    errors: str


# ══════════════════════════════════════════════════════════════════════
#  CORE UTILITIES
# ══════════════════════════════════════════════════════════════════════

def is_ipv4(val: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(val), ipaddress.IPv4Address)
    except ValueError:
        return False


def percentile(vals: List[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


class RateLimiter:
    """Strict global start-time limiter for outbound DNS queries.

    This is intentionally a spacing limiter rather than a bursty token bucket:
    if --qps is 10, query start times are spaced by at least 100ms globally,
    regardless of how many worker threads exist. That makes it useful for
    diagnosing router/CPE/ISP UDP/53 policing because it caps the actual
    sustained query rate, not merely the number of in-flight threads.

    qps <= 0 disables the limiter.
    """

    def __init__(self, qps: float):
        self.qps = float(qps or 0)
        self.interval = 0.0 if self.qps <= 0 else 1.0 / self.qps
        self._lock = threading.Lock()
        self._next_at = time.perf_counter()

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def wait(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = time.perf_counter()
            wait_for = self._next_at - now
            if wait_for > 0:
                time.sleep(wait_for)
                now = time.perf_counter()
            # Schedule the next query start. max() prevents a long pause from
            # building a backlog/burst when workers resume.
            self._next_at = max(self._next_at + self.interval, now + self.interval)


def build_dns_query(domain: str) -> Tuple[int, bytes]:
    txid = secrets.randbits(16)
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(lbl := part.encode("idna"))]) + lbl
        for part in domain.rstrip(".").split(".")
    ) + b"\x00"
    return txid, header + qname + struct.pack("!HH", QTYPE_A, 1)


def build_dnssec_query(domain: str) -> Tuple[int, bytes]:
    """Same as build_dns_query, but with the DO (DNSSEC OK) bit set via an
    EDNS0 OPT pseudo-record in the additional section, so a validating
    resolver has a reason to actually check signatures."""
    txid = secrets.randbits(16)
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 1)  # ARCOUNT=1 for the OPT record
    qname = b"".join(
        bytes([len(lbl := part.encode("idna"))]) + lbl
        for part in domain.rstrip(".").split(".")
    ) + b"\x00"
    question = qname + struct.pack("!HH", QTYPE_A, 1)
    # OPT RR: NAME=root(0), TYPE=41, CLASS=UDP payload size(4096),
    # TTL=extended-rcode(0)+version(0)+DO-bit(0x8000), RDLEN=0
    opt = struct.pack("!BHHIH", 0, QTYPE_OPT, 4096, 0x00008000, 0)
    return txid, header + question + opt


def query_dnssec(ip: str, domain: str, timeout: float) -> Tuple[Optional[bool], Optional[int], str]:
    """Sends a DO-bit query. Returns (ad_flag_set, rcode, error).
    ad_flag_set is None if the query itself failed/timed out."""
    try:
        txid, packet = build_dnssec_query(domain)
    except Exception as e:
        return None, None, f"build_error:{e}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, 53))
            sock.send(packet)
            data = sock.recv(1024)
    except socket.timeout:
        return None, None, "timeout"
    except OSError as e:
        return None, None, f"os:{e}"

    if len(data) < 12:
        return None, None, "short_response"
    resp_txid, flags = struct.unpack("!HH", data[:4])
    if resp_txid != txid:
        return None, None, "txid_mismatch"

    rcode = flags & 0x000F
    ad_flag = bool(flags & 0x0020)
    return ad_flag, rcode, ""


def check_dnssec_validation(ip: str, timeout: float) -> Optional[bool]:
    """Best-effort DNSSEC-validation check using the well-known
    dnssec-failed.org test domain (its RRSIG is intentionally broken).
    A resolver that actually validates DNSSEC MUST return SERVFAIL for
    it; a resolver that doesn't validate will happily return NOERROR
    since it never checks the signature. Returns True/False, or None if
    the probe itself failed (inconclusive, not a "does not validate").
    """
    ad_flag, rcode, error = query_dnssec(ip, DNSSEC_TEST_DOMAIN, timeout)
    if rcode is None:
        return None
    return rcode == 2  # SERVFAIL


def query_dns(ip: str, domain: str, timeout: float) -> Tuple[bool, Optional[float], Optional[int], str]:
    """Raw UDP DNS query, sent directly to ip:53."""
    try:
        txid, packet = build_dns_query(domain)
    except Exception as e:
        return False, None, None, f"build_error:{e}"

    start = time.perf_counter()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, 53))
            sock.send(packet)
            data = sock.recv(512)
    except socket.timeout:
        return False, None, None, "timeout"
    except OSError as e:
        return False, None, None, f"os:{e}"

    elapsed_ms = (time.perf_counter() - start) * 1000

    if len(data) < 12:
        return False, elapsed_ms, None, "short_response"

    resp_txid, flags = struct.unpack("!HH", data[:4])
    if resp_txid != txid:
        return False, elapsed_ms, None, "txid_mismatch"

    rcode = flags & 0x000F
    ok = rcode in (0, 3)  # NOERROR or NXDOMAIN both prove the resolver works
    return ok, elapsed_ms, rcode, ("" if ok else RCODE_NAMES.get(rcode, f"RCODE_{rcode}"))


def ping_host(ip: str, count: int = 3) -> Tuple[Optional[float], float, bool]:
    """Optional ICMP ping (only called if --ping is passed)."""
    try:
        if sys.platform.startswith("win"):
            cmd = ["ping", "-n", str(count), "-w", "1200", ip]
        else:
            cmd = ["ping", "-c", str(count), "-W", "1", ip]
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=count + 4).stdout.decode(errors="replace")

        avg_ms, loss_pct = None, 100.0
        if sys.platform.startswith("win"):
            for line in out.splitlines():
                if "Average" in line:
                    try:
                        avg_ms = float(line.split("=")[-1].replace("ms", "").strip())
                    except ValueError:
                        pass
                if "Lost" in line:
                    try:
                        loss_pct = float(line.split("(")[1].split("%")[0])
                    except Exception:
                        pass
        else:
            for line in out.splitlines():
                if "min/avg/max" in line or ("rtt" in line and "=" in line):
                    try:
                        avg_ms = float(line.split("=")[-1].strip().split("/")[1])
                    except (IndexError, ValueError):
                        pass
                if "packet loss" in line:
                    try:
                        loss_pct = float(line.split("%")[0].split()[-1])
                    except Exception:
                        pass
        return avg_ms, loss_pct, avg_ms is not None
    except Exception:
        return None, 100.0, False


def make_test_domains(base_domains: List[str], uncached_count: int) -> List[Tuple[str, str]]:
    """Build one round's worth of (domain, test_type) queries.

    Three distinct test types, each measuring something different:
      "normal"   - popular domains, likely cache-hot everywhere. Reflects
                   real day-to-day browsing latency.
      "longtail" - real, resolvable domains that are unlikely to be
                   pre-cached. Forces a genuine (or recent) recursive
                   lookup, so this is the best proxy for "first visit to
                   a site" latency. A random domain is picked from
                   DOMAINS_LONG_TAIL each call so no single one gets
                   artificially warmed into every resolver's cache.
      "nxdomain" - synthetic, guaranteed-nonexistent subdomain. This
                   mainly measures how fast/correctly a resolver reports
                   "this doesn't exist" (NXDOMAIN) rather than recursion
                   speed, since most resolvers can answer NXDOMAIN from
                   local knowledge almost immediately. Useful mainly as
                   a basic liveness/correctness signal, not a speed one.
    Half of --uncached slots go to "longtail", half to "nxdomain" (with
    "longtail" getting the extra slot on an odd count, since it's the
    more informative of the two).
    """
    tests = [(d, "normal") for d in base_domains]

    longtail_n = (uncached_count + 1) // 2
    nxdomain_n = uncached_count - longtail_n

    for _ in range(longtail_n):
        tests.append((random.choice(DOMAINS_LONG_TAIL), "longtail"))
    for _ in range(nxdomain_n):
        rand = "dnsbench-" + secrets.token_hex(6)
        tests.append((f"{rand}.example.com", "nxdomain"))

    random.shuffle(tests)
    return tests


def discover_system_dns() -> Dict[str, Dict]:
    found: List[str] = []
    try:
        s = platform.system().lower()
        if "windows" in s:
            ps = ["powershell", "-NoProfile", "-Command",
                  "Get-DnsClientServerAddress -AddressFamily IPv4 | "
                  "Select-Object -ExpandProperty ServerAddresses"]
            out = subprocess.run(ps, capture_output=True, text=True, timeout=6).stdout
            found.extend(out.replace("{", "").replace("}", "").replace(",", "\n").split())
        elif "linux" in s and os.path.exists("/etc/resolv.conf"):
            with open("/etc/resolv.conf", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip().startswith("nameserver"):
                        parts = line.split()
                        if len(parts) >= 2:
                            found.append(parts[1])
        elif "darwin" in s:
            out = subprocess.run(["scutil", "--dns"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if line.strip().startswith("nameserver"):
                    found.append(line.split()[-1])
    except Exception:
        pass

    result = {}
    for tok in found:
        tok = tok.strip()
        if is_ipv4(tok):
            result[f"System / ISP DNS ({tok})"] = {"ip": tok, "category": "System / ISP"}
    return result


def _clean_provider_name(ip: str, name: str, default_prefix: str = "Custom") -> str:
    """Keep provider labels readable, even when an --extra file repeats the IP
    inside the display name, e.g. "87.236.233.70 JUNet ... 87.236.233.70"."""
    name = name.strip()
    if not name:
        return f"{default_prefix} ({ip})"
    # Remove repeated IP at the beginning/end of the name.
    name = re.sub(rf"^\s*{re.escape(ip)}\s+", "", name).strip()
    name = re.sub(rf"\s*\(?{re.escape(ip)}\)?\s*$", "", name).strip()
    return name or f"{default_prefix} ({ip})"


def load_custom_dns(path: str) -> Dict[str, Dict]:
    """Load extra DNS servers. Supported line formats:

      1.1.1.1
      1.1.1.1 Cloudflare Primary
      87.236.233.70 | JUNet/AHU Gateway Candidate | Jordan Candidate

    If the filename contains "jordan", extra entries default to
    Jordan Candidate instead of Custom, because public Jordan resolver
    lists are unverified and should be treated cautiously.
    """
    out = {}
    default_category = "Jordan Candidate" if "jordan" in os.path.basename(path).lower() else "Custom"
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "|" in line:
                    fields = [p.strip() for p in line.split("|")]
                    ip = fields[0]
                    name = fields[1] if len(fields) > 1 and fields[1] else f"Custom ({ip})"
                    category = fields[2] if len(fields) > 2 and fields[2] else default_category
                else:
                    parts = line.split(maxsplit=1)
                    ip = parts[0].strip()
                    name = parts[1].strip() if len(parts) > 1 else f"Custom ({ip})"
                    category = default_category

                if is_ipv4(ip):
                    name = _clean_provider_name(ip, name, default_prefix=category)
                    out[name] = {"ip": ip, "category": category}
    except FileNotFoundError:
        console.print(f"[yellow]Warning: --extra file '{path}' not found.[/yellow]")
    return out


_DEDUP_CATEGORY_RANK = {
    # Prefer named built-in/global entries over ad-hoc Custom/System aliases
    # when the same IP is discovered more than once.
    "Global": 0, "Global Security": 0, "Global Family": 0,
    "Global Filtering": 0, "Regional (JO)": 0, "Regional": 0,
    "Jordan Candidate": 1, "Custom": 2, "System / ISP": 3,
}


def deduplicate_server_pool(pool: Dict[str, Dict]) -> Dict[str, Dict]:
    """De-duplicate by IP after adding --include-current and --extra servers.
    get_servers() already deduplicates the built-in list, but extra/system DNS
    can reintroduce duplicates such as Google Primary and System / ISP DNS
    both pointing to 8.8.8.8.
    """
    by_ip: Dict[str, str] = {}
    out: Dict[str, Dict] = {}
    for name, info in pool.items():
        ip = info.get("ip")
        if not ip or not is_ipv4(ip):
            continue
        category = info.get("category", "Custom")
        info = {**info, "category": category}
        if ip not in by_ip:
            by_ip[ip] = name
            out[name] = info
            continue

        existing = by_ip[ip]
        old_rank = _DEDUP_CATEGORY_RANK.get(out[existing].get("category", "Custom"), 9)
        new_rank = _DEDUP_CATEGORY_RANK.get(category, 9)
        if new_rank < old_rank:
            del out[existing]
            by_ip[ip] = name
            out[name] = info
    return out


# ══════════════════════════════════════════════════════════════════════
#  PHASE 1 — LIVENESS PROBE  (fast, all servers in parallel)
# ══════════════════════════════════════════════════════════════════════

def probe_all(servers: Dict[str, Dict], probe_timeout: float, workers: int, qps: float,
               logger: logging.Logger) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """One quick query per server, run concurrently but rate-limited.

    The probe phase used to fire one query to every resolver almost at once.
    On a CPE/router that polices UDP/53, even this short burst can poison the
    rest of the run. --probe-qps caps the actual probe send rate.
    """
    alive: Dict[str, Dict] = {}
    dead: Dict[str, Dict] = {}
    limiter = RateLimiter(qps)

    def _probe_one(name: str, info: Dict):
        ip = info["ip"]
        limiter.wait()
        ok, elapsed_ms, rcode, error = query_dns(ip, "google.com", probe_timeout)
        return name, info, ok, elapsed_ms, error

    qps_label = f" at <= {qps:g} qps" if qps and qps > 0 else ""
    with console.status(f"[cyan]Probing {len(servers)} servers for liveness{qps_label}...[/cyan]"):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_probe_one, n, i) for n, i in servers.items()]
            for fut in as_completed(futures):
                try:
                    name, info, ok, elapsed_ms, error = fut.result()
                except Exception as e:
                    logger.error(f"PROBE crashed unexpectedly: {e}\n{traceback.format_exc()}")
                    continue
                if ok:
                    alive[name] = info
                    logger.debug(f"PROBE alive   {name:<28} {info['ip']:<16} {elapsed_ms:.1f}ms")
                else:
                    dead[name] = info
                    logger.info(f"PROBE DEAD    {name:<28} {info['ip']:<16} reason={error}")

    console.print(
        f"[green]✓ {len(alive)} servers responded[/green]   "
        f"[red]✗ {len(dead)} servers dead/unreachable (skipped, see log)[/red]"
    )
    return alive, dead


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — FLAT PARALLEL QUERY QUEUE
# ══════════════════════════════════════════════════════════════════════

def build_job_queue(servers: Dict[str, Dict], rounds: int, domains: List[str],
                     uncached: int) -> List[Tuple[str, str, str, str]]:
    """Build a balanced, interleaved job list.

    The previous fully-random flat queue was fast, but a random shuffle can
    occasionally place several queries to the same resolver close together.
    With --qps added, we want the cleanest possible measurement: spread query
    starts across servers first, then across domains/rounds. Slow servers still
    cannot monopolize workers, but no resolver gets hit with an accidental
    mini-burst from the scheduler itself.
    """
    per_server: Dict[str, List[Tuple[str, str, str, str]]] = {}
    for name, info in servers.items():
        server_jobs: List[Tuple[str, str, str, str]] = []
        for _ in range(rounds):
            tests = make_test_domains(domains, uncached)
            for domain, test_type in tests:
                server_jobs.append((name, info["ip"], domain, test_type))
        random.shuffle(server_jobs)
        per_server[name] = server_jobs

    jobs: List[Tuple[str, str, str, str]] = []
    active = [name for name, j in per_server.items() if j]
    while active:
        random.shuffle(active)
        next_active = []
        for name in active:
            if per_server[name]:
                jobs.append(per_server[name].pop())
            if per_server[name]:
                next_active.append(name)
        active = next_active
    return jobs


def run_flat_benchmark(
    servers: Dict[str, Dict],
    rounds: int,
    domains: List[str],
    uncached: int,
    timeout: float,
    workers: int,
    qps: float,
    logger: logging.Logger,
) -> Dict[str, List[QueryResult]]:
    jobs = build_job_queue(servers, rounds, domains, uncached)
    by_server: Dict[str, List[QueryResult]] = {name: [] for name in servers}
    limiter = RateLimiter(qps)

    def _run_one(job):
        name, ip, domain, test_type = job
        limiter.wait()
        started_at = time.time()
        ok, elapsed_ms, rcode, error = query_dns(ip, domain, timeout)
        ended_at = time.time()
        return QueryResult(name, ip, domain, test_type, ok, elapsed_ms, rcode, error, started_at, ended_at)

    crashes = 0
    qps_label = f", qps cap {qps:g}" if qps and qps > 0 else ", no qps cap"
    with Progress(
        SpinnerColumn(), TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30), MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(), console=console, transient=False,
    ) as prog:
        task = prog.add_task(f"Querying ({len(jobs):,} jobs, {workers} workers{qps_label})...", total=len(jobs))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_run_one, job) for job in jobs]
            for fut in as_completed(futures):
                try:
                    qr = fut.result()
                except Exception as e:
                    crashes += 1
                    logger.error(f"QUERY job crashed: {e}\n{traceback.format_exc()}")
                    prog.advance(task)
                    continue
                by_server[qr.provider].append(qr)
                if qr.ok:
                    logger.debug(f"OK    {qr.provider:<28} {qr.ip:<16} {qr.domain:<30} {qr.elapsed_ms:.1f}ms")
                else:
                    logger.debug(f"FAIL  {qr.provider:<28} {qr.ip:<16} {qr.domain:<30} err={qr.error}")
                prog.advance(task)

    if crashes:
        logger.warning(f"{crashes} query jobs crashed with an exception (see above) — "
                        "results for affected servers may undercount queries_total.")
    return by_server


# ══════════════════════════════════════════════════════════════════════
#  AGGREGATION / SCORING
# ══════════════════════════════════════════════════════════════════════

# Composite score weights (lower score = better). Exposed on the CLI via
# --weight-avg / --weight-p95 / --weight-fail / --weight-stdev so users can
# bias the ranking toward raw speed vs. consistency vs. reliability without
# editing source. Don't need to sum to 1 — they're just relative weights
# applied to millisecond-scale components, kept in the same rough magnitude
# as the original hand-tuned 0.55/0.25/0.20 split.
DEFAULT_WEIGHTS = {"avg": 0.45, "p95": 0.20, "fail": 0.20, "stdev": 0.15}


def is_private_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def check_for_local_bottleneck(results: List[ServerStats], logger: logging.Logger) -> bool:
    """Detects the specific signature of local NAT/conntrack exhaustion
    rather than genuine multi-provider DNS unreliability: a majority of
    independent WAN servers failing heavily, while any LAN-local server
    (private IP, e.g. your router via --include-current) stays healthy.
    Independent public providers essentially never fail in lockstep for
    real reasons — if they did, it's virtually always local. Returns
    True (and prints/logs a warning) if the pattern is detected.
    """
    wan = [r for r in results if r.alive and not is_private_ip(r.ip)]
    lan = [r for r in results if r.alive and is_private_ip(r.ip)]
    if len(wan) < 5:
        return False

    high_fail_wan = [r for r in wan if r.fail_pct >= 30]
    frac_high = len(high_fail_wan) / len(wan)
    if frac_high < 0.5:
        return False

    wan_fail_avg = statistics.mean(r.fail_pct for r in wan)
    lan_fail_avg = statistics.mean(r.fail_pct for r in lan) if lan else None

    # If there's no LAN sample to compare against, still warn (a majority
    # of independent WAN providers collapsing together is suspicious on
    # its own), but word it a bit more cautiously.
    if lan_fail_avg is not None and (wan_fail_avg - lan_fail_avg) < 20:
        return False  # LAN struggled about as much too — less clearly local-NAT-specific

    msg_lines = [
        f"[bold yellow]⚠ {len(high_fail_wan)}/{len(wan)} independent WAN servers "
        f"(avg fail {wan_fail_avg:.0f}%) failed heavily this run",
    ]
    if lan_fail_avg is not None:
        msg_lines.append(f"   while LAN-local server(s) stayed healthy (avg fail {lan_fail_avg:.0f}%).[/]")
    else:
        msg_lines.append("   — independent public DNS providers essentially never fail in lockstep for real reasons.[/]")
    msg_lines += [
        "",
        "[yellow]This almost always means local DNS-rate policing / NAT or connection-tracking trouble, not that "
        "Cloudflare, Google, etc. actually broke at once. --workers controls in-flight queries; "
        "--qps controls the real sustained outbound DNS query rate.[/yellow]",
        "",
        "[bold]Fix:[/bold] re-run with a lower [cyan]--qps[/cyan] first (try 5-10). Keep "
        "[cyan]--workers[/cyan] modest (8-12) so timeouts don't stall the test, but do not use "
        "workers as the rate control. Don't trust this run's fail%/scores/recommendations.",
    ]
    console.print(Panel("\n".join(msg_lines), title="[bold red]Likely local network bottleneck detected[/bold red]",
                         border_style="red"))
    logger.warning(f"LOCAL BOTTLENECK SUSPECTED: {len(high_fail_wan)}/{len(wan)} WAN servers "
                    f"fail>=30%% (avg {wan_fail_avg:.1f}%%), LAN avg "
                    f"{'n/a' if lan_fail_avg is None else f'{lan_fail_avg:.1f}%%'} — "
                    "recommend lower --qps and re-run before trusting results.")
    return True


def aggregate_server(provider: str, info: Dict, raw_results: List[QueryResult],
                      alive: bool, ping_avg: Optional[float], ping_loss: Optional[float],
                      ping_ok: Optional[bool], min_success: float,
                      dnssec_validates: Optional[bool] = None,
                      weights: Optional[Dict[str, float]] = None) -> ServerStats:
    ip = info["ip"]
    category = info.get("category", "Unknown")
    cat_info = CATEGORY_INFO.get(category, {"filters": "Unknown", "daily_use": "Unknown"})

    ok_times = [r.elapsed_ms for r in raw_results if r.ok and r.elapsed_ms is not None]
    failures = [r for r in raw_results if not r.ok]
    total = len(raw_results)
    rcode_ctr = Counter(RCODE_NAMES.get(r.rcode, str(r.rcode)) for r in raw_results if r.ok and r.rcode is not None)
    err_ctr = Counter(r.error for r in failures if r.error)

    fail_pct = (len(failures) / total * 100) if total else 100.0
    avg_ms = statistics.mean(ok_times) if ok_times else None
    median_ms = statistics.median(ok_times) if ok_times else None
    p95_ms = percentile(ok_times, 95) if ok_times else None
    min_ms = min(ok_times) if ok_times else None
    max_ms = max(ok_times) if ok_times else None
    stdev_ms = statistics.stdev(ok_times) if len(ok_times) >= 2 else 0.0

    w = weights or DEFAULT_WEIGHTS
    if not alive:
        score = 99999.0
    else:
        dns_component = (avg_ms or 9999) * w["avg"]
        p95_component = (p95_ms or 9999) * w["p95"]
        fail_penalty = fail_pct * 8 * w["fail"]
        # Consistency component: a resolver that's fast on average but
        # jittery (high stdev) is worse for real-time use (calls, gaming)
        # than one with a slightly higher but rock-steady average. Only
        # applied once there's enough data to be meaningful.
        stdev_penalty = (stdev_ms if len(ok_times) >= 2 else 0.0) * w["stdev"]
        score = dns_component + p95_component + fail_penalty + stdev_penalty

    # Tier classification for chart coloring (green/yellow/red).
    # This is computed FIRST, then "recommended" is derived from it,
    # so the table/chart and the recommendation can never disagree
    # (a server marked red can never be flagged "recommended").
    if not alive or avg_ms is None:
        tier = "red"
    elif fail_pct > 10:
        tier = "red"
    elif avg_ms <= 40 and fail_pct <= 2:
        tier = "green"
    elif avg_ms <= 90 and fail_pct <= 5:
        tier = "yellow"
    else:
        tier = "red"

    recommended = alive and tier in ("green", "yellow") and \
        (fail_pct <= (100 - min_success)) and bool(ok_times)

    # Explicit, unambiguous status string (separate from the speed tier)
    if not alive:
        status = "DEAD"            # never answered a single query, even the probe
    elif fail_pct == 0:
        status = "PASS"             # every query succeeded
    elif fail_pct <= (100 - min_success):
        status = "PASS"             # within the acceptable failure threshold
    elif fail_pct < 100:
        status = "DEGRADED"         # answered, but failed enough queries to be unreliable
    else:
        status = "FAIL"             # alive at probe time but failed every query in the full test

    return ServerStats(
        provider=provider, ip=ip, category=category, alive=alive, status=status,
        queries_total=total, queries_ok=len(ok_times),
        avg_ms=avg_ms, median_ms=median_ms, min_ms=min_ms, max_ms=max_ms,
        p95_ms=p95_ms, stdev_ms=stdev_ms, fail_pct=fail_pct,
        ping_avg_ms=ping_avg, ping_loss_pct=ping_loss, ping_reachable=ping_ok,
        dnssec_validates=dnssec_validates,
        score=round(score, 2), recommended=recommended, tier=tier,
        filters=cat_info.get("filters", "Unknown"), daily_use=cat_info.get("daily_use", "Unknown"),
        rcodes=";".join(f"{k}:{v}" for k, v in rcode_ctr.items()) or "-",
        errors=";".join(f"{k}:{v}" for k, v in err_ctr.items()) or "-",
    )


def run_pings(servers: Dict[str, Dict], workers: int, logger: logging.Logger) -> Dict[str, Tuple]:
    """Run ICMP pings concurrently (only when --ping is passed)."""
    out: Dict[str, Tuple] = {}

    def _ping_one(name, info):
        avg, loss, ok = ping_host(info["ip"])
        return name, avg, loss, ok

    with console.status(f"[cyan]Pinging {len(servers)} servers (optional, --ping)...[/cyan]"):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_ping_one, n, i) for n, i in servers.items()]
            for fut in as_completed(futures):
                try:
                    name, avg, loss, ok = fut.result()
                except Exception as e:
                    logger.error(f"PING crashed: {e}")
                    continue
                out[name] = (avg, loss, ok)
                logger.debug(f"PING  {name:<28} avg={avg} loss={loss}% reachable={ok}")
    return out


def run_dnssec_checks(servers: Dict[str, Dict], timeout: float, workers: int,
                       logger: logging.Logger) -> Dict[str, Optional[bool]]:
    """Optional (--dnssec) one-shot check per server: does it validate
    DNSSEC signatures? See check_dnssec_validation() docstring."""
    out: Dict[str, Optional[bool]] = {}

    def _check_one(name, info):
        return name, check_dnssec_validation(info["ip"], timeout)

    with console.status(f"[cyan]Checking DNSSEC validation on {len(servers)} servers (optional, --dnssec)...[/cyan]"):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_check_one, n, i) for n, i in servers.items()]
            for fut in as_completed(futures):
                try:
                    name, validates = fut.result()
                except Exception as e:
                    logger.error(f"DNSSEC check crashed: {e}")
                    continue
                out[name] = validates
                logger.debug(f"DNSSEC {name:<28} validates={validates}")
    return out


# ══════════════════════════════════════════════════════════════════════
#  TERMINAL OUTPUT
# ══════════════════════════════════════════════════════════════════════

TIER_COLOR = {"green": "bold green", "yellow": "bold yellow3", "red": "bold red"}
TIER_DOT = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
STATUS_STYLE = {"PASS": "[bold green]PASS[/]", "DEGRADED": "[bold yellow3]DEGRADED[/]",
                 "FAIL": "[bold red]FAIL[/]", "DEAD": "[dim red]DEAD[/]"}

DAILY_SAFE_CATEGORIES = {"Global", "Global Security", "Regional (JO)"}
FAMILY_CATEGORIES = {"Global Family"}


def daily_safe_results(results: List[ServerStats]) -> List[ServerStats]:
    """Recommended servers that are appropriate for normal daily/work use.
    This deliberately excludes Family DNS unless the user wants adult-content
    filtering, even if Family resolvers are very fast.
    """
    return [r for r in results if r.recommended and r.category in DAILY_SAFE_CATEGORIES]


def family_results(results: List[ServerStats]) -> List[ServerStats]:
    return [r for r in results if r.recommended and r.category in FAMILY_CATEGORIES]


def jordan_candidate_results(results: List[ServerStats]) -> List[ServerStats]:
    return [r for r in results if r.recommended and r.category == "Jordan Candidate"]


def print_setup_lines(results: List[ServerStats]) -> None:
    safe = daily_safe_results(results)
    family = family_results(results)
    jo = jordan_candidate_results(results)

    if len(safe) >= 2:
        console.print(
            f"\n[bold]💡 Daily/work safe setup:[/bold]  Primary → [cyan]{safe[0].ip}[/cyan] "
            f"[dim]({safe[0].provider}, {safe[0].category})[/dim]   "
            f"Secondary → [cyan]{safe[1].ip}[/cyan] "
            f"[dim]({safe[1].provider}, {safe[1].category})[/dim]"
        )
    if len(jo) >= 2:
        console.print(
            f"[bold]🇯🇴 Experimental local setup:[/bold]  Primary → [cyan]{jo[0].ip}[/cyan] "
            f"[dim]({jo[0].provider})[/dim]   Secondary → [cyan]{jo[1].ip}[/cyan] "
            f"[dim]({jo[1].provider})[/dim]   [yellow]caution: re-test on another day/time first[/yellow]"
        )
    elif jo:
        console.print(
            f"[bold]🇯🇴 Experimental local candidate:[/bold]  [cyan]{jo[0].ip}[/cyan] "
            f"[dim]({jo[0].provider})[/dim]   [yellow]caution: re-test before daily use[/yellow]"
        )
    if len(family) >= 2:
        console.print(
            f"[bold]👪 Family-filtered setup:[/bold]  Primary → [cyan]{family[0].ip}[/cyan] "
            f"Secondary → [cyan]{family[1].ip}[/cyan]   "
            f"[yellow]only if adult-content filtering is desired[/yellow]"
        )


def print_rich_results(results: List[ServerStats], do_ping: bool, do_dnssec: bool = False):
    table = Table(title="DNS Benchmark Results — Amman, Jordan", show_lines=False,
                   header_style="bold cyan", border_style="bright_black")
    table.add_column("#", justify="right", width=3)
    table.add_column("", width=2)
    table.add_column("Server", style="white")
    table.add_column("IP", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Avg", justify="right")
    table.add_column("P95", justify="right")
    table.add_column("Jitter", justify="right")
    table.add_column("Fail%", justify="right")
    if do_ping:
        table.add_column("Ping", justify="right")
    if do_dnssec:
        table.add_column("DNSSEC", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Category", style="magenta")

    for i, r in enumerate(results, 1):
        def _f(v): return f"{v:.1f}ms" if v is not None else "—"
        row = [
            str(i), TIER_DOT[r.tier],
            f"[{TIER_COLOR[r.tier]}]{r.provider}[/]",
            r.ip, STATUS_STYLE[r.status], _f(r.avg_ms), _f(r.p95_ms),
            _f(r.stdev_ms) if r.alive else "—", f"{r.fail_pct:.1f}%",
        ]
        if do_ping:
            row.append(_f(r.ping_avg_ms) if r.ping_reachable else "—")
        if do_dnssec:
            if r.dnssec_validates is True:
                row.append("[bold green]✓[/]")
            elif r.dnssec_validates is False:
                row.append("[dim]✗[/]")
            else:
                row.append("—")
        row += [f"{r.score:.1f}" if r.alive else "—", r.category]
        table.add_row(*row)

    console.print(table)
    console.print()

    top5 = [r for r in results if r.recommended][:5]
    used_fallback = not top5
    if used_fallback:
        top5 = results[:5]
    lines = []
    for i, r in enumerate(top5):
        medal = ["🥇", "🥈", "🥉", "4.", "5."][i]
        lines.append(
            f"  {medal} [bold green]{r.provider}[/bold green]  [dim]({r.ip})[/dim]   "
            f"Avg: [cyan]{r.avg_ms:.1f}ms[/cyan]  P95: [yellow]{r.p95_ms:.1f}ms[/yellow]  "
            f"Score: {r.score:.1f}  Fail: {r.fail_pct:.1f}%  [dim]({r.category})[/dim]"
        )
    panel_title = ("[bold red]⚠ NO SERVER MET THE PASS THRESHOLD — closest 5 shown[/bold red]"
                   if used_fallback else "[bold green]🏆 TOP RECOMMENDED (PASS-status only)[/bold green]")
    console.print(Panel("\n".join(lines), title=panel_title,
                         border_style="red" if used_fallback else "green"))
    if used_fallback:
        console.print(
            "[red]   None of the tested servers stayed reliable enough to recommend "
            "(too many timeouts/failures this run). This usually means the network "
            "conditions during this run were too harsh — try fewer --workers, a higher "
            "--timeout, or re-run later. Don't commit to any of these as your daily "
            "DNS based on this run.[/red]"
        )

    # Daily-use guidance: fastest is not always appropriate for work/daily use.
    # Family resolvers are listed, but excluded from the default daily/work suggestion.
    print_setup_lines(results)
    console.print(
        "\n[dim]Tip: speed varies by time of day. Run with --repeat 2 or 3 (spaced apart) "
        "for a steadier average, or re-run this script at a different hour and compare.[/dim]"
    )
    console.print(
        "[dim]Jitter = stdev of successful query latency; lower is steadier "
        "(matters for calls/gaming, not just page loads). Use --dnssec to also "
        "check whether each resolver validates DNSSEC signatures.[/dim]\n"
    )


# ══════════════════════════════════════════════════════════════════════
#  CHARTS — one PNG per chart, green/yellow/red performance coloring
# ══════════════════════════════════════════════════════════════════════

TIER_HEX = {"green": "#22C55E", "yellow": "#F5B400", "red": "#EF4444"}
BG_COLOR, PANEL_COLOR, GRID_COLOR = "#0F1225", "#171A33", "#2A2E4D"
TEXT_COLOR, SUBTEXT_COLOR = "#E7E9F5", "#9AA0C3"


def _rounded_hbar(ax, y, width, color, height=0.62, rounding=0.35):
    """Draw a single horizontal bar with rounded ends instead of a plain rectangle."""
    if width <= 0:
        width = 0.001
    box = mpatches.FancyBboxPatch(
        (0, y - height / 2), width, height,
        boxstyle=f"round,pad=0,rounding_size={rounding}",
        linewidth=0, facecolor=color, zorder=3,
    )
    ax.add_patch(box)


def _save_hbar_chart(stats: List[ServerStats], value_fn, title: str, subtitle: str,
                      xlabel: str, out_path: str, value_suffix: str = "ms",
                      footnote: Optional[str] = None):
    """Dashboard-style horizontal bar chart: rank + provider + category labels,
    rounded/zebra-striped rows, and an on-chart explainer of what the metric means
    so the PNG is self-contained and doesn't require cross-referencing the README."""
    if not stats:
        return
    n = len(stats)
    fig_h = max(4.2, 0.52 * n + 2.4)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    fig.patch.set_facecolor(BG_COLOR)
    ax.set_facecolor(PANEL_COLOR)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=TEXT_COLOR, labelsize=9.5, length=0)
    ax.xaxis.label.set_color(SUBTEXT_COLOR)
    ax.grid(axis="x", color=GRID_COLOR, linewidth=0.9, alpha=0.9, zorder=0)
    ax.set_axisbelow(True)
    for i in range(n):
        if i % 2 == 0:
            ax.axhspan(i - 0.5, i + 0.5, color="#FFFFFF", alpha=0.02, zorder=0)

    vals = [value_fn(s) or 0 for s in stats]
    max_val = max(vals) if vals else 1
    y_positions = list(range(n - 1, -1, -1))

    for s, val, y in zip(stats, vals, y_positions):
        _rounded_hbar(ax, y, val, TIER_HEX.get(s.tier, TIER_HEX["yellow"]))
        ax.text(val + max_val * 0.015, y, f"{val:.1f}{value_suffix}",
                va="center", ha="left", color=TEXT_COLOR, fontsize=9.5,
                fontweight="bold", zorder=4)

    labels = [f"{i + 1:>2}  {s.provider}\n     {s.ip}  ·  {s.category}"
              for i, s in enumerate(stats)]
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=9, color=TEXT_COLOR, linespacing=1.6, family="monospace")
    ax.set_xlim(0, max_val * 1.30 if max_val else 1)
    ax.set_ylim(-0.7, n - 0.3)
    ax.set_xlabel(xlabel, fontsize=10, labelpad=10)

    tier_patches = [mpatches.Patch(color=TIER_HEX["green"], label="Good"),
                    mpatches.Patch(color=TIER_HEX["yellow"], label="OK"),
                    mpatches.Patch(color=TIER_HEX["red"], label="Poor / Dead")]
    leg = ax.legend(handles=tier_patches, loc="upper right", fontsize=8.5,
                     framealpha=0.0, labelcolor=TEXT_COLOR, handlelength=1.2,
                     title="Performance tier", title_fontsize=8.5, ncol=3,
                     bbox_to_anchor=(1.0, 1.06))
    leg.get_title().set_color(SUBTEXT_COLOR)

    fig.text(0.06, 0.965, title, fontsize=17, fontweight="bold", color=TEXT_COLOR, ha="left")
    fig.text(0.06, 0.937, subtitle, fontsize=10.5, color=SUBTEXT_COLOR, ha="left", style="italic")
    fig.text(0.94, 0.965, "DNS Benchmark", fontsize=10, color=SUBTEXT_COLOR, ha="right")
    fig.text(0.94, 0.945, "Amman, Jordan  ·  " + datetime.now().strftime("%Y-%m-%d %H:%M"),
             fontsize=9, color=SUBTEXT_COLOR, ha="right")
    if footnote:
        fig.text(0.06, 0.012, footnote, fontsize=8, color=SUBTEXT_COLOR, ha="left", wrap=True)

    fig.subplots_adjust(left=0.22, right=0.97, top=0.90, bottom=0.14 + 0.01 * n / 10)
    fig.savefig(out_path, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    console.print(f"[green]📊 Saved:[/green] [cyan]{out_path}[/cyan]")


def generate_charts(results: List[ServerStats], do_ping: bool, prefix: str, top_n: int = 20):
    valid = [r for r in results if r.alive and r.avg_ms is not None]
    if not valid:
        console.print("[yellow]No valid (alive) results to chart.[/yellow]")
        return

    top_avg = sorted(valid, key=lambda r: r.avg_ms)[:top_n]
    _save_hbar_chart(
        top_avg, lambda r: r.avg_ms,
        f"Average DNS Query Latency — Top {len(top_avg)}",
        "Typical reply time across all successful queries — lower means pages start resolving faster on a normal day.",
        "Milliseconds (lower = better)", f"{prefix}_avg_latency.png",
        footnote="Avg = mean latency of successful replies only. Failed/timed-out queries are excluded here and "
                 "are instead reflected in the Fail% column and the composite score.",
    )

    top_p95 = sorted(valid, key=lambda r: r.p95_ms or 99999)[:top_n]
    _save_hbar_chart(
        top_p95, lambda r: r.p95_ms,
        f"P95 (Worst-Case) DNS Query Latency — Top {len(top_p95)}",
        "95% of replies came in at or under this number — this is the 'bad moment' you'll actually notice, not an average.",
        "Milliseconds — 95th percentile (lower = better)", f"{prefix}_p95_latency.png",
        footnote="P95 = 95th percentile of successful reply latency. A resolver can have a low average but a high "
                 "P95 if it occasionally stalls — that inconsistency shows up here, not in the average chart.",
    )

    if do_ping:
        ping_valid = [r for r in valid if r.ping_reachable]
        top_ping = sorted(ping_valid, key=lambda r: r.ping_avg_ms)[:top_n]
        if top_ping:
            _save_hbar_chart(
                top_ping, lambda r: r.ping_avg_ms,
                f"ICMP Ping Latency — Top {len(top_ping)}",
                "Raw network round-trip time, independent of DNS software — useful to separate network path issues from resolver issues.",
                "Milliseconds (lower = better)", f"{prefix}_ping_latency.png",
            )

    # Composite chart is now daily/work-safe by default: it excludes Family DNS
    # from the "recommended" chart unless there are no safe recommendations.
    top_rec = daily_safe_results(results)[:10] or [r for r in results if r.recommended][:10] or valid[:10]
    _save_hbar_chart(
        top_rec, lambda r: r.score,
        f"Top {len(top_rec)} Daily/Work-Safe Recommended — Composite Score",
        "One ranking number blending average speed, worst-case speed, failure rate, and consistency — lower is better overall.",
        "Composite score (lower = better, not milliseconds)", f"{prefix}_composite_score.png",
        value_suffix="",
        footnote="Score = avg×0.45 + P95×0.20 + (fail%×8)×0.20 + jitter×0.15. Failures are weighted 8x because a "
                 "fast resolver that silently drops queries is worse than a slightly slower, stable one.",
    )


# ══════════════════════════════════════════════════════════════════════
#  SAVE: CSV / TXT / TIMELINE
# ══════════════════════════════════════════════════════════════════════

def build_timeline_rows(by_server: Dict[str, List[QueryResult]]) -> List[Dict[str, object]]:
    """Per-second health summary based on actual query start times.

    This makes rate-limit patterns visible in a small CSV: if failures appear
    in synchronized bands across unrelated WAN DNS providers, the issue is
    local rate policing or packet loss, not the providers themselves.
    """
    all_results = [qr for rows in by_server.values() for qr in rows if qr.started_at > 0]
    if not all_results:
        return []
    t0 = min(qr.started_at for qr in all_results)
    buckets: Dict[int, List[QueryResult]] = {}
    for qr in all_results:
        sec = int(qr.started_at - t0)
        buckets.setdefault(sec, []).append(qr)

    rows: List[Dict[str, object]] = []
    for sec in sorted(buckets):
        items = buckets[sec]
        total = len(items)
        ok = sum(1 for q in items if q.ok)
        fail = total - ok
        wan = [q for q in items if not is_private_ip(q.ip)]
        lan = [q for q in items if is_private_ip(q.ip)]
        wan_fail = sum(1 for q in wan if not q.ok)
        lan_fail = sum(1 for q in lan if not q.ok)
        err_ctr = Counter(q.error or RCODE_NAMES.get(q.rcode, str(q.rcode)) for q in items if not q.ok)
        rows.append({
            "second": sec,
            "queries": total,
            "ok": ok,
            "fail": fail,
            "fail_pct": round((fail / total * 100) if total else 0, 2),
            "wan_queries": len(wan),
            "wan_fail_pct": round((wan_fail / len(wan) * 100) if wan else 0, 2),
            "lan_queries": len(lan),
            "lan_fail_pct": round((lan_fail / len(lan) * 100) if lan else 0, 2),
            "top_errors": ";".join(f"{k}:{v}" for k, v in err_ctr.most_common(5)) or "-",
        })
    return rows


def save_timeline_csv(by_server: Dict[str, List[QueryResult]], path: str):
    rows = build_timeline_rows(by_server)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["second", "queries", "ok", "fail", "fail_pct",
                      "wan_queries", "wan_fail_pct", "lan_queries", "lan_fail_pct", "top_errors"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    console.print(f"[green]🧭 Timeline CSV saved to:[/green] [cyan]{path}[/cyan]")


def save_csv(results: List[ServerStats], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Provider", "IP", "Category", "Status", "Alive", "Tier", "Avg_ms", "Median_ms",
                    "P95_ms", "Min_ms", "Max_ms", "Stdev_ms", "Fail_pct", "Ping_avg_ms",
                    "Ping_loss_pct", "DNSSEC_validates", "Score", "Recommended", "Filters", "DailyUse",
                    "RCodes", "Errors"])
        for i, r in enumerate(results, 1):
            w.writerow([i, r.provider, r.ip, r.category, r.status, r.alive, r.tier,
                        r.avg_ms, r.median_ms, r.p95_ms, r.min_ms, r.max_ms, round(r.stdev_ms, 2),
                        round(r.fail_pct, 2), r.ping_avg_ms, r.ping_loss_pct, r.dnssec_validates, r.score,
                        r.recommended, r.filters, r.daily_use, r.rcodes, r.errors])
    console.print(f"[green]💾 CSV saved to:[/green] [cyan]{path}[/cyan]")


def save_txt(results: List[ServerStats], path: str, do_ping: bool, do_dnssec: bool = False,
             bottleneck_suspected: bool = False, run_id: Optional[str] = None,
             output_dir: Optional[str] = None):
    with open(path, "w", encoding="utf-8") as f:
        f.write("=" * 110 + "\n")
        f.write(f"DNS BENCHMARK REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("Location : Amman, Jordan\n")
        if run_id:
            f.write(f"Run ID   : {run_id}\n")
        if output_dir:
            f.write(f"Output   : {os.path.abspath(output_dir)}\n")
        f.write("=" * 110 + "\n\n")

        if bottleneck_suspected:
            f.write("*** WARNING: LIKELY LOCAL NETWORK BOTTLENECK DETECTED ***\n"
                     "A majority of independent WAN DNS servers failed heavily in this run while\n"
                     "any LAN-local server stayed healthy. Independent public providers essentially\n"
                     "never fail in lockstep for real reasons — this pattern almost always means\n"
                     "local DNS-rate policing, NAT/connection-tracking pressure, or UDP/53 packet\n"
                     "dropping near your router/CPE/ISP path, not that the providers actually broke.\n"
                     "Re-run with a lower --qps first (try 3-5) before trusting the fail%/scores/\n"
                     "recommendations below. --workers is only concurrency; --qps is the real rate cap.\n\n")

        hdr = (f"{'Rank':<5} {'Status':<9} {'Tier':<6} {'Server':<26} {'IP':<18} {'Avg':>8} "
               f"{'P95':>8} {'Jitter':>8} {'Fail%':>7}")
        if do_ping:
            hdr += f" {'Ping':>8}"
        if do_dnssec:
            hdr += f" {'DNSSEC':>7}"
        hdr += f" {'Score':>9}  Category"
        f.write(hdr + "\n")
        f.write("-" * len(hdr) + "\n")

        for i, r in enumerate(results, 1):
            def _f(v): return f"{v:.1f}ms" if v is not None else "N/A"
            row = (f"{i:<5} {r.status:<9} {r.tier:<6} {r.provider:<26} {r.ip:<18} "
                   f"{_f(r.avg_ms):>8} {_f(r.p95_ms):>8} "
                   f"{(_f(r.stdev_ms) if r.alive else 'N/A'):>8} {r.fail_pct:>6.1f}%")
            if do_ping:
                row += f" {(_f(r.ping_avg_ms) if r.ping_reachable else 'N/A'):>8}"
            if do_dnssec:
                dnssec_str = "yes" if r.dnssec_validates is True else ("no" if r.dnssec_validates is False else "N/A")
                row += f" {dnssec_str:>7}"
            row += f" {(f'{r.score:.1f}' if r.alive else 'N/A'):>9}  {r.category}"
            f.write(row + "\n")

        f.write("\nSTATUS KEY: PASS = answered reliably | DEGRADED = answered but failed too many "
                 "queries to be trustworthy | FAIL = alive at probe time but failed every query in "
                 "the full test | DEAD = never answered, not benchmarked\n")
        f.write("Jitter = stdev (ms) of successful query latency; lower is steadier.\n")
        if do_dnssec:
            f.write("DNSSEC = whether the resolver validates DNSSEC signatures (tested via "
                     "dnssec-failed.org, which must return SERVFAIL if validation is enforced).\n")

        f.write("\n\nTOP 5 FASTEST PASSING SERVERS (raw score, may include Family DNS):\n")
        top5 = [r for r in results if r.recommended][:5]
        if not top5:
            f.write("  NONE — no server met the PASS threshold this run. Showing closest 5 "
                     "below for reference only; do not commit to one of these as your daily "
                     "DNS based on this run alone.\n")
            top5 = results[:5]
        for i, r in enumerate(top5, 1):
            f.write(f"  {i}. {r.provider} ({r.ip}) — Score: {r.score:.1f} | Avg: {r.avg_ms:.1f}ms | "
                     f"Category: {r.category} | Filters: {r.filters} | Daily use: {r.daily_use}\n")

        safe = daily_safe_results(results)
        family = family_results(results)
        jo = jordan_candidate_results(results)
        f.write("\nRECOMMENDED SETUPS:\n")
        if len(safe) >= 2:
            f.write(f"  Daily/work safe: Primary = {safe[0].ip} ({safe[0].provider})  "
                    f"Secondary = {safe[1].ip} ({safe[1].provider})\n")
        elif safe:
            f.write(f"  Daily/work safe: Primary = {safe[0].ip} ({safe[0].provider})  Secondary = N/A\n")
        else:
            f.write("  Daily/work safe: NONE — no safe Global/Global Security/Regional (JO) server passed.\n")
        if len(jo) >= 2:
            f.write(f"  Jordan experimental: Primary = {jo[0].ip} ({jo[0].provider})  "
                    f"Secondary = {jo[1].ip} ({jo[1].provider}) — re-test before daily use.\n")
        elif jo:
            f.write(f"  Jordan experimental: {jo[0].ip} ({jo[0].provider}) — re-test before daily use.\n")
        if len(family) >= 2:
            f.write(f"  Family-filtered: Primary = {family[0].ip} ({family[0].provider})  "
                    f"Secondary = {family[1].ip} ({family[1].provider}) — only if adult-content filtering is desired.\n")

        f.write("\n\nCATEGORY GUIDE — what each category filters and whether it's safe for daily use:\n")
        for cat, info in CATEGORY_INFO.items():
            if any(r.category == cat for r in results):
                f.write(f"  [{cat}]  Filters: {info['filters']}  |  Daily use: {info['daily_use']}\n")
                f.write(f"      {info['note']}\n")

    console.print(f"[green]📝 TXT report saved to:[/green] [cyan]{path}[/cyan]")




def build_timestamped_output_dir(base_dir: str, prefix: str, run_id: str) -> str:
    """Return an output directory with the run timestamp appended.

    Examples:
      --out-dir clean_dns_safe -> clean_dns_safe_2026-07-03_12-08-44
      --out-dir .              -> dns_run_2026-07-03_12-08-44
      --out-dir C:\\tmp\\dns     -> C:\\tmp\\dns_2026-07-03_12-08-44

    The function is intentionally simple and filesystem-safe for Windows:
    colons are not used in the timestamp.
    """
    base = (base_dir or ".").strip()
    if base in (".", "./", ".\\"):
        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", (prefix or "dns")).strip("_.-") or "dns"
        return f"{safe_prefix}_run_{run_id}"

    # Preserve absolute/relative parent path while timestamping only the final folder name.
    norm = os.path.normpath(base)
    parent = os.path.dirname(norm)
    leaf = os.path.basename(norm.rstrip("/\\")) or "dns_results"
    stamped = f"{leaf}_{run_id}"
    return os.path.join(parent, stamped) if parent else stamped

# ══════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="DNS Benchmark — Amman, Jordan (rate-limited flat-parallel engine)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dns_benchmark.py --include-current --save --qps 5   # validated safe run
  python dns_benchmark.py --quick --qps 5 --save              # fast conservative subset
  python dns_benchmark.py --qps 10 --save                     # higher-rate comparison
  python dns_benchmark.py --ping                              # also collect ICMP (slower)
  python dns_benchmark.py --extra custom.txt --save           # add extra DNS IPs from a file

Output folders are timestamped by default, e.g. clean_dns_safe_2026-07-03_12-08-44.
Use --no-timestamp-output only if you intentionally want to overwrite/reuse a fixed folder.
        """,
    )
    p.add_argument("--rounds", type=int, default=3, help="Query rounds per domain per server (default: 3)")
    p.add_argument("--workers", type=int, default=10,
                   help="Parallel worker threads for the query phase (default: 10). Workers cap in-flight queries, NOT query rate.")
    p.add_argument("--qps", type=float, default=10.0,
                   help="Strict global outbound DNS query-rate cap for the full test (default: 10 qps). Use 0 to disable.")
    p.add_argument("--probe-workers", type=int, default=10,
                   help="Parallel workers for the liveness probe (default: 10)")
    p.add_argument("--probe-qps", type=float, default=5.0,
                   help="Strict outbound DNS query-rate cap for the probe phase (default: 5 qps). Use 0 to disable.")
    p.add_argument("--timeout", type=float, default=2.0, help="Per-query timeout seconds (default: 2.0)")
    p.add_argument("--probe-timeout", type=float, default=2.0, help="Liveness probe timeout seconds (default: 2.0)")
    p.add_argument("--uncached", type=int, default=2, help="Cache-busting random queries per round (default: 2)")
    p.add_argument("--min-success", type=float, default=95.0, help="Min success%% to be 'recommended' (default: 95)")
    p.add_argument("--include-current", action="store_true", help="Also test your system/ISP/router DNS")
    p.add_argument("--include-archived", action="store_true",
                   help="Also re-test servers confirmed dead in the last full run (see dns_servers.py)")
    p.add_argument("--repeat", type=int, default=1,
                   help="Run the full query phase N times and average results — reduces noise from "
                        "one-off network spikes (default: 1, i.e. a single pass)")
    p.add_argument("--extra", default="", help="File with extra/unknown DNS IPs to test")
    p.add_argument("--quick", action="store_true", help="Quick mode: 10 servers, 2 rounds, 8 domains")
    p.add_argument("--ping", action="store_true", help="Also run ICMP ping (off by default — see header docstring)")
    p.add_argument("--dnssec", action="store_true",
                   help="Also check whether each resolver validates DNSSEC (one extra query/server, "
                        "via dnssec-failed.org — adds a 'DNSSEC' column, never affects the score)")
    p.add_argument("--weight-avg", type=float, default=DEFAULT_WEIGHTS["avg"],
                   help=f"Composite score weight for average latency (default: {DEFAULT_WEIGHTS['avg']})")
    p.add_argument("--weight-p95", type=float, default=DEFAULT_WEIGHTS["p95"],
                   help=f"Composite score weight for P95/worst-case latency (default: {DEFAULT_WEIGHTS['p95']})")
    p.add_argument("--weight-fail", type=float, default=DEFAULT_WEIGHTS["fail"],
                   help=f"Composite score weight for failure rate (default: {DEFAULT_WEIGHTS['fail']})")
    p.add_argument("--weight-stdev", type=float, default=DEFAULT_WEIGHTS["stdev"],
                   help=f"Composite score weight for latency consistency/jitter (default: {DEFAULT_WEIGHTS['stdev']})")
    p.add_argument("--save", action="store_true", help="Save CSV + TXT report (charts and log always save)")
    p.add_argument("--out-dir", default=".",
                   help="Base output directory. A timestamp is appended by default so repeated runs do not overwrite each other.")
    p.add_argument("--no-timestamp-output", action="store_true",
                   help="Disable automatic timestamping of the output directory. Use only if you intentionally want a fixed folder.")
    p.add_argument("--prefix", default="dns", help="Filename prefix for chart files (default: dns)")
    p.add_argument("--top", type=int, default=0, help="Show only top N in terminal (0 = all)")
    return p.parse_args()


def main():
    args = parse_args()
    run_start = time.time()
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    requested_out_dir = args.out_dir
    if not args.no_timestamp_output:
        args.out_dir = build_timestamped_output_dir(args.out_dir, args.prefix, run_id)
    args.run_id = run_id
    args.requested_out_dir = requested_out_dir

    os.makedirs(args.out_dir, exist_ok=True)
    log_path = os.path.join(args.out_dir, "dns_benchmark.log")
    logger = setup_logging(log_path)
    logger.info("=" * 70)
    logger.info("DNS BENCHMARK RUN START")
    logger.info(f"Run ID: {run_id}")
    logger.info(f"Requested output dir: {requested_out_dir}")
    logger.info(f"Effective output dir: {args.out_dir}")
    logger.info(f"Args: {vars(args)}")

    # ── build server pool (deduplicated by IP) ──────────────────────
    servers = get_servers(include_archived=args.include_archived)

    if args.quick:
        quick_keys = [
            "Google Primary", "Google Secondary", "Cloudflare Primary", "Cloudflare Secondary",
            "Quad9 Secure", "OpenDNS Primary", "AdGuard Primary",
            "JUNet/BAU DNS 1", "JUNet/BAU DNS 2", "Orange/JT Candidate 1",
        ]
        servers = {k: servers[k] for k in quick_keys if k in servers}
        rounds = min(args.rounds, 2)
        domains = ALL_DOMAINS[:8]
    else:
        rounds = args.rounds
        domains = ALL_DOMAINS

    if args.include_current:
        servers.update(discover_system_dns())
    if args.extra:
        servers.update(load_custom_dns(args.extra))

    # Re-run de-duplication after --include-current and --extra so the same IP
    # cannot appear twice under different names/categories.
    before_dedup = len(servers)
    servers = deduplicate_server_pool(servers)
    if len(servers) != before_dedup:
        logger.info(f"Deduplicated server pool after extras/current DNS: {before_dedup} -> {len(servers)}")

    if not servers:
        console.print("[red]No servers to test.[/red]")
        logger.error("No servers configured — aborting run.")
        sys.exit(2)

    console.print(Panel.fit(
        f"[bold cyan]DNS Benchmark — Amman, Jordan[/bold cyan]\n\n"
        f"  Servers      : [yellow]{len(servers)}[/yellow] (deduplicated by IP)"
        f"{'  [+ archived, re-testing dead servers]' if args.include_archived else ''}\n"
        f"  Domains      : [yellow]{len(domains)} real + {args.uncached} cache-busting / round[/yellow]\n"
        f"  Rounds       : [yellow]{rounds}[/yellow]   Passes: [yellow]{args.repeat}[/yellow]\n"
        f"  Query workers: [yellow]{args.workers}[/yellow]   QPS cap: [yellow]{args.qps:g}[/yellow]\n"
        f"  Probe workers: [yellow]{args.probe_workers}[/yellow]   Probe QPS cap: [yellow]{args.probe_qps:g}[/yellow]\n"
        f"  Run ID       : [yellow]{args.run_id}[/yellow]\n"
        f"  Output dir   : [dim]{args.out_dir}[/dim]\n"
        f"  ICMP ping    : [yellow]{'yes' if args.ping else 'no (use --ping to enable)'}[/yellow]\n"
        f"  DNSSEC check : [yellow]{'yes' if args.dnssec else 'no (use --dnssec to enable)'}[/yellow]\n"
        f"  Score weights: [dim]avg={args.weight_avg} p95={args.weight_p95} "
        f"fail={args.weight_fail} stdev={args.weight_stdev}[/dim]\n"
        f"  Log file     : [dim]{log_path}[/dim]",
        border_style="cyan",
    ))
    console.print()

    # ── Phase 1: probe ───────────────────────────────────────────────
    alive, dead = probe_all(servers, args.probe_timeout, args.probe_workers, args.probe_qps, logger)

    if dead:
        dead_list = ", ".join(f"{n} ({i.get('ip')})" for n, i in dead.items())
        logger.info(f"{len(dead)} servers marked DEAD after probe and excluded from full test: {dead_list}")

    if not alive:
        console.print("[red]No servers passed the liveness probe — nothing to benchmark.[/red]")
        logger.error("All servers failed the probe phase. Aborting.")
        sys.exit(1)

    # ── Phase 2: flat parallel full test (alive servers only) ───────
    # --repeat N runs this phase N times and merges all raw query
    # results before aggregating, which smooths out one-off network
    # spikes (the kind that show up as a single huge max_ms value).
    # For true time-of-day averaging (e.g. morning vs evening), still
    # re-run the whole script separately and compare reports — that's
    # not something a single process invocation can capture.
    by_server: Dict[str, List[QueryResult]] = {name: [] for name in alive}
    for pass_num in range(1, args.repeat + 1):
        if args.repeat > 1:
            console.print(f"[cyan]── Pass {pass_num}/{args.repeat} ──[/cyan]")
        pass_results = run_flat_benchmark(alive, rounds, domains, args.uncached, args.timeout,
                                           args.workers, args.qps, logger)
        for name, results_list in pass_results.items():
            by_server[name].extend(results_list)
        if pass_num < args.repeat:
            time.sleep(1.5)

    # ── Optional ping ─────────────────────────────────────────────
    ping_data: Dict[str, Tuple] = {}
    if args.ping:
        ping_data = run_pings(alive, args.probe_workers, logger)

    # ── Optional DNSSEC validation check ────────────────────────────
    dnssec_data: Dict[str, Optional[bool]] = {}
    if args.dnssec:
        dnssec_data = run_dnssec_checks(alive, args.timeout, args.probe_workers, logger)

    weights = {"avg": args.weight_avg, "p95": args.weight_p95,
               "fail": args.weight_fail, "stdev": args.weight_stdev}

    # ── Aggregate ────────────────────────────────────────────────
    results: List[ServerStats] = []
    for name, info in alive.items():
        p_avg, p_loss, p_ok = ping_data.get(name, (None, None, None))
        results.append(aggregate_server(name, info, by_server.get(name, []), True,
                                         p_avg, p_loss, p_ok, args.min_success,
                                         dnssec_data.get(name), weights))
    for name, info in dead.items():
        results.append(aggregate_server(name, info, [], False, None, None, None,
                                         args.min_success, None, weights))

    results.sort(key=lambda r: r.score)

    display = results[:args.top] if args.top > 0 else results
    print_rich_results(display, args.ping, args.dnssec)
    bottleneck_suspected = check_for_local_bottleneck(results, logger)

    # ── Outputs ──────────────────────────────────────────────────
    chart_prefix = os.path.join(args.out_dir, args.prefix)
    generate_charts(results, args.ping, chart_prefix)

    if args.save:
        save_csv(results, os.path.join(args.out_dir, "dns_benchmark_results.csv"))
        save_timeline_csv(by_server, os.path.join(args.out_dir, "dns_timeline.csv"))
        save_txt(results, os.path.join(args.out_dir, "dns_benchmark_report.txt"), args.ping, args.dnssec,
                 bottleneck_suspected, args.run_id, args.out_dir)

    elapsed = time.time() - run_start
    n_alive = len(alive)
    n_dead = len(dead)
    n_red_alive = sum(1 for r in results if r.alive and r.tier == "red")
    console.print(
        f"\n[bold]Done in {elapsed:.1f}s[/bold]  "
        f"([green]{n_alive} alive[/green], [red]{n_dead} dead[/red], "
        f"[yellow]{n_red_alive} alive-but-poor[/yellow])"
    )
    if bottleneck_suspected:
        console.print("[bold red]⚠ See warning above — this run's fail%/scores are likely "
                       "distorted by local network saturation, not real provider reliability.[/bold red]")

    logger.info(f"RUN COMPLETE in {elapsed:.1f}s — alive={n_alive} dead={n_dead} "
                f"poor_but_alive={n_red_alive}")
    logger.info("=" * 70)
    console.print(f"[green]Run output folder:[/green] [cyan]{os.path.abspath(args.out_dir)}[/cyan]")
    console.print(f"[dim]Full per-query log: {log_path}[/dim]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
