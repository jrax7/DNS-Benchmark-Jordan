"""
dns_servers.py
═══════════════════════════════════════════════════════════════════════
Single source of truth for every DNS server the benchmark knows about.

Updated 2026-06-30 after a full benchmark run against the previous list:
After follow-up runs, known-dead and consistently degraded servers are kept
out of the default list in ARCHIVED_SERVERS so normal runs stay fast and
focused. Use --include-archived to periodically re-test them. They're kept on file — pass
`--include-archived` to dns_benchmark.py to re-test them occasionally
(servers do come back online, get reconfigured, etc).

Each entry: "Display Name": {"ip": "x.x.x.x", "category": "..."}

CATEGORY_INFO (below) documents, per category, whether it filters
content and whether it's a sane choice for your everyday/work DNS —
see that dict (and the README) before picking a "winner" purely by
speed. A fast resolver that blocks/filters things you didn't ask for
is not automatically the right daily choice.
"""

from __future__ import annotations
from typing import Dict


# ══════════════════════════════════════════════════════════════════════
#  CATEGORY METADATA — what each category actually does, and whether
#  it's appropriate to set as your everyday/system DNS resolver.
# ══════════════════════════════════════════════════════════════════════

CATEGORY_INFO: Dict[str, Dict] = {
    "Global": {
        "filters": "None",
        "daily_use": "Recommended",
        "note": "Plain public resolver, no content filtering. Safe default "
                 "for normal browsing, streaming, and work.",
    },
    "Global Security": {
        "filters": "Malware / phishing domains only",
        "daily_use": "Recommended",
        "note": "Blocks known-malicious domains, leaves everything else "
                 "untouched. A reasonable upgrade over plain DNS for daily "
                 "use — won't interfere with normal sites or work tools.",
    },
    "Global Family": {
        "filters": "Adult content + malware",
        "daily_use": "Only if you want that filtering",
        "note": "WILL block adult sites and sometimes misclassifies other "
                 "sites as a side effect. Don't use this as your only "
                 "resolver for general/work use unless the filtering is "
                 "actually what you want (e.g. a shared family device).",
    },
    "Global Filtering": {
        "filters": "Ads / trackers (varies by provider/mode)",
        "daily_use": "Usually fine, test first",
        "note": "Can occasionally break sites that detect ad-blocking or "
                 "rely on a blocked tracking domain to function. Mullvad's "
                 "filtering IPs (194.242.2.x) additionally only answer "
                 "while connected to the Mullvad VPN — they will appear "
                 "DEAD on a normal ISP connection, which is expected.",
    },
    "Regional (JO)": {
        "filters": "Unknown / ISP-dependent",
        "daily_use": "Recommended if alive and fast",
        "note": "Local Jordan ISP resolvers. When reachable, these are "
                 "often the lowest latency option since they're "
                 "geographically closest to you.",
    },
    "Regional": {
        "filters": "Unknown / provider-dependent",
        "daily_use": "Case-by-case",
        "note": "Non-Jordan regional resolvers (e.g. Yandex). Check the "
                 "provider's own policy before using for daily/work use.",
    },
    "Jordan Candidate": {
        "filters": "Unknown",
        "daily_use": "Caution",
        "note": "Sourced from public DNS-monitoring sites, not official "
                 "ISP documentation. May be an open resolver not intended "
                 "for public use, ISP-internal only, or rate-limited. "
                 "Only consider one of these for daily use if it scores "
                 "green AND stays green across more than one benchmark "
                 "run on a different day/time.",
    },
    "System / ISP": {
        "filters": "Whatever your ISP currently does",
        "daily_use": "Your current baseline",
        "note": "Auto-discovered from this machine's current DNS settings "
                 "(only present with --include-current). Useful as a "
                 "comparison point against the public resolvers above.",
    },
    "Custom": {
        "filters": "Unknown",
        "daily_use": "Depends on the server",
        "note": "Added at runtime via --extra. Filtering/safety depends "
                 "entirely on what you pointed it at.",
    },
}


# ══════════════════════════════════════════════════════════════════════
#  ACTIVE SERVERS — tested by default
# ══════════════════════════════════════════════════════════════════════

