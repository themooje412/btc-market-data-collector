"""Record runner access to official Binance Futures sources without bypasses."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import time


REST_BASE = "https://fapi.binance.com"
REST_CHECKS = (
    ("open_interest", "/fapi/v1/openInterest?symbol=BTCUSDT"),
    ("mark_funding", "/fapi/v1/premiumIndex?symbol=BTCUSDT"),
    (
        "open_interest_history",
        "/futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=2",
    ),
)


def curl_status(url):
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    process = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--connect-timeout",
            "20",
            "--max-time",
            "30",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--user-agent",
            "btc-market-data-collector/1.0 (public-data-only)",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=35,
    )
    try:
        status = int(process.stdout.strip())
    except ValueError:
        status = None
    return {
        "url": url,
        "requested_at": started,
        "http_status": status,
        "status": "ok" if process.returncode == 0 and status == 200 else "error",
        "error": None
        if process.returncode == 0 and status == 200
        else (process.stderr.strip()[:300] or f"HTTP {status}"),
    }


def main():
    rest = {}
    for name, path in REST_CHECKS:
        rest[name] = curl_status(REST_BASE + path)
        time.sleep(0.2)

    archive_day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    archive_url = (
        "https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/"
        f"BTCUSDT-metrics-{archive_day}.zip"
    )
    archive = curl_status(archive_url)
    archive["data_date"] = archive_day
    archive["usage"] = "access_check_only_not_used_for_current_snapshot"
    archive["latency"] = "daily_historical_archive_not_realtime"

    rest_ok = all(item["status"] == "ok" for item in rest.values())
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "official_rest_documentation": "https://developers.binance.com/en/docs/derivatives/usds-margined-futures/general-info",
        "official_rest_base": REST_BASE,
        "official_rest": rest,
        "official_archive_documentation": "https://github.com/binance/binance-public-data",
        "official_archive": archive,
        "rest_access_resolved": rest_ok,
        "collector_policy": (
            "If official REST is unavailable, Binance Futures fields remain null/error. "
            "The delayed archive and other exchanges never populate Binance fields."
        ),
    }
    path = Path("docs/binance-futures-runner-check.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    for name, item in rest.items():
        print(f"Binance Futures {name}: {item['status']} (HTTP {item['http_status']})")
    print(
        "Binance official daily archive:",
        f"{archive['status']} (HTTP {archive['http_status']}, {archive_day})",
    )
    print("Binance Futures REST access resolved:", rest_ok)


if __name__ == "__main__":
    main()
