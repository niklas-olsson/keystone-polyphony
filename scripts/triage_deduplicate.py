#!/usr/bin/env python3
import asyncio
import os
import sys
import glob
import json
import re

# Add parent directory to sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(current_dir))

try:
    from src.liminal_bridge.architect import Architect
except ImportError:
    print("Error: Could not import Architect from src.liminal_bridge.architect")
    sys.exit(1)


def extract_json(text):
    """Extract JSON from potential markdown code blocks."""
    text = text.strip()
    # Try to find JSON code block
    match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return match.group(1)

    # Try generic code block if it looks like JSON
    match = re.search(r'```\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        content = match.group(1)
        if content.strip().startswith('{'):
            return content

    return text


async def main():
    issue_dir = os.path.join(os.path.dirname(current_dir), ".github/issues")
    if not os.path.exists(issue_dir):
        print(f"Issue directory {issue_dir} does not exist.")
        sys.exit(0)

    files = glob.glob(os.path.join(issue_dir, "*.md"))

    if len(files) < 2:
        print("Fewer than 2 issues found. Skipping deduplication.")
        sys.exit(0)

    issues = {}
    for f in files:
        fname = os.path.basename(f)
        try:
            with open(f, "r") as fd:
                issues[fname] = fd.read()
        except Exception as e:
            print(f"Error reading {fname}: {e}")
            continue

    architect = Architect()

    # Check if Architect is configured (simple check based on provider)
    if not architect.client and not architect.google_model:
        # If using ollama, client might be None if import failed, checked in __init__
        # But generic check:
        if architect.provider == "openai" and not architect.api_key:
            print("Architect not configured (OpenAI key missing). Skipping deduplication.")
            sys.exit(0)
        # ... other checks ...
        # We can rely on methods returning error strings or raising if not configured?
        # But let's try to proceed.

    print(f"Deduplicating {len(issues)} issues...")
    try:
        result_json_str = await architect.deduplicate_issues(issues)

        # Check for error string returned by Architect
        if result_json_str.startswith("Error"):
            print(f"Architect returned error: {result_json_str}")
            sys.exit(1)

        clean_json_str = extract_json(result_json_str)

        try:
            refined_issues = json.loads(clean_json_str)
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON response: {e}")
            print(f"Raw response: {result_json_str}")
            sys.exit(1)

        if not isinstance(refined_issues, dict):
            print("Error: Architect response is not a dictionary.")
            sys.exit(1)

        # Apply changes
        input_filenames = set(issues.keys())
        output_filenames = set(refined_issues.keys())

        # Update/Create
        for fname, content in refined_issues.items():
            # Basic sanitization of filename
            safe_fname = os.path.basename(fname)
            if not safe_fname.endswith(".md"):
                safe_fname += ".md"

            path = os.path.join(issue_dir, safe_fname)
            with open(path, "w") as fd:
                fd.write(content)
            print(f"Updated/Created {safe_fname}")

        # Delete merged/removed files
        # Only delete files that were in input and are NOT in output
        # And ensure we are deleting from issue_dir
        to_delete = input_filenames - output_filenames

        # Be careful: if output filenames changed slightly (e.g. extension), we might delete originals.
        # But instructions say "Use the most relevant filename or pick one."
        # If LLM renames "a.md" to "b.md", "a.md" is in input, not output -> deleted. "b.md" is in output -> created.
        # This is correct behavior for rename/merge.

        for fname in to_delete:
            path = os.path.join(issue_dir, fname)
            if os.path.exists(path):
                os.remove(path)
                print(f"Deleted merged issue {fname}")

    except Exception as e:
        print(f"Error during deduplication process: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
