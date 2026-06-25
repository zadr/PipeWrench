#!/usr/bin/env python3
"""
Adversarial debate for a single memory issue.

Usage:
    python3 run_debate.py --provider claude --model claude-sonnet-4-20250514 [leak.json]
    python3 run_debate.py --provider openai --model gpt-4.1 [leak.json]
"""
import argparse
import json
import os
import sys
import concurrent.futures
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent.parent.parent

sys.path.insert(0, str(SCRIPTS))
from llm import chat, READONLY_TOOLS, ALL_TOOLS, configure_tool_logging


def parse_edges(issue):
    """Extract edges from the issue chain. Works for cycle_stack (legacy) or chain."""
    chain = issue.get("chain") or issue.get("cycle_stack", [])
    edges = []
    for entry in chain:
        if "CYCLE BACK" in entry:
            continue
        parts = entry.split("→")
        if len(parts) == 2:
            prop = parts[0].replace("__strong", "").strip()
            to_class = parts[1].strip()
            from_class = edges[-1]["to_class"] if edges else issue.get("root_class", "?")
            edges.append({
                "index": len(edges) + 1,
                "from_class": from_class,
                "to_class": to_class,
                "property": prop,
                "edge_desc": entry.strip(),
            })
        elif not edges:
            # First entry is just the root class name, not an edge
            pass
        else:
            # Non-edge chain entry (e.g. heap growth allocation type)
            edges.append({
                "index": len(edges) + 1,
                "from_class": entry.strip(),
                "to_class": entry.strip(),
                "property": "(allocation)",
                "edge_desc": entry.strip(),
            })
    return edges


def run_advocate(edge, chain_text, issue_desc, issue_type, provider, model):
    """Run one advocate. Returns (index, text, usage)."""
    template = (SCRIPTS / "advocate_prompt.txt").read_text()
    prompt = (template
        .replace("{{ISSUE_TYPE}}", issue_type)
        .replace("{{ISSUE_DESCRIPTION}}", issue_desc)
        .replace("{{CHAIN}}", chain_text)
        .replace("{{EDGE_DESCRIPTION}}", edge["edge_desc"])
        .replace("{{FIX_DETAIL}}", f"{edge['from_class']}.{edge['property']} should be weak or restructured")
        .replace("{{FROM_CLASS}}", edge["from_class"])
        .replace("{{TO_CLASS}}", edge["to_class"])
        .replace("{{PROPERTY_NAME}}", edge["property"])
    )
    idx = edge["index"]
    t0 = time.time()
    print(f"    [{idx}] Started: {edge['from_class']}.{edge['property']} → {edge['to_class']}", flush=True)
    text, usage = chat(prompt, provider=provider, model=model, tools=READONLY_TOOLS, cwd=str(REPO))
    elapsed = time.time() - t0
    print(f"    [{idx}] Done ({elapsed:.0f}s, {len(text)} chars, {usage.get('input_tokens',0)+usage.get('output_tokens',0)} tokens)", flush=True)
    return idx, text, usage


def run_judge(chain_text, issue_desc, issue_type, all_arguments, provider, model):
    """Run the judge. Returns (parsed_dict_or_None, raw_text, usage)."""
    template = (SCRIPTS / "judge_prompt.txt").read_text()
    prompt = (template
        .replace("{{ISSUE_TYPE}}", issue_type)
        .replace("{{ISSUE_DESCRIPTION}}", issue_desc)
        .replace("{{CHAIN}}", chain_text)
        .replace("{{ALL_ARGUMENTS}}", all_arguments)
    )
    print("    Judge deliberating...", flush=True)
    t0 = time.time()
    text, usage = chat(prompt, provider=provider, model=model, tools=None)
    elapsed = time.time() - t0
    print(f"    Judge done ({elapsed:.0f}s, {len(text)} chars)", flush=True)

    # Parse JSON
    cleaned = text.strip()
    for fence in ["```json", "```"]:
        if cleaned.startswith(fence):
            cleaned = "\n".join(cleaned.split("\n")[1:])
    if cleaned.endswith("```"):
        cleaned = "\n".join(cleaned.split("\n")[:-1])
    try:
        return json.loads(cleaned), text, usage
    except json.JSONDecodeError:
        return None, text, usage


def run_fix(leak_title, root_class, chain_text, judge_decision_json, provider, model, worktree_dir):
    """Run the fix agent in a worktree. Returns (text, usage)."""
    template = (SCRIPTS / "fix_leak_prompt.txt").read_text()
    prompt = (template
        .replace("{{LEAK_TITLE}}", leak_title)
        .replace("{{ROOT_CLASS}}", root_class)
        .replace("{{CYCLE_STACK}}", chain_text)
        .replace("{{JUDGE_DECISION}}", judge_decision_json)
    )
    print("    Fix agent working...", flush=True)
    t0 = time.time()
    text, usage = chat(prompt, provider=provider, model=model, tools=ALL_TOOLS, cwd=worktree_dir)
    elapsed = time.time() - t0
    print(f"    Fix agent done ({elapsed:.0f}s)", flush=True)
    return text, usage


