import re
import json
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

        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total_lines = len(lines)
        errors = [l for l in lines if "ERROR" in l]
        warnings = [l for l in lines if "WARNING" in l]
        web_fetches = [l for l in lines if "🌐 Fetching content" in l]
        fetch_fails = [
            l for l in lines if "⚠️ Fetch failed" in l or "❌ Error fetching" in l
        ]
        web_searches = [l for l in lines if "🔍 Performing web search" in l]

        print(
            f"=== Log Analysis Report ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ==="
        )
        print(f"Total Log Lines: {total_lines}")
        print(f"Total Errors: {len(errors)}")
        print(f"Total Warnings: {len(warnings)}")
        print("-" * 40)
        print(f"Web Fetch Attempts: {len(web_fetches)}")
        print(f"Web Fetch Failures: {len(fetch_fails)}")
        print(f"Web Search Attempts: {len(web_searches)}")

        if errors:
            print("\n--- Recent Errors ---")
            for err in errors[-5:]:
                print(err.strip())

        if fetch_fails:
            print("\n--- Recent Fetch Failures ---")
            for fail in fetch_fails[-5:]:
                print(fail.strip())

        urls = re.findall(r"https?://[^\s]+", "".join(fetch_fails))
        if urls:
            print("\n--- Top Failing Domains ---")
            domains = [re.sub(r"https?://([^/]+).*", r"\1", url) for url in urls]
            for domain, count in Counter(domains).most_common(3):
                print(f"{domain}: {count} failures")


if __name__ == "__main__":
    analyzer = LogAnalyzer()
    analyzer.analyze()