SERVERS: Dict[str, Dict[str, str]] = {

    # ── Global: general purpose, no filtering ───────────────────────
    "Google Primary":              {"ip": "8.8.8.8",          "category": "Global"},
    "Google Secondary":            {"ip": "8.8.4.4",           "category": "Global"},
    "Cloudflare Primary":          {"ip": "1.1.1.1",           "category": "Global"},
    "Cloudflare Secondary":        {"ip": "1.0.0.1",           "category": "Global"},
    "Quad9 Secure":                {"ip": "9.9.9.9",           "category": "Global"},
    "Quad9 Secondary":             {"ip": "149.112.112.112",   "category": "Global"},
    "OpenDNS Primary":             {"ip": "208.67.222.222",    "category": "Global"},
    "OpenDNS Secondary":           {"ip": "208.67.220.220",    "category": "Global"},
    "AdGuard Primary":             {"ip": "94.140.14.14",      "category": "Global"},
    "AdGuard Secondary":           {"ip": "94.140.15.15",      "category": "Global"},
    "AdGuard Non-filtering":       {"ip": "94.140.14.140",     "category": "Global"},
    "AdGuard Non-filtering 2":     {"ip": "94.140.14.141",     "category": "Global"},
    "Verisign Primary":            {"ip": "64.6.64.6",         "category": "Global"},
    "Verisign Secondary":          {"ip": "64.6.65.6",         "category": "Global"},
    # NOTE: this is NextDNS's generic anycast base IP with no profile ID
    # attached. Real NextDNS use normally appends a per-account profile
    # ID (via a linked IP or DoH URL), which enables per-user config and
    # logging. Tested bare like this, it behaves like a generic open
    # resolver rather than reflecting a real NextDNS-configured setup —
    # treat its results here as a rough floor, not representative.
    "NextDNS":                     {"ip": "45.90.28.0",        "category": "Global"},
    "ControlD Free 1":             {"ip": "76.76.2.11",        "category": "Global"},
    "ControlD Free 2":             {"ip": "76.76.10.11",       "category": "Global"},
    "ControlD DNS 1":              {"ip": "76.76.2.22",        "category": "Global"},
    "ControlD DNS 2":              {"ip": "76.76.10.22",       "category": "Global"},
    "SafeDNS":                     {"ip": "195.46.39.39",      "category": "Global"},
    "SafeDNS Secondary":           {"ip": "195.46.39.40",      "category": "Global"},
    "Neustar UltraDNS":            {"ip": "156.154.70.1",      "category": "Global"},
    "Neustar Secondary":           {"ip": "156.154.71.1",      "category": "Global"},

    # ── Global: security / malware filtering (safe for daily use) ──
    "Cloudflare Malware 1":        {"ip": "1.1.1.2",           "category": "Global Security"},
    "Cloudflare Malware 2":        {"ip": "1.0.0.2",           "category": "Global Security"},
    "Quad9 Secure ECS 1":          {"ip": "9.9.9.11",          "category": "Global Security"},
    "Quad9 Secure ECS 2":          {"ip": "149.112.112.11",    "category": "Global Security"},
    "Quad9 Unsecured 1":           {"ip": "9.9.9.10",          "category": "Global Security"},
    "Quad9 Unsecured 2":           {"ip": "149.112.112.10",    "category": "Global Security"},
    "CleanBrowsing Security":      {"ip": "185.228.168.9",     "category": "Global Security"},

    # ── Global: family / adult-content filtering (opt-in only) ──────
    "Cloudflare Family 1":         {"ip": "1.1.1.3",           "category": "Global Family"},
    "Cloudflare Family 2":         {"ip": "1.0.0.3",           "category": "Global Family"},
    "OpenDNS FamilyShield 1":      {"ip": "208.67.222.123",    "category": "Global Family"},
    "OpenDNS FamilyShield 2":      {"ip": "208.67.220.123",    "category": "Global Family"},
    "AdGuard Family 1":            {"ip": "94.140.14.15",      "category": "Global Family"},
    "AdGuard Family 2":            {"ip": "94.140.15.16",      "category": "Global Family"},
    "CleanBrowsing Family":        {"ip": "185.228.168.168",   "category": "Global Family"},

    # ── Regional / Middle East (general) ────────────────────────────
    "Yandex DNS Primary":          {"ip": "77.88.8.8",         "category": "Regional"},
    "Yandex DNS Secondary":        {"ip": "77.88.8.1",         "category": "Regional"},

    # ── Jordan Candidates: currently confirmed reachable ─────────────
    "JUNet/BAU DNS 1":             {"ip": "87.236.233.117",    "category": "Jordan Candidate"},
    "JUNet/BAU DNS 2":             {"ip": "87.236.232.5",      "category": "Jordan Candidate"},
    "JUNet/AHU Gateway Candidate": {"ip": "87.236.233.70",     "category": "Jordan Candidate"},
    "Orange/JT Candidate 1":       {"ip": "79.173.251.155",    "category": "Jordan Candidate"},
    "Orange/JT Candidate 2":       {"ip": "79.173.251.142",    "category": "Jordan Candidate"},
}


