import re
from collections import Counter
from datetime import datetime
from pathlib import Path


class LogAnalyzer:
    def __init__(self, log_path: str = "gateway.log"):
        self.log_path = Path(log_path)

    def analyze(self):
        if not self.log_path.exists():
            print(f"Log file not found: {self.log_path}")
            return

        with open(self.log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size > 2 * 1024 * 1024:
                f.seek(-2 * 1024 * 1024, 2)
                raw_lines = f.read().decode("utf-8", errors="ignore").splitlines()
                lines = raw_lines[1:] if len(raw_lines) > 1 else raw_lines
            else:
                f.seek(0)
                lines = f.read().decode("utf-8", errors="ignore").splitlines()

        total_lines = len(lines)
        errors = [line for line in lines if "ERROR" in line]
        warnings = [line for line in lines if "WARNING" in line]
        web_fetches = [line for line in lines if "🌐 Fetching content" in line]
        fetch_fails = [
            line
            for line in lines
            if "⚠️ Fetch failed" in line or "❌ Error fetching" in line
        ]
        web_searches = [line for line in lines if "🔍 Performing web search" in line]

        request_groups = {}
        for line in lines:
            rid_match = re.search(r"\[(req_[a-z0-9]+)\]", line)
            if rid_match:
                rid = rid_match.group(1)
                if rid not in request_groups:
                    request_groups[rid] = []
                request_groups[rid].append(line.strip())

        print(
            f"=== Log Analysis Report ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ==="
        )
        print(f"Total Log Lines: {total_lines}")
        print(f"Unique Requests Tracked: {len(request_groups)}")
        print(f"Total Errors: {len(errors)}")
        print(f"Total Warnings: {len(warnings)}")
        print("-" * 40)
        print(f"Web Fetch Attempts: {len(web_fetches)}")
        print(f"Web Fetch Failures: {len(fetch_fails)}")
        print(f"Web Search Attempts: {len(web_searches)}")

        if len(web_fetches) > 0:
            success_rate = (
                (len(web_fetches) - len(fetch_fails)) / len(web_fetches)
            ) * 100
            print(f"Web Scrape Success Rate: {success_rate:.1f}%")

        if errors:
            print("\n--- Recent Errors with Request Context ---")
            for err in errors[-3:]:
                rid_match = re.search(r"\[(req_[a-z0-9]+)\]", err)
                if rid_match:
                    rid = rid_match.group(1)
                    print(f"\n[Request Trace: {rid}]")
                    for log_line in request_groups.get(rid, [])[-10:]:
                        print(f"  {log_line}")
                else:
                    print(f"  {err.strip()}")

            print("\n--- Error Category Breakdown ---")
            categories = re.findall(r"\[([A-Z_]+)\] Request failed", "".join(errors))
            for cat, count in Counter(categories).most_common():
                print(f"  {cat}: {count}")

        if fetch_fails:
            print("\n--- Recent Fetch Failures ---")
            for fail in fetch_fails[-5:]:
                print(fail.strip())

        exhausted = [l for l in lines if "exhausted" in l.lower() or "429" in l]
        if exhausted:
            print("\n--- Quota & Rate Limit Summary ---")
            providers = re.findall(r"Provider (\w+) exhausted", "".join(exhausted))
            for p, count in Counter(providers).most_common():
                print(f"  {p.upper()}: {count} exhaustion events detected")

        urls = re.findall(r"https?://[^\s]+", "".join(fetch_fails))
        if urls:
            print("\n--- Top Failing Domains ---")
            domains = [re.sub(r"https?://([^/]+).*", r"\1", url) for url in urls]
            for domain, count in Counter(domains).most_common(3):
                print(f"{domain}: {count} failures")


if __name__ == "__main__":
    analyzer = LogAnalyzer()
    analyzer.analyze()
