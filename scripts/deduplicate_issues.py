import asyncio
import json
import os
import sys
import glob

# Get the directory containing this script
current_dir = os.path.dirname(os.path.abspath(__file__))
# Add parent directory of current_dir to sys.path so we can import 'src'
sys.path.append(os.path.dirname(current_dir))

try:
    from src.liminal_bridge.architect import Architect
except ImportError:
    print("Error: Could not import Architect from src.liminal_bridge.architect")
    sys.exit(1)

async def main():
    issues_dir = os.path.join(os.path.dirname(current_dir), ".github", "issues")
    if not os.path.exists(issues_dir):
        print(f"No issues directory found at {issues_dir}")
        sys.exit(0)

    issue_files = glob.glob(os.path.join(issues_dir, "*.md"))
    if len(issue_files) < 2:
        print("Not enough issues to deduplicate.")
        sys.exit(0)

    summaries = []
    for fpath in issue_files:
        try:
            with open(fpath, "r") as f:
                content = f.read()
                # Take first 1000 chars as summary
                summary = content[:1000]
                summaries.append({
                    "filename": os.path.basename(fpath),
                    "content": summary
                })
        except Exception as e:
            print(f"Error reading {fpath}: {e}")

    architect = Architect()
    # Check if Architect is configured
    if not architect.client and not architect.google_model:
        print("Warning: Architect not configured. Skipping deduplication.")
        sys.exit(0)

    print(f"Analyzing {len(summaries)} issues for duplicates...")
    try:
        response_json = await architect.deduplicate_issues(json.dumps(summaries, indent=2))
        # Clean response if it contains markdown code blocks
        if "```json" in response_json:
            response_json = response_json.split("```json")[1].split("```")[0].strip()
        elif "```" in response_json:
             response_json = response_json.split("```")[1].split("```")[0].strip()

        result = json.loads(response_json)
        duplicates = result.get("duplicates", [])

        if not duplicates:
            print("No duplicates found.")
            sys.exit(0)

        for group in duplicates:
            keep_file = group.get("keep")
            files_to_merge = group.get("merge", [])
            reason = group.get("reason", "No reason provided")

            if not keep_file or not files_to_merge:
                continue

            # Security Check: Ensure filenames are valid basenames
            if os.path.basename(keep_file) != keep_file or keep_file == "." or keep_file == "..":
                 print(f"Error: Invalid filename {keep_file} returned by LLM. Skipping group.")
                 continue

            valid_merge_files = []
            for mf in files_to_merge:
                if os.path.basename(mf) != mf or mf == "." or mf == "..":
                    print(f"Error: Invalid filename {mf} returned by LLM. Skipping file.")
                    continue
                valid_merge_files.append(mf)
            files_to_merge = valid_merge_files

            if not files_to_merge:
                continue

            keep_path = os.path.join(issues_dir, keep_file)
            if not os.path.exists(keep_path):
                print(f"Error: Target file {keep_file} not found. Skipping group.")
                continue

            print(f"Keeping {keep_file}. Merging: {files_to_merge}. Reason: {reason}")

            # Read content of keep_file
            with open(keep_path, "r") as f:
                combined_content = f.read()

            merged_list_str = ""
            for merge_file in files_to_merge:
                merge_path = os.path.join(issues_dir, merge_file)
                if os.path.exists(merge_path):
                    with open(merge_path, "r") as f:
                        content = f.read()
                        combined_content += "\n\n---\n# Content from duplicate issue (" + merge_file + ")\n" + content
                        merged_list_str += f"- {merge_file}\n"

            # Refine the combined content
            print(f"Refining combined content for {keep_file}...")
            try:
                refined_body = await architect.refine_issue(combined_content)

                # Add a note about merged issues and refinement marker
                if merged_list_str:
                     refined_body += f"\n\n<!-- triage-merged: \n{merged_list_str.strip()}\n -->"

                if "<!-- triage-refined: architect -->" not in refined_body:
                    refined_body += "\n\n<!-- triage-refined: architect -->"

                # Write back to keep_file
                with open(keep_path, "w") as f:
                    f.write(refined_body)
                print(f"Updated {keep_file} with refined content.")

                # Now delete the merged files
                for merge_file in files_to_merge:
                    merge_path = os.path.join(issues_dir, merge_file)
                    if os.path.exists(merge_path):
                        os.remove(merge_path)
                        print(f"Deleted {merge_file}")

            except Exception as e:
                print(f"Error refining combined issue {keep_file}: {e}")
                continue

    except Exception as e:
        print(f"Error during deduplication: {str(e)}")
        # Don't fail the build if deduplication fails, just warn
        sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