# ══════════════════════════════════════════════════════════════════════
#  ARCHIVED SERVERS — confirmed 100% unreachable in the 2026-06-30 run.
#  Excluded from default runs. Re-test with --include-archived.
# ══════════════════════════════════════════════════════════════════════

ARCHIVED_SERVERS: Dict[str, Dict[str, str]] = {
    # Mullvad's filtering IPs only answer while connected to Mullvad's
    # own VPN tunnel — dead on a normal ISP connection by design, not
    # a bug. Kept here for completeness, not worth retesting normally.
    "Mullvad Plain":               {"ip": "194.242.2.2",       "category": "Global Filtering"},
    "Mullvad Adblock":             {"ip": "194.242.2.3",       "category": "Global Filtering"},
    "Mullvad Base":                {"ip": "194.242.2.4",       "category": "Global Filtering"},
    "Mullvad Extended":            {"ip": "194.242.2.5",       "category": "Global Filtering"},
    "Mullvad Family":              {"ip": "194.242.2.6",       "category": "Global Family"},
    "Mullvad All":                 {"ip": "194.242.2.9",       "category": "Global Family"},

    # Legacy / discontinued, unreliable, or degraded public services
    "Level3 DNS 1":                {"ip": "4.2.2.1",           "category": "Global"},
    "Level3 DNS 2":                {"ip": "4.2.2.2",           "category": "Global"},
    "Comodo Secure DNS":           {"ip": "8.26.56.26",        "category": "Global"},
    "Comodo Secondary":            {"ip": "8.20.247.20",       "category": "Global"},
    "DNS.WATCH Primary":           {"ip": "84.200.69.80",      "category": "Global"},
    "DNS.WATCH Secondary":         {"ip": "84.200.70.40",      "category": "Global"},
    "Alternate DNS":               {"ip": "76.76.19.19",       "category": "Global"},
    "Alternate DNS 2":             {"ip": "76.223.122.150",    "category": "Global"},

    # Jordan ISP resolvers — dead from this vantage point at test time;
    # may be ISP-internal-only (only reachable from inside that ISP's
    # own network, not from arbitrary internet hosts).
    "Umniah Jordan DNS 1":         {"ip": "91.149.96.10",      "category": "Regional (JO)"},
    "Umniah Jordan DNS 2":         {"ip": "91.149.96.11",      "category": "Regional (JO)"},
    "Orange Jordan DNS 1":         {"ip": "62.3.1.1",          "category": "Regional (JO)"},
    "Orange Jordan DNS 2":         {"ip": "62.3.6.6",          "category": "Regional (JO)"},
    "Zain Jordan DNS 1":           {"ip": "212.118.2.2",       "category": "Regional (JO)"},
    "Zain Jordan DNS 2":           {"ip": "212.118.3.3",       "category": "Regional (JO)"},
    "VTEL Jordan":                 {"ip": "77.75.64.1",        "category": "Regional (JO)"},

    # Jordan Candidates — unverified, confirmed dead at test time
    "Umniah Cloud DNS 1":          {"ip": "212.118.12.22",     "category": "Jordan Candidate"},
    "Umniah Cloud DNS 2":          {"ip": "212.118.12.23",     "category": "Jordan Candidate"},
    "Orange Cache ODNS A":         {"ip": "194.165.130.114",   "category": "Jordan Candidate"},
    "Orange Cache ODNS B":         {"ip": "194.165.130.115",   "category": "Jordan Candidate"},
    "Orange Cache ODNS C":         {"ip": "194.165.130.178",   "category": "Jordan Candidate"},
    "Umniah Listed 1":             {"ip": "91.106.105.142",    "category": "Jordan Candidate"},
    "Umniah Listed 2":             {"ip": "91.106.105.218",    "category": "Jordan Candidate"},
    "Zain Data Listed":            {"ip": "188.247.93.122",    "category": "Jordan Candidate"},
    "Zain Listed 1":               {"ip": "176.28.250.235",    "category": "Jordan Candidate"},
    "Zain Listed 2":               {"ip": "176.28.250.122",    "category": "Jordan Candidate"},
    "VTEL Listed":                 {"ip": "185.96.70.36",      "category": "Jordan Candidate"},
    "JCS FiberLink Listed":        {"ip": "79.134.152.192",    "category": "Jordan Candidate"},
    "DAMAMAX Listed":              {"ip": "82.212.107.34",     "category": "Jordan Candidate"},
    "Orange/JTG Listed 1":         {"ip": "92.253.127.65",     "category": "Jordan Candidate"},
    "Orange/JTG Listed 2":         {"ip": "92.253.60.133",     "category": "Jordan Candidate"},
    "Orange/JTG Listed 3":         {"ip": "92.253.101.67",     "category": "Jordan Candidate"},
    "Orange/JTG Listed 4":         {"ip": "94.249.14.227",     "category": "Jordan Candidate"},
    "Nextjo Listed":               {"ip": "217.144.6.6",       "category": "Jordan Candidate"},
    "Jordan Telecom Listed":       {"ip": "212.34.0.140",      "category": "Jordan Candidate"},

    # Extra public-list candidates from the 2026-06-30 follow-up run —
    # timed out from this network/vantage point. Kept for occasional re-test.
    "Jordan Candidate 5.198.243.202":       {"ip": "5.198.243.202",     "category": "Jordan Candidate"},
    "Joramco Candidate":                    {"ip": "80.90.161.26",      "category": "Jordan Candidate"},
    "Orange/JT Candidate 92.253.23.8":      {"ip": "92.253.23.8",       "category": "Jordan Candidate"},
    "Orange/JT Zarqa Candidate":            {"ip": "185.98.225.173",    "category": "Jordan Candidate"},
    "Orange/JT Candidate 149.200.254.136":  {"ip": "149.200.254.136",   "category": "Jordan Candidate"},
    "VTEL Candidate 109.237.202.116":       {"ip": "109.237.202.116",   "category": "Jordan Candidate"},
    "Orange/JT Candidate 86.108.15.199":    {"ip": "86.108.15.199",     "category": "Jordan Candidate"},
    "Umniah/Batelco Candidate":             {"ip": "91.106.107.227",    "category": "Jordan Candidate"},
    "Orange/JT Candidate 79.173.253.186":   {"ip": "79.173.253.186",    "category": "Jordan Candidate"},
    "Zain Data Candidate 77.245.12.68":     {"ip": "77.245.12.68",      "category": "Jordan Candidate"},
    "VTEL Candidate 109.237.194.63":        {"ip": "109.237.194.63",    "category": "Jordan Candidate"},
    "Alzerini/Mail Candidate":              {"ip": "82.212.126.180",    "category": "Jordan Candidate"},
}


