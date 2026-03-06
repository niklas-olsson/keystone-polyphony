import asyncio
import os
import sys
import json
import subprocess
from datetime import datetime
from typing import Dict, Any

sys.path.insert(0, os.path.abspath("src"))
from liminal_bridge.architect import Architect


def truncate_text(text: str, max_length: int = 15000) -> str:
    """Truncates text to prevent LLM context window overflows."""
    if len(text) > max_length:
        return text[:max_length] + "\n\n...[TRUNCATED DUE TO LENGTH]..."
    return text


def read_file_content(filepath: str) -> str:
    """Reads and returns the content of a file, returning a placeholder if not found."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return truncate_text(f.read())
    except FileNotFoundError:
        return f"[File not found: {filepath}]"
    except Exception as e:
        return f"[Error reading {filepath}: {str(e)}]"


def run_script_output(script_path: str, args: list = None) -> str:
    """Runs a script and captures its output."""
    if args is None:
        args = []
    try:
        cmd = ["python3", script_path] + args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"[Error running {script_path}: {result.stderr.strip()}]"
    except Exception as e:
        return f"[Exception running {script_path}: {str(e)}]"


def get_pending_issues() -> str:
    """Gets a list of pending issues in .github/issues/."""
    issues_dir = ".github/issues"
    if not os.path.exists(issues_dir):
        return "No pending issues."
    issues = []
    for filename in os.listdir(issues_dir):
        if filename.endswith(".md"):
            issues.append(filename)
    return ", ".join(issues) if issues else "No pending issues."


async def aggregate_data() -> Dict[str, Any]:
    """Aggregates metrics and data from various sources."""
    data = {}

    # Read TODO.md
    data["TODO"] = read_file_content("TODO.md")

    # Read meta/DISCOVERIES.md
    data["DISCOVERIES"] = read_file_content("meta/DISCOVERIES.md")

    # Capture output of swarm_status.py
    print("Gathering swarm status...")
    raw_status = run_script_output("scripts/swarm_status.py")
    data["swarm_status"] = truncate_text(raw_status)

    # Pending issues
    data["pending_issues"] = get_pending_issues()

    return data


async def main():
    print("Starting periodic app review...")

    # 1. Aggregate Data
    metrics = await aggregate_data()
    print("Data aggregated. Initializing Architect...")

    # 2. Initialize Architect
    architect = Architect()

    # 3. Request Review
    if architect.is_configured:
        print("Architect configured. Requesting review...")
        review_markdown = await architect.review_app(metrics)
    else:
        print("Architect is NOT configured. Generating a mock review report.")
        # Fallback if no LLM is configured (for dry-runs / missing keys)
        review_markdown = "# Periodic App Review (Mock)\n\n## 1. Executive Summary\nThe system is running, but Architect is not configured to provide a real review.\n\n## 2. Technical Debt & Bottlenecks\nN/A\n\n## 3. Unhandled Edge Cases\nN/A\n\n## 4. Proposed Next Steps\n- Configure Architect credentials to enable full AI-driven reviews.\n"

    # 4. Save Review Report
    today_str = datetime.now().strftime("%Y-%m-%d")
    report_dir = "docs/reviews"
    os.makedirs(report_dir, exist_ok=True)
    report_filename = f"app_review_{today_str}.md"
    report_path = os.path.join(report_dir, report_filename)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(review_markdown)

    print(f"Review report generated and saved to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
