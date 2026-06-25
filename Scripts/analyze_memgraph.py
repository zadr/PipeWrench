#!/usr/bin/env python3
"""
Analyze a .memgraph file and extract individual memory issues.

Runs the full chain:
  1. analyze_memgraph.sh → raw report
  2. analyze_memgraph_prompt.txt + LLM → structured analysis
  3. extract_issues_prompt.txt + LLM → individual issue JSON files

Usage:
    python3 analyze_memgraph.py --provider claude --model claude-opus-4-7 /path/to/file.memgraph
    python3 analyze_memgraph.py --provider claude --model claude-opus-4-7 /path/to/file.memgraph --output-dir /tmp/issues

Outputs one JSON file per issue to <output-dir>/<memgraph-stem>/issue_<N>.json
Prints the output directory path to stdout on completion.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from llm import chat


def run_raw_analysis(memgraph_path):
    """Run analyze_memgraph.sh and return raw report text."""
    script = SCRIPTS / "analyze_memgraph.sh"
    r = subprocess.run(
        [str(script), str(memgraph_path)],
        capture_output=True, text=True,
    )
    return r.stdout + r.stderr


def run_llm_analysis(raw_report, provider, model):
    """Send raw report through analyze_memgraph_prompt.txt."""
    prompt_template = (SCRIPTS / "analyze_memgraph_prompt.txt").read_text()
    prompt = f"{prompt_template}\n\n--- REPORT ---\n{raw_report}"
    text, usage = chat(prompt, provider=provider, model=model)
    return text, usage


def extract_issues(analysis_text, provider, model):
    """Send analysis through extract_issues_prompt.txt. Returns list of dicts."""
    prompt_template = (SCRIPTS / "extract_issues_prompt.txt").read_text()
    prompt = f"{prompt_template}\n{analysis_text}"
    text, usage = chat(prompt, provider=provider, model=model)

    # Parse JSON from response
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = "\n".join(cleaned.split("\n")[1:])
    if cleaned.startswith("```"):
        cleaned = "\n".join(cleaned.split("\n")[1:])
    if cleaned.endswith("```"):
        cleaned = "\n".join(cleaned.split("\n")[:-1])
    return json.loads(cleaned), usage


def main():
    parser = argparse.ArgumentParser(description="Analyze a .memgraph file and extract memory issues")
    parser.add_argument("memgraph", help="Path to .memgraph file")
    parser.add_argument("--provider", required=True, choices=["claude", "openai"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", default="/tmp/memgraph-analysis", help="Base output directory")
    args = parser.parse_args()

    memgraph = Path(args.memgraph)
    if not memgraph.exists():
        print(f"Error: {memgraph} not found", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir) / memgraph.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Raw report
    print(f"  Raw analysis: {memgraph.name}...", flush=True)
    raw_report = run_raw_analysis(memgraph)
    (output_dir / "raw_report.txt").write_text(raw_report)
    print(f"    → {len(raw_report.splitlines())} lines")

    # Step 2: LLM analysis
    print(f"  LLM analysis...", flush=True)
    analysis, usage1 = run_llm_analysis(raw_report, args.provider, args.model)
    (output_dir / "analysis.txt").write_text(analysis)
    print(f"    → {len(analysis)} chars")

    # Step 3: Extract issues
    print(f"  Extracting issues...", flush=True)
    issues, usage2 = extract_issues(analysis, args.provider, args.model)
    print(f"    → {len(issues)} issues")

    for i, issue in enumerate(issues, 1):
        issue_path = output_dir / f"issue_{i}.json"
        issue_path.write_text(json.dumps(issue, indent=2))
        title = issue.get("title", "unknown")
        print(f"    {i}. {title} → {issue_path}")

    print(f"\n  Output: {output_dir}")
    # Print the directory to stdout so callers can capture it
    print(output_dir)


if __name__ == "__main__":
    main()