_CATEGORY_RANK = {
    "Regional (JO)": 0, "Global": 0, "Global Security": 0,
    "Global Family": 0, "Global Filtering": 0, "Regional": 0,
    "Jordan Candidate": 1, "System / ISP": 0, "Custom": 1,
}


def get_servers(include_archived: bool = False) -> Dict[str, Dict[str, str]]:
    """Return the de-duplicated server pool. Pass include_archived=True
    to also re-test servers confirmed dead in the last full run."""
    pool = dict(SERVERS)
    if include_archived:
        pool.update(ARCHIVED_SERVERS)
    return _deduplicate(pool)


def _deduplicate(pool: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    by_ip: Dict[str, str] = {}
    out: Dict[str, Dict[str, str]] = {}
    for name, info in pool.items():
        ip = info["ip"]
        if ip not in by_ip:
            by_ip[ip] = name
            out[name] = info
        else:
            existing_name = by_ip[ip]
            existing_rank = _CATEGORY_RANK.get(out[existing_name]["category"], 1)
            new_rank = _CATEGORY_RANK.get(info["category"], 1)
            if new_rank < existing_rank:
                del out[existing_name]
                by_ip[ip] = name
                out[name] = info
    return out


# Backwards-compatible alias used by older calls in dns_benchmark.py
def deduplicated() -> Dict[str, Dict[str, str]]:
    return get_servers(include_archived=False)