def debate(leak, provider, model, debate_jobs=7, output_dir=None):
    """Run the full adversarial debate for a single issue.

    Args:
        leak: dict with id, title, root_class, leaked_bytes, leaked_nodes, cycle_stack
        provider: "claude" or "openai"
        model: model name
        debate_jobs: max concurrent advocate agents within this debate
        output_dir: directory for debate artifacts (default: artifacts/debates/<id>)

    Returns:
        (decision_dict_or_None, debate_dir, arguments, edges, total_usage)
    """
    edges = parse_edges(leak)
    chain = leak.get("chain") or leak.get("cycle_stack", [])
    chain_text = "\n".join(f"    {line}" for line in chain)
    impact_nodes = leak.get("impact_nodes") or leak.get("leaked_nodes", 0)
    impact_bytes = leak.get("impact_bytes") or leak.get("leaked_bytes", "?")
    issue_type = leak.get("issue_type", "retain_cycle")
    issue_desc = f"{leak['title']} — {impact_nodes} objects, {impact_bytes}"
    if output_dir:
        debate_dir = Path(output_dir)
    else:
        debate_dir = REPO / "artifacts" / "debates" / leak.get("id", "test")
    debate_dir.mkdir(parents=True, exist_ok=True)

    configure_tool_logging(str(debate_dir))

    total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}

    print(f"═══ Adversarial Debate: {leak['title']} ═══")
    print(f"    {len(edges)} edges, {debate_jobs} parallel advocates")
    print(f"    Provider: {provider}, Model: {model}")
    print()

    # ── Phase 1: Advocates ──
    print("── Phase 1: Advocates ──", flush=True)
    arguments = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=debate_jobs) as pool:
        futures = {
            pool.submit(run_advocate, edge, chain_text, issue_desc, issue_type, provider, model): edge
            for edge in edges
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                idx, text, usage = future.result()
                arguments[idx] = text
                (debate_dir / f"advocate_{idx}.txt").write_text(text)
                for k in total_usage:
                    total_usage[k] += usage.get(k, 0)
            except Exception as e:
                edge = futures[future]
                print(f"    [{edge['index']}] FAILED: {e}", flush=True)
                arguments[edge["index"]] = f"ERROR: {e}"

    all_arguments = ""
    for idx in sorted(arguments.keys()):
        all_arguments += f"\n--- ADVOCATE {idx}: {edges[idx-1]['from_class']}.{edges[idx-1]['property']} → {edges[idx-1]['to_class']} ---\n{arguments[idx]}\n"
    (debate_dir / "all_arguments.txt").write_text(all_arguments)
    (debate_dir / "edges.jsonl").write_text("\n".join(json.dumps(e) for e in edges))

    # ── Phase 2: Judge ──
    print()
    print("── Phase 2: Judge ──", flush=True)
    decision, raw, usage = run_judge(chain_text, issue_desc, issue_type, all_arguments, provider, model)
    (debate_dir / "judge_raw.txt").write_text(raw)
    for k in total_usage:
        total_usage[k] += usage.get(k, 0)

    if decision:
        (debate_dir / "judge_decision.json").write_text(json.dumps(decision, indent=2))
        print()
        print("── Decision ──")
        for key in ["decision", "fix_target", "confidence", "fix_description", "nil_handling", "reasoning", "risks", "dissent"]:
            val = decision.get(key, "")
            if val:
                print(f"    {key}: {str(val)[:200]}")
    else:
        print("    ⚠ Could not parse judge JSON:")
        print(raw[:500])

    print()
    print(f"── Usage ──")
    print(f"    Input:  {total_usage['input_tokens']:,} tokens")
    print(f"    Output: {total_usage['output_tokens']:,} tokens")
    print(f"    Cache:  {total_usage['cache_read']:,} read, {total_usage.get('cache_creation',0):,} created")
    print()
    print(f"═══ Done — results in {debate_dir} ═══")

    return decision, debate_dir, arguments, edges, total_usage


def main():
    parser = argparse.ArgumentParser(description="Adversarial debate for a single memory issue")
    parser.add_argument("issue_file", help="Path to issue JSON file (from analyze_memgraph.py)")
    parser.add_argument("--provider", required=True, choices=["claude", "openai"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--debate-jobs", type=int, default=7,
                        help="Max concurrent advocate agents within the debate")
    parser.add_argument("--max-parallel", type=int, default=None,
                        help="Deprecated: use --debate-jobs instead")
    parser.add_argument("--output-dir",
                        help="Directory for debate artifacts (default: artifacts/debates/<id>)")
    args = parser.parse_args()

    debate_jobs = args.max_parallel if args.max_parallel is not None else args.debate_jobs
    leak = json.load(open(args.issue_file))
    debate(leak, args.provider, args.model, debate_jobs,
           output_dir=args.output_dir)


if __name__ == "__main__":
    main()
