#!/usr/bin/env python3
"""
Abstract memgraph analysis pipeline.

Subclass MemgraphPipeline and implement the abstract methods to integrate
with your CI, source control, and code review systems.
"""
import abc
import concurrent.futures
import json
import os
import re as _stdlib_re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from llm import chat, Session, READONLY_TOOLS, ALL_TOOLS, EventLog


# ---------------------------------------------------------------------------
# Pipeline report — single source of truth for all pipeline state
# ---------------------------------------------------------------------------

class PipelineReport:
    """Thread-safe, atomically-written JSON report.

    The report is the primary record of pipeline state.  Flat files
    (raw reports, advocate text, judge decisions) are still written as
    backup, but the report is what resume logic reads.

    Writes are atomic: write to a temp file in the same directory,
    then rename over the target.  This prevents partial writes from
    corrupting the file on crash.
    """

    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = {
            "last_updated": None,
            "memgraphs": {},
            "issues": {},
            "dedup_groups": [],
        }
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        """Atomically write the report to disk."""
        self._data["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent),
            suffix=".tmp",
            prefix=".report-",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2, default=str)
            os.replace(tmp, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- Memgraph records --

    def set_memgraph(self, stem, **fields):
        """Create or update a memgraph record."""
        with self._lock:
            mg = self._data["memgraphs"].setdefault(stem, {})
            mg.update(fields)
            mg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._save()

    def get_memgraph(self, stem):
        with self._lock:
            return self._data["memgraphs"].get(stem)

    # -- Issue records --

    def set_issue(self, scoped_id, **fields):
        """Create or update an issue record."""
        with self._lock:
            issue = self._data["issues"].setdefault(scoped_id, {})
            issue.update(fields)
            issue["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._save()

    def get_issue(self, scoped_id):
        with self._lock:
            return self._data["issues"].get(scoped_id)

    def all_issues(self):
        with self._lock:
            return dict(self._data["issues"])

    # -- Dedup groups --

    def set_dedup_groups(self, groups):
        with self._lock:
            self._data["dedup_groups"] = groups
            self._save()

    def get_dedup_groups(self):
        with self._lock:
            return list(self._data.get("dedup_groups", []))

    # -- Bulk read --

    @property
    def data(self):
        """Return a snapshot of the full report."""
        with self._lock:
            return json.loads(json.dumps(self._data, default=str))


# ---------------------------------------------------------------------------
# Helpers (generic)
# ---------------------------------------------------------------------------

def _ts():
    """Short timestamp for log headlines."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


_BRANCH_BAD_CHARS = _stdlib_re.compile(r'[~^:?*\[\]\\@{}\s]+')
_BRANCH_MULTI_DASH = _stdlib_re.compile(r'-{2,}')


def _sanitize_branch(name):
    """Replace characters invalid in git branch names."""
    name = _BRANCH_BAD_CHARS.sub("-", name)
    name = name.replace("..", "-")
    name = _BRANCH_MULTI_DASH.sub("-", name)
    return name.strip("-")


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                       errors="replace")
    if check and r.returncode != 0:
        print(f"  ⚠ Command failed: {cmd}\n    {r.stderr[:200]}", file=sys.stderr)
    return r.stdout.strip()


def fill_template(template_path, replacements):
    text = template_path.read_text()
    for key, val in replacements.items():
        text = text.replace(f"{{{{{key}}}}}", str(val))
    return text


def parse_json_response(text):
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = "\n".join(cleaned.split("\n")[1:])
    if cleaned.startswith("```"):
        cleaned = "\n".join(cleaned.split("\n")[1:])
    if cleaned.endswith("```"):
        cleaned = "\n".join(cleaned.split("\n")[:-1])
    return json.loads(cleaned)


def parse_edges(issue):
    chain = issue.get("chain") or issue.get("cycle_stack", [])
    edges = []
    for entry in chain:
        if "CYCLE BACK" in entry:
            continue
        parts = entry.split("→")
        if len(parts) == 2:
            prop = parts[0].replace("__strong", "").strip()
            to_class = parts[1].strip().split("(")[0].strip()
            from_class = edges[-1]["to_class"] if edges else issue.get("root_class", "?")
            edges.append({
                "index": len(edges) + 1,
                "from_class": from_class,
                "to_class": to_class,
                "property": prop,
                "edge_desc": entry.strip(),
            })
        else:
            # Non-edge entry (standalone leak class, heap allocation type, etc.)
            # Still debatable as a level to intervene at.
            edges.append({
                "index": len(edges) + 1,
                "from_class": entry.strip(),
                "to_class": entry.strip(),
                "property": "(level)",
                "edge_desc": entry.strip(),
            })
    return edges


# ---------------------------------------------------------------------------
# Memgraph deduplication
# ---------------------------------------------------------------------------

_ROOT_RE = _stdlib_re.compile(r"ROOT (?:CYCLE|LEAK):\s+(.+)")
_GENERIC_RE = _stdlib_re.compile(r"<[^>]*>")


def _extract_root_classes(report_path):
    """Extract simplified root class names from a raw analysis report."""
    classes = set()
    for line in report_path.read_text(errors="replace").splitlines():
        m = _ROOT_RE.search(line)
        if m:
            raw = m.group(1).strip()
            # Strip Combine/Swift generics for stable fingerprints
            simplified = _GENERIC_RE.sub("", raw).strip()
            # Drop framework-internal noise
            if simplified and not simplified.startswith(("_", "OS_", "CF", "NS")):
                classes.add(simplified)
    return frozenset(classes)


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 0.0


def _group_by_similarity(fingerprints, threshold=0.6):
    """Group memgraphs whose root-class sets overlap above *threshold*.

    *fingerprints* is a dict ``{stem: frozenset_of_classes}``.
    Returns a list of groups, each a list of stems.  The first stem in
    each group is the representative.
    """
    stems = list(fingerprints)
    assigned = set()
    groups = []
    for i, s in enumerate(stems):
        if s in assigned:
            continue
        group = [s]
        assigned.add(s)
        for j in range(i + 1, len(stems)):
            other = stems[j]
            if other in assigned:
                continue
            if _jaccard(fingerprints[s], fingerprints[other]) >= threshold:
                group.append(other)
                assigned.add(other)
        groups.append(group)
    return groups


# ---------------------------------------------------------------------------
# Review result container
# ---------------------------------------------------------------------------

ReviewResult = namedtuple("ReviewResult", ["decision", "arguments", "edges", "debate_dir"])


# ---------------------------------------------------------------------------
# Abstract pipeline
# ---------------------------------------------------------------------------

class MemgraphPipeline(abc.ABC):
    """Abstract base for the memgraph analysis pipeline.

    Subclasses must implement:
        download_memgraphs()   - fetch memgraphs from CI into a directory
        open_draft_pr()        - open a draft PR on the code hosting platform
        post_review_loop()     - platform-specific code review + CI loop

    The pipeline streams each memgraph through:
        LLM analysis → issue extraction → adversarial review → fix
    instead of batching all memgraphs through one stage at a time.

    Concurrency is controlled by three knobs:
        --analysis-jobs N   max concurrent memgraphs being analyzed at once
        --fix-jobs M        max concurrent fix agents (code changes, PRs)
        --debate-jobs K     max concurrent advocates within a single debate
    """

    def __init__(self, provider, model, repo_dir, analysis_jobs=5, fix_jobs=2,
                 debate_jobs=7, once=False,
                 skip_downloading_artifacts=False, skip_collecting_memgraphs=False,
                 skip_raw_analysis=False, skip_llm_analysis=False,
                 skip_duplicate_analysis=False,
                 memgraph_name=None, preserve_decision_record=False,
                 artifact_path=None):
        self.provider = provider
        self.model = model
        self.repo_dir = Path(repo_dir)
        self.artifacts_dir = (Path(artifact_path) if artifact_path
                              else Path(repo_dir) / "artifacts").resolve()
        self.report = PipelineReport(self.artifacts_dir / "report.json")
        self.analysis_jobs = analysis_jobs
        self.fix_jobs = fix_jobs
        self.debate_jobs = debate_jobs
        self.once = once
        self.skip_downloading_artifacts = skip_downloading_artifacts
        self.skip_collecting_memgraphs = skip_collecting_memgraphs
        self.skip_raw_analysis = skip_raw_analysis
        self.skip_llm_analysis = skip_llm_analysis
        self.skip_duplicate_analysis = skip_duplicate_analysis
        # Normalize: strip .memgraph extension if provided
        self._memgraph_filter = memgraph_name.replace(".memgraph", "") if memgraph_name else None
        self.preserve_decision_record = preserve_decision_record

        # Concurrency primitives
        self._analysis_sem = threading.Semaphore(analysis_jobs)
        self._debate_sem = threading.Semaphore(debate_jobs)
        self._fix_sem = threading.Semaphore(fix_jobs)
        self._print_lock = threading.Lock()
        self._cancel = threading.Event()       # set by --once after first PR
        self._review_workers = []
        self._review_lock = threading.Lock()
        # Issue-level dedup — two strategies by issue type:
        #   retain_cycle/standalone_leak: dedup at extraction (chain is the bug)
        #   heap_growth/fragmentation/autorelease_bloat: dedup after debate (need fix_target)
        self._known_issues = []            # extracted issues, persisted to disk
        self._known_issues_lock = threading.Lock()
        self._known_issues_file = self.artifacts_dir / "extracted_issues.jsonl"
        self._known_fix_targets = set()    # judge fix_targets, for heap-type dedup
        self._known_fix_targets_lock = threading.Lock()
        self._load_known_dedup_state()

    # -- Abstract methods (CI/platform-specific) --

    @abc.abstractmethod
    def download_memgraphs(self, output_dir):
        """Download memgraphs into output_dir. Return Path to memgraphs subdir."""

    @abc.abstractmethod
    def open_draft_pr(self, branch_name, title, body):
        """Open a draft PR. Return an identifier (e.g. PR number or URL)."""

    @abc.abstractmethod
    def post_review_loop(self, pr_id, branch_name, issue_title, session):
        """Run code review + CI loop until the PR is ready to merge.

        session is the Session from the fix agent, preserving full context
        about what was changed and why.
        """

    # -- Skip already-completed work --

    def _find_existing_pr(self, branch_name):
        """Return the PR id/number if *branch_name* already has a PR.

        Subclasses should override with a platform-specific check (e.g. ``gh
        pr view``).  The default implementation checks whether the branch
        exists on the remote, which is a cheap proxy.

        Returns a truthy identifier string, or ``None``.
        """
        ref = run(f"git ls-remote --heads origin {branch_name}",
                  cwd=str(self.repo_dir), check=False)
        return branch_name if ref else None

    def _get_pr_state(self, pr_id):
        """Return the PR state: OPEN, MERGED, or CLOSED.

        Subclasses should override.  Default assumes open.
        """
        return "OPEN"

    def _find_branch_for_issue(self, issue_id):
        """Check if any remote branch ending with *issue_id* already exists.

        Catches branches from other memgraph prefixes or pre-scoping runs.
        """
        refs = run(f"git ls-remote --heads origin 'fix/memory-leak-*{issue_id}'",
                   cwd=str(self.repo_dir), check=False)
        if refs:
            # Return the first matching branch name
            first_line = refs.strip().splitlines()[0]
            return first_line.split("refs/heads/")[-1]
        return None

    # -- Logging --

    def _log(self, label, msg):
        """Thread-safe, labeled print."""
        with self._print_lock:
            print(f"  [{label}] {msg}", flush=True)

    # -- PR body --

    def build_pr_body(self, issue, decision, edges, arguments, memgraph_file):
        """Build the PR description."""
        issue_title = issue.get("title", "Unknown")
        severity = issue.get("severity", "unknown")
        impact_nodes = issue.get("impact_nodes") or issue.get("leaked_nodes", 0)
        impact_bytes = issue.get("impact_bytes") or issue.get("leaked_bytes", "?")
        root_class = issue.get("root_class", "Unknown")
        chain = issue.get("chain") or issue.get("cycle_stack", [])
        chain_lines = []
        depth = 0
        for line in chain:
            chain_lines.append("  " * depth + line)
            if "CYCLE BACK" not in line:
                depth += 1
        chain_text = "\n".join(chain_lines)
        confidence = decision.get("confidence", "low")
        d = decision.get("decision", "?")
        fix_target = decision.get("fix_target", "")
        mg_name = memgraph_file.name if memgraph_file else "unknown"

        advocate_sections = ""
        for idx in sorted(arguments):
            e = edges[idx - 1]
            label = f"{e['from_class']}.{e['property']} → {e['to_class']}"
            advocate_sections += (
                f"<details>\n<summary>Advocate {idx}: <code>{label}</code></summary>\n\n"
                f"{arguments[idx]}\n\n</details>\n\n"
            )

        return f"""## 🧠 Memory Fix: {issue_title}

1. **Severity:** {severity}
2. **Impact:** {impact_nodes} objects, {impact_bytes}
3. **Root class:** `{root_class}`
4. **Judge confidence:** {confidence}
5. **Memgraph:** `{mg_name}`

### Reference Chain

```
{chain_text}
```

### Fix Applied

1. **Decision:** {d}
2. **Target:** `{fix_target}`

{decision.get('fix_description', '')}

### Nil Handling

{decision.get('nil_handling', '')}

### Why This Fix Point

{decision.get('reasoning', '')}

### Risks

{decision.get('risks', '')}

### Strongest Counter-Argument

{decision.get('dissent', '')}

---

### Adversarial Review

{len(edges)} advocate agents each argued for a different fix point. A senior engineer agent reviewed all arguments and made the final call.

{advocate_sections}

<details>
<summary>🧑‍⚖️ Judge Decision (raw)</summary>

```json
{json.dumps(decision, indent=2)}
```

</details>

---
*Generated automatically by the memgraph analysis pipeline (adversarial review).*"""

    # -----------------------------------------------------------------------
    # Pipeline orchestration
    # -----------------------------------------------------------------------

    def run_pipeline(self):
        """Run the full pipeline."""
        artifacts_dir = self.artifacts_dir
        memgraphs_dir = artifacts_dir / "memgraphs"
        reports_dir = artifacts_dir / "reports"

        if not self.skip_downloading_artifacts:
            memgraphs_dir = self.download_memgraphs(artifacts_dir)
        else:
            print(f"═══ Skipping artifact download ═══  {_ts()}")

        if not self.skip_collecting_memgraphs:
            memgraphs = sorted(memgraphs_dir.glob("*.memgraph"))
            if not memgraphs:
                print("  No memgraph files found. Nothing to analyze.")
                return
            print(f"  Found {len(memgraphs)} .memgraph files\n")
        else:
            print(f"═══ Skipping memgraph collection ═══  {_ts()}")
            memgraphs = sorted(memgraphs_dir.glob("*.memgraph"))

        if self._memgraph_filter:
            memgraphs = [m for m in memgraphs if m.stem == self._memgraph_filter]
            if not memgraphs:
                print(f"  No memgraph matching '{self._memgraph_filter}' found.")
                return
            print(f"  Filtered to: {memgraphs[0].name}\n")

        if not self.skip_raw_analysis:
            self._step_raw_analysis(memgraphs, reports_dir)
        else:
            print(f"═══ Skipping raw analysis ═══  {_ts()}")

        # Streaming: each memgraph flows through analysis → issues → review → fix
        # Issue-level dedup happens inside the pipeline, not at the memgraph level.
        self._run_streaming_pipeline(memgraphs, reports_dir, memgraphs_dir)

        # Wait for review workers (post-PR CI loops).
        # _run_streaming_pipeline blocks until every memgraph/issue thread has
        # finished, which means every _spawn_review_worker call has completed
        # its append.  No new workers can appear — safe to read without lock.
        if self._review_workers:
            print(f"\n═══ Waiting for {len(self._review_workers)} review workers ═══  {_ts()}")
            for t in self._review_workers:
                t.join()

        print(f"\n═══ Done ═══  {_ts()}")
        print(f"  Artifacts:   {artifacts_dir}")
        print(f"  Memgraphs:   {memgraphs_dir}")
        print(f"  Reports:     {reports_dir}")
        print(f"  Debates:     {artifacts_dir / 'debates'}")
        print(f"  Total:       {len(memgraphs)} memgraph files analyzed")

    def _step_raw_analysis(self, memgraphs, reports_dir):
        """Run vmmap/heap/leaks on each memgraph (local tools, not LLM)."""
        print(f"═══ Raw analysis ═══  {_ts()}")
        reports_dir.mkdir(parents=True, exist_ok=True)
        analyze_sh = SCRIPTS / "analyze_memgraph.sh"
        for i, mg in enumerate(memgraphs, 1):
            report = reports_dir / f"{mg.stem}.txt"
            print(f"  [{i}/{len(memgraphs)}] {mg.name}...")
            result = subprocess.run([str(analyze_sh), str(mg)], capture_output=True, text=True)
            raw_text = result.stdout + result.stderr
            report.write_text(raw_text)
            self.report.set_memgraph(mg.stem,
                                     raw_report=raw_text,
                                     raw_report_at=_ts())
            print(f"    → {len(result.stdout.splitlines())} lines")
        print()

    # -----------------------------------------------------------------------
    # Deduplication
    # -----------------------------------------------------------------------

    # Issue-level dedup
    # -----------------------------------------------------------------------

    _STRUCTURAL_TYPES = frozenset(("retain_cycle", "standalone_leak"))
    _HEAP_TYPES = frozenset(("heap_growth", "fragmentation", "autorelease_bloat"))

    def _load_known_dedup_state(self):
        """Load previously extracted issues and judge fix targets from disk."""
        # Extracted issues (for structural dedup at extraction time)
        if self._known_issues_file.exists():
            for line in self._known_issues_file.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        self._known_issues.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # Judge fix targets (for heap-type dedup after debate)
        debates_dir = self.artifacts_dir / "debates"
        if debates_dir.exists():
            for jf in debates_dir.glob("*/judge_decision.json"):
                try:
                    decision = json.loads(jf.read_text())
                    ft = decision.get("fix_target", "")
                    if ft:
                        self._known_fix_targets.add(ft)
                except (json.JSONDecodeError, OSError):
                    continue

        if self._known_issues or self._known_fix_targets:
            print(f"  Loaded {len(self._known_issues)} known issues, "
                  f"{len(self._known_fix_targets)} known fix targets")

    def _persist_issues(self, issues, mg_stem):
        """Append extracted issues to the known-issues file and in-memory list."""
        self._known_issues_file.parent.mkdir(parents=True, exist_ok=True)
        with self._known_issues_lock:
            with open(self._known_issues_file, "a") as f:
                for issue in issues:
                    tagged = {**issue, "_memgraph": mg_stem}
                    f.write(json.dumps(tagged, default=str) + "\n")
            self._known_issues.extend(issues)

    def _load_saved_issues(self, mg_stem):
        """Load previously extracted issues for a memgraph from the JSONL."""
        with self._known_issues_lock:
            return [i for i in self._known_issues
                    if i.get("_memgraph") == mg_stem]

    def _known_issues_for_extraction(self):
        """Return a summary of known structural issues for the extraction prompt."""
        with self._known_issues_lock:
            structural = [i for i in self._known_issues
                          if (i.get("issue_type") or i.get("root_type", ""))
                          in self._STRUCTURAL_TYPES]
        if not structural:
            return ""
        lines = ["\n\n--- ALREADY KNOWN ISSUES (do NOT extract duplicates of these) ---\n"]
        for i in structural:
            lines.append(json.dumps({
                "title": i.get("title", ""),
                "issue_type": i.get("issue_type", ""),
                "root_class": i.get("root_class", ""),
                "chain": i.get("chain") or i.get("cycle_stack", []),
            }))
        return "\n".join(lines)

    def _is_known_structural_issue(self, issue):
        """Check if a retain_cycle/standalone_leak matches one already extracted.

        Compares (root_class, chain class overlap) as a fast heuristic.
        The extraction prompt also tells the LLM to skip known issues,
        so this is a safety net for cases the LLM misses.
        """
        root_class = issue.get("root_class", "")
        chain = issue.get("chain") or issue.get("cycle_stack", [])
        chain_classes = set()
        for entry in chain:
            for part in entry.replace("→", " ").split():
                stripped = part.strip("()")
                if stripped and stripped[0].isupper() and not stripped.startswith(("0x", "CYCLE")):
                    chain_classes.add(_GENERIC_RE.sub("", stripped).strip())

        with self._known_issues_lock:
            for known in self._known_issues:
                if known is issue:
                    continue
                known_type = known.get("issue_type") or known.get("root_type", "")
                if known_type not in self._STRUCTURAL_TYPES:
                    continue
                if known.get("root_class", "") != root_class:
                    continue
                known_chain = known.get("chain") or known.get("cycle_stack", [])
                known_classes = set()
                for entry in known_chain:
                    for part in entry.replace("→", " ").split():
                        stripped = part.strip("()")
                        if stripped and stripped[0].isupper() and not stripped.startswith(("0x", "CYCLE")):
                            known_classes.add(_GENERIC_RE.sub("", stripped).strip())
                if chain_classes and known_classes and _jaccard(chain_classes, known_classes) >= 0.5:
                    return True
        return False

    def _is_known_fix_target(self, fix_target, label):
        """Check if a judge's fix_target overlaps with one already decided.

        Uses an LLM call to compare — file/function names can differ in
        phrasing while pointing at the same code.
        """
        if not fix_target:
            return False

        with self._known_fix_targets_lock:
            targets = list(self._known_fix_targets)

        if not targets:
            with self._known_fix_targets_lock:
                self._known_fix_targets.add(fix_target)
            return False

        for known_target in targets:
            prompt = (
                "You are deduplicating memory fix targets in an iOS codebase.\n\n"
                "Would fixing Target A also address Target B (same file, same "
                "function, or one subsumes the other)?\n\n"
                f"### Target A (already being fixed)\n{known_target}\n\n"
                f"### Target B (new candidate)\n{fix_target}\n\n"
                "Reply with ONLY the word YES or NO."
            )
            for attempt in range(1, 4):
                if attempt > 1:
                    prompt = (
                        f"You did not answer YES or NO.\n\n"
                        f"Would fixing Target A also fix Target B?\n"
                        f"A: {known_target}\nB: {fix_target}\n\n"
                        f"Reply ONLY YES or NO."
                    )
                with self._analysis_sem:
                    response, _ = chat(prompt, provider=self.provider,
                                       model=self.model)
                answer = response.strip().upper()
                if answer.startswith("YES"):
                    self._log(label, f"Fix target already covered: {known_target[:80]}")
                    return True
                if answer.startswith("NO"):
                    break

        # New target — register it
        with self._known_fix_targets_lock:
            self._known_fix_targets.add(fix_target)
        return False

    # -----------------------------------------------------------------------
    # Streaming pipeline
    # -----------------------------------------------------------------------

    def _run_streaming_pipeline(self, memgraphs, reports_dir, memgraphs_dir):
        """Stream memgraphs through analysis → issues → review → fix.

        Each memgraph is submitted to a thread pool sized for the full
        pipeline width.  Work flows through three independent stages —
        analysis, debate, fix — each with its own semaphore, so a memgraph
        finishing analysis can immediately start debating while the analysis
        slot opens for the next memgraph.
        """
        print(f"═══ Streaming pipeline ({self.analysis_jobs} analysis, "
              f"{self.fix_jobs} fix, {self.debate_jobs} debate) ═══  {_ts()}")

        pipeline_width = self.analysis_jobs + self.debate_jobs + self.fix_jobs
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=pipeline_width,
        ) as pool:
            futures = []
            for mg in memgraphs:
                if self._cancel.is_set():
                    break
                futures.append(pool.submit(
                    self._stream_memgraph, mg, reports_dir, memgraphs_dir,
                ))
            for f in concurrent.futures.as_completed(futures):
                try:
                    f.result()
                except Exception as exc:
                    self._log("pipeline", f"⚠ Memgraph thread failed: {exc}")

    # -- per-memgraph -------------------------------------------------------

    def _stream_memgraph(self, mg, reports_dir, memgraphs_dir):
        """Analyse one memgraph, extract issues, fan out reviews + fixes."""
        if self._cancel.is_set():
            return

        label = mg.stem[:8]
        analysis_file = reports_dir / f"{mg.stem}_analysis.txt"

        # 1. LLM analysis
        if not self.skip_llm_analysis:
            raw_report = reports_dir / f"{mg.stem}.txt"
            if not raw_report.exists():
                self._log(label, "⚠ No raw report, skipping")
                return
            self._log(label, "LLM analysis")
            prompt_template = (SCRIPTS / "analyze_memgraph_prompt.txt").read_text()
            prompt = f"{prompt_template}\n\n--- REPORT ---\n{raw_report.read_text()}"
            with self._analysis_sem:
                text, _ = chat(prompt, provider=self.provider, model=self.model)
            analysis_file.write_text(text)
            self.report.set_memgraph(mg.stem,
                                     analysis=text,
                                     analysis_at=_ts())
            self._log(label, "LLM analysis complete")

        if not analysis_file.exists():
            self._log(label, "⚠ No analysis file (re-run without --skip-llm-analysis)")
            return

        # 2. Extract issues
        if self.skip_duplicate_analysis:
            saved = self._load_saved_issues(mg.stem)
            if saved:
                self._log(label, f"Reusing {len(saved)} saved issues")
                issues = saved
            else:
                self._log(label, "No saved issues for this memgraph, extracting")
                issues = self._extract_issues(analysis_file, label)
                if not issues:
                    self._log(label, "No actionable issues")
                    return
                self._log(label, f"Found {len(issues)} issues")
        else:
            self._log(label, "Extracting issues")
            issues = self._extract_issues(analysis_file, label)
            if not issues:
                self._log(label, "No actionable issues")
                return
            self._log(label, f"Found {len(issues)} issues")

        # 3. Persist extracted issues and filter structural dupes
        #    (retain_cycle/standalone_leak only — heap types dedup after debate)
        if not saved:
            self._persist_issues(issues, mg.stem)
        issue_ids = []
        for issue in issues:
            scoped_id = _sanitize_branch(f"{mg.stem[:8]}-{issue.get('id', 'unknown')}")
            issue_ids.append(scoped_id)
            self.report.set_issue(scoped_id,
                                  id=issue.get("id"),
                                  memgraph=mg.stem,
                                  title=issue.get("title"),
                                  issue_type=issue.get("issue_type"),
                                  root_class=issue.get("root_class"),
                                  severity=issue.get("severity"),
                                  impact_bytes=issue.get("impact_bytes"),
                                  impact_nodes=issue.get("impact_nodes"),
                                  chain=issue.get("chain") or issue.get("cycle_stack"),
                                  fix_point=issue.get("fix_point"),
                                  status="extracted",
                                  extracted_at=_ts())
        self.report.set_memgraph(mg.stem, issues=issue_ids)

        structural_types = ("retain_cycle", "standalone_leak")
        unique_issues = []
        for issue in issues:
            itype = issue.get("issue_type") or issue.get("root_type", "")
            scoped_id = _sanitize_branch(f"{mg.stem[:8]}-{issue.get('id', 'unknown')}")
            if itype in structural_types and self._is_known_structural_issue(issue):
                self._log(label, f"⏭ Duplicate: {issue.get('title', '?')}")
                self.report.set_issue(scoped_id,
                                      status="skipped_dupe",
                                      dedup_reason="extraction_chain_match")
                continue
            unique_issues.append(issue)

        if not unique_issues:
            self._log(label, "All issues are duplicates of earlier memgraphs")
            return

        if len(unique_issues) < len(issues):
            self._log(label, f"{len(unique_issues)}/{len(issues)} unique issues")

        # 4. Fan out: one thread per issue
        memgraph_file = next(memgraphs_dir.glob(f"{mg.stem}.memgraph"), None)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.debate_jobs + self.fix_jobs,
        ) as pool:
            futures = []
            for i, issue in enumerate(unique_issues, 1):
                if self._cancel.is_set():
                    break
                issue_label = f"{label}/{i}"
                futures.append(pool.submit(
                    self._stream_issue, issue, memgraph_file, mg.stem,
                    issue_label,
                ))
            for f in concurrent.futures.as_completed(futures):
                try:
                    f.result()
                except Exception as exc:
                    self._log(label, f"⚠ Issue thread failed: {exc}")

    # -- per-issue ----------------------------------------------------------

    def _stream_issue(self, issue, memgraph_file, mg_stem, label):
        """Adversarial review → fix → PR for one issue."""
        if self._cancel.is_set():
            return

        issue_title = issue.get("title", "Unknown")
        self._log(label, f"Review: {issue_title}")

        try:
            review_result = self._run_adversarial_review(issue, memgraph_file, mg_stem, label)
        except Exception as exc:
            self._log(label, f"⚠ Review failed: {exc}")
            return
        if not review_result:
            self._log(label, "⚠ No decision from review")
            return

        d = review_result.decision.get("decision", "?")
        confidence = review_result.decision.get("confidence", "low")
        fix_target = review_result.decision.get("fix_target", "")
        self._log(label, f"Decision: {d} → {fix_target} (confidence: {confidence})")

        # Decision E: not worth fixing
        if d == "E":
            reason = review_result.decision.get("reason_not_to_fix", "")
            self._log(label, f"⏭ Not worth fixing: {reason[:100]}")
            scoped_id = _sanitize_branch(f"{mg_stem[:8]}-{issue.get('id', 'unknown')}")
            self.report.set_issue(scoped_id,
                                  status="skipped_not_worth_fixing",
                                  reason_not_to_fix=reason)
            return

        # Check if a branch/PR already exists for this issue.
        issue_id = issue.get("id", "unknown")
        scoped_id = _sanitize_branch(f"{mg_stem[:8]}-{issue_id}")
        branch_name = f"fix/memory-leak-{scoped_id}"
        # Search for any existing branch for this issue (scoped, unscoped,
        # or from another memgraph prefix).
        existing_branch = None
        for candidate in (branch_name, f"fix/memory-leak-{issue_id}"):
            ref = run(f"git ls-remote --heads origin {candidate}",
                      cwd=str(self.repo_dir), check=False)
            if ref:
                existing_branch = candidate
                break
        if not existing_branch:
            existing_branch = self._find_branch_for_issue(issue_id)

        if existing_branch:
            # Branch exists — check if it has a PR
            existing_pr = self._find_existing_pr(existing_branch)
            if existing_pr:
                pr_state = self._get_pr_state(existing_pr)
                if pr_state in ("MERGED", "CLOSED"):
                    self._log(label, f"⏭ PR #{existing_pr} is {pr_state.lower()}, skipping")
                    return
                # PR is still open/draft — resume the review cycle
                self._log(label, f"🔄 PR #{existing_pr} still open, resuming review")
                review_session = Session(
                    provider=self.provider, model=self.model, tools=ALL_TOOLS,
                    cwd=str(self.repo_dir),
                )
                self._spawn_review_worker(
                    existing_pr, existing_branch, issue_title,
                    review_session, debate_dir=None, label=label,
                )
                return
            else:
                # Branch exists but no PR — open one
                self._log(label, f"🔧 Branch {existing_branch} exists without PR, opening one")
                pr_body = self.build_pr_body(
                    issue, review_result.decision, review_result.edges,
                    review_result.arguments, memgraph_file)
                pr_id = self.open_draft_pr(
                    existing_branch, f"Fix memory issue: {issue_title}", pr_body)
                if pr_id:
                    self._log(label, f"PR: {pr_id}")
                    review_session = Session(
                        provider=self.provider, model=self.model, tools=ALL_TOOLS,
                        cwd=str(self.repo_dir),
                    )
                    self._spawn_review_worker(
                        pr_id, existing_branch, issue_title,
                        review_session, debate_dir=None, label=label,
                    )
                return

        # Heap-type dedup: now that we have fix_target and no existing
        # branch/PR, check if another issue already targets the same code.
        issue_type = issue.get("issue_type") or issue.get("root_type", "")
        if issue_type in self._HEAP_TYPES and fix_target:
            if self._is_known_fix_target(fix_target, label):
                self._log(label, f"⏭ Fix target already covered, skipping")
                self.report.set_issue(scoped_id,
                                      status="skipped_dupe",
                                      dedup_reason="fix_target_overlap",
                                      dedup_detail=f"fix_target '{fix_target[:80]}' overlaps with known target")
                return

        # Fix (blocks until a fix slot opens)
        try:
            with self._fix_sem:
                self._run_fix_and_pr(issue, memgraph_file, mg_stem, review_result, label)
        except Exception as exc:
            self._log(label, f"⚠ Fix failed: {exc}")

    # -- issue extraction ---------------------------------------------------

    def _extract_issues(self, analysis_file, label):
        """Parse actionable issues from an LLM analysis file."""
        known_section = self._known_issues_for_extraction()
        extract_prompt = (
            (SCRIPTS / "extract_issues_prompt.txt").read_text()
            + known_section
            + "\n" + analysis_file.read_text()
        )
        issues_text = ""
        for attempt in range(1, 6):
            if attempt == 1:
                prompt = extract_prompt
            else:
                self._log(label, f"Retrying extraction (attempt {attempt})")
                prompt = (
                    f"{extract_prompt}\n\n"
                    f"Your previous response could not be parsed as JSON. "
                    f"You MUST output ONLY a valid JSON array. No other text."
                )
            with self._analysis_sem:
                issues_text, _ = chat(prompt, provider=self.provider,
                                      model=self.model)
            try:
                return parse_json_response(issues_text)
            except json.JSONDecodeError:
                continue
        self._log(label, "⚠ Failed to extract issues after 5 attempts")
        return []

    # -- adversarial review -------------------------------------------------

    def _run_adversarial_review(self, issue, memgraph_file, mg_stem, label):
        """Run advocate + judge debate for one issue.

        Returns ``ReviewResult(decision, arguments, edges, debate_dir)`` or
        ``None``.
        """
        issue_id = issue.get("id", "unknown")
        # Scope debate dir and branch name by memgraph so that identical
        # issue ids from different memgraphs don't collide.
        scoped_id = _sanitize_branch(f"{mg_stem[:8]}-{issue_id}")
        issue_type = issue.get("issue_type") or issue.get("root_type", "retain_cycle")
        impact_nodes = issue.get("impact_nodes") or issue.get("leaked_nodes", 0)
        impact_bytes = issue.get("impact_bytes") or issue.get("leaked_bytes", "?")
        chain = issue.get("chain") or issue.get("cycle_stack", [])
        chain_text = "\n".join(f"    {line}" for line in chain)
        issue_desc = (f"{issue.get('title', 'Unknown')} — "
                      f"{impact_nodes} objects, {impact_bytes}")
        edges = parse_edges(issue)

        debates_root = self.artifacts_dir / "debates"
        debates_root.mkdir(parents=True, exist_ok=True)
        debate_dir = debates_root / scoped_id

        # Reuse preserved decision?
        if (self.preserve_decision_record
                and (debate_dir / "judge_decision.json").exists()):
            self._log(label, "Reusing saved decision")
            decision = json.loads(
                (debate_dir / "judge_decision.json").read_text())
            arguments = {}
            for idx, _e in enumerate(edges, 1):
                af = debate_dir / f"advocate_{idx}.txt"
                arguments[idx] = af.read_text() if af.exists() else ""
            return ReviewResult(decision, arguments, edges, debate_dir)

        # -- Fresh debate ----------------------------------------------------
        debate_dir.mkdir(parents=True, exist_ok=True)
        # NOTE: We intentionally do NOT call configure_tool_logging() here.
        # It sets a process-global path that races when multiple issues run
        # concurrently.  Per-agent EventLog objects already capture tool calls
        # in structured JSONL, which is more useful for debugging anyway.

        # --- Advocates (parallel, capped at debate_jobs) ---
        self.report.set_issue(scoped_id,
                              status="debating",
                              debate_started_at=_ts())
        self._log(label, f"Running {len(edges)} advocates")

        def _run_advocate(edge):
            idx = edge["index"]
            adv_label = f"{label}/adv{idx}"
            prompt = fill_template(SCRIPTS / "advocate_prompt.txt", {
                "ISSUE_TYPE": issue_type,
                "ISSUE_DESCRIPTION": issue_desc,
                "CHAIN": chain_text,
                "EDGE_DESCRIPTION": edge["edge_desc"],
                "FIX_DETAIL": (f"{edge['from_class']}.{edge['property']} "
                               "should be weak or restructured"),
                "FROM_CLASS": edge["from_class"],
                "TO_CLASS": edge["to_class"],
                "PROPERTY_NAME": edge["property"],
            })
            advocate_log = EventLog(str(debate_dir / f"advocate_{idx}.jsonl"))
            self._log(adv_label,
                      f"{edge['from_class']}.{edge['property']} → "
                      f"{edge['to_class']}")
            with self._debate_sem:
                t0 = time.time()
                text, _ = chat(prompt, provider=self.provider,
                               model=self.model, tools=READONLY_TOOLS,
                               cwd=str(self.repo_dir),
                               event_log=advocate_log)
                elapsed = time.time() - t0
            self._log(adv_label, f"Done ({elapsed:.0f}s)")
            (debate_dir / f"advocate_{idx}.txt").write_text(text)
            return idx, text

        arguments = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.debate_jobs,
        ) as pool:
            futures = {pool.submit(_run_advocate, e): e for e in edges}
            for f in concurrent.futures.as_completed(futures):
                try:
                    idx, text = f.result()
                    arguments[idx] = text
                except Exception as exc:
                    edge = futures[f]
                    self._log(label,
                              f"⚠ Advocate {edge['index']} failed: {exc}")
                    arguments[edge["index"]] = f"ERROR: {exc}"

        # Combine arguments
        all_arguments = ""
        for idx in sorted(arguments):
            e = edges[idx - 1]
            all_arguments += (
                f"\n--- ADVOCATE {idx}: {e['from_class']}.{e['property']}"
                f" → {e['to_class']} ---\n{arguments[idx]}\n"
            )
        (debate_dir / "all_arguments.txt").write_text(all_arguments)
        (debate_dir / "edges.jsonl").write_text(
            "\n".join(json.dumps(e) for e in edges))

        # --- Judge (retry until valid JSON) ---
        self._log(label, "Running judge")
        judge_log = EventLog(str(debate_dir / "judge.jsonl"))
        judge_prompt = fill_template(SCRIPTS / "judge_prompt.txt", {
            "ISSUE_TYPE": issue_type,
            "ISSUE_DESCRIPTION": issue_desc,
            "CHAIN": chain_text,
            "ALL_ARGUMENTS": all_arguments,
        })
        decision = None
        judge_text = ""
        for judge_attempt in range(1, 6):
            if judge_attempt == 1:
                prompt = judge_prompt
            else:
                self._log(label, f"Retrying judge (attempt {judge_attempt})")
                prompt = (
                    f"{judge_prompt}\n\n"
                    f"Your previous response could not be parsed as JSON. "
                    f"You MUST output ONLY a valid JSON block. No other text. "
                    f"Previous response:\n{judge_text[:500]}"
                )
            judge_log.emit("judge_attempt", attempt=judge_attempt)
            with self._debate_sem:
                judge_text, _ = chat(prompt, provider=self.provider,
                                     model=self.model, event_log=judge_log)
            (debate_dir / "judge_raw.txt").write_text(judge_text)
            try:
                decision = parse_json_response(judge_text)
                break
            except json.JSONDecodeError:
                continue

        if decision is None:
            self._log(label, "⚠ Judge failed after 5 attempts")
            self.report.set_issue(scoped_id, status="debate_failed")
            return None

        (debate_dir / "judge_decision.json").write_text(
            json.dumps(decision, indent=2))

        # Record debate results in report
        advocate_records = {}
        for idx in sorted(arguments):
            e = edges[idx - 1] if idx <= len(edges) else {}
            advocate_records[str(idx)] = {
                "edge": e.get("edge_desc", ""),
                "text": arguments[idx],
            }
        self.report.set_issue(scoped_id,
                              status="debated",
                              debate_finished_at=_ts(),
                              advocates=advocate_records,
                              judge_decision=decision)

        return ReviewResult(decision, arguments, edges, debate_dir)

    # -- fix + PR -----------------------------------------------------------

    def _cleanup_worktree(self, branch_name, label):
        """Remove any git worktree associated with *branch_name*."""
        worktree_list = run("git worktree list --porcelain",
                            cwd=str(self.repo_dir), check=False)
        for block in worktree_list.split("\n\n"):
            lines = block.strip().splitlines()
            wt_path = wt_branch = None
            for line in lines:
                if line.startswith("worktree "):
                    wt_path = line.split(" ", 1)[1]
                if line.startswith("branch "):
                    wt_branch = line.split(" ", 1)[1]
            if wt_branch and wt_branch.endswith(branch_name) and wt_path:
                self._log(label, f"Cleaning up worktree: {wt_path}")
                run(f"git worktree remove --force {wt_path}",
                    cwd=str(self.repo_dir), check=False)
                return

    def _run_fix_and_pr(self, issue, memgraph_file, mg_stem, review_result, label):
        """Run fix agent, open PR, spawn review worker.

        Caller must hold ``_fix_sem``.
        """
        decision, arguments, edges, debate_dir = review_result
        issue_id = issue.get("id", "unknown")
        scoped_id = _sanitize_branch(f"{mg_stem[:8]}-{issue_id}")
        issue_title = issue.get("title", "Unknown")
        root_class = issue.get("root_class", "Unknown")
        chain = issue.get("chain") or issue.get("cycle_stack", [])
        chain_text = "\n".join(f"    {line}" for line in chain)

        branch_name = f"fix/memory-leak-{scoped_id}"
        fix_log = EventLog(str(debate_dir / "fix_agent.jsonl"))

        fix_prompt = fill_template(SCRIPTS / "fix_leak_prompt.txt", {
            "LEAK_TITLE": issue_title,
            "ROOT_CLASS": root_class,
            "CYCLE_STACK": chain_text,
            "JUDGE_DECISION": json.dumps(decision, indent=2),
            "ISSUE_ID": issue_id,
        })

        self._log(label, "Fix agent starting")
        self.report.set_issue(scoped_id,
                              status="fixing",
                              fix_started_at=_ts(),
                              branch=branch_name)
        fix_session = Session(
            provider=self.provider, model=self.model, tools=ALL_TOOLS,
            cwd=str(self.repo_dir), event_log=fix_log,
        )
        try:
            try:
                fix_output, _ = fix_session.send(fix_prompt)
            except Exception as exc:
                fix_log.emit("crash", error=str(exc))
                self._log(label, f"⚠ Fix agent crashed: {exc}")
                if "prompt is too long" in str(exc) or "too long" in str(exc).lower():
                    fix_session = self._recover_fix_session(
                        branch_name, issue_title, root_class, chain_text,
                        decision, fix_log, label,
                    )
                    if not fix_session:
                        self._log(label, f"⚠ Recovery also failed. Logs: {debate_dir}")
                        return
                else:
                    self._log(label, f"⚠ Non-recoverable crash. Logs: {debate_dir}")
                    return

            # Verify branch pushed
            branch_exists = run(
                f"git ls-remote --heads origin {branch_name}",
                cwd=str(self.repo_dir))
            fix_log.emit("branch_check", branch=branch_name,
                         exists=bool(branch_exists))
            if not branch_exists:
                self._log(label, "⚠ Branch not pushed, recovering")
                fix_session = self._recover_fix_session(
                    branch_name, issue_title, root_class, chain_text,
                    decision, fix_log, label,
                )
                if not fix_session:
                    self._log(label, f"⚠ Recovery failed. Logs: {debate_dir}")
                    return
                branch_exists = run(
                    f"git ls-remote --heads origin {branch_name}",
                    cwd=str(self.repo_dir))
                fix_log.emit("branch_check_after_recovery", branch=branch_name,
                             exists=bool(branch_exists))
                if not branch_exists:
                    self._log(label,
                              f"⚠ Still no branch after recovery. "
                              f"Issue: {issue_title} | Branch: {branch_name} | "
                              f"Logs: {debate_dir}")
                    return

            # Open PR
            pr_body = self.build_pr_body(issue, decision, edges, arguments,
                                         memgraph_file)
            pr_id = self.open_draft_pr(
                branch_name, f"Fix memory issue: {issue_title}", pr_body)
            self._log(label, f"PR: {pr_id}")

            self.report.set_issue(scoped_id,
                                  fix_finished_at=_ts())
            if pr_id:
                self.report.set_issue(scoped_id,
                                      status="pr_open",
                                      pr_number=pr_id,
                                      pr_opened_at=_ts())
                self._spawn_review_worker(pr_id, branch_name, issue_title,
                                          fix_session, debate_dir, label)
                if self.once:
                    self._cancel.set()
        finally:
            # Always clean up the worktree — the branch is either pushed
            # (so the worktree is no longer needed) or the fix failed.
            self._cleanup_worktree(branch_name, label)

    def _spawn_review_worker(self, pr_id, branch_name, issue_title,
                             fix_session, debate_dir, label):
        """Run post_review_loop in a background thread."""
        def _worker():
            try:
                self.post_review_loop(pr_id, branch_name, issue_title,
                                      fix_session)
            except Exception as exc:
                self._log(label, f"⚠ Review worker crashed: {exc}")

        t = threading.Thread(target=_worker, name=f"review-{label}",
                             daemon=True)
        t.start()
        with self._review_lock:
            self._review_workers.append(t)
        self._log(label, "Review worker started")

    # -- Recovery -----------------------------------------------------------

    def _recover_fix_session(self, branch_name, issue_title, root_class,
                             chain_text, decision, fix_log, label=""):
        """Start a fresh Session after the original hit the context limit.

        Finds the worktree (if any), collects the diff of work done so far,
        and gives a new agent the decision + diff + instructions to finish.
        Returns the new Session, or None if recovery failed.
        """
        fix_log.emit("recovery_start", branch=branch_name)
        self._log(label, f"🔄 Recovery session for {branch_name}")

        # Find the worktree for this branch
        worktree_dir = None
        worktree_list = run("git worktree list --porcelain",
                            cwd=str(self.repo_dir), check=False)
        for block in worktree_list.split("\n\n"):
            lines = block.strip().splitlines()
            wt_path = wt_branch = None
            for line in lines:
                if line.startswith("worktree "):
                    wt_path = line.split(" ", 1)[1]
                if line.startswith("branch "):
                    wt_branch = line.split(" ", 1)[1]
            if wt_branch and wt_branch.endswith(branch_name) and wt_path:
                worktree_dir = wt_path
                break

        # Collect diff of work done so far
        _DIFF_EXCLUDE = ("':(exclude)*.memgraph' ':(exclude)*.tar.gz' "
                         "':(exclude)*.zip' ':(exclude)*.tgz'")
        diff = ""
        cwd_for_session = str(self.repo_dir)
        if worktree_dir:
            self._log(label, f"Found worktree: {worktree_dir}")
            cwd_for_session = worktree_dir
            main = self._get_main_branch()
            diff = run(f"git diff origin/{main}..HEAD -- . {_DIFF_EXCLUDE}",
                       cwd=worktree_dir, check=False)
        else:
            self._log(label, "No worktree found, checking local branch")
            local_branch = run(f"git rev-parse --verify {branch_name}",
                               cwd=str(self.repo_dir), check=False)
            if local_branch:
                main_branch = self._get_main_branch()
                diff = run(
                    f"git diff origin/{main_branch}..{branch_name}"
                    f" -- . {_DIFF_EXCLUDE}",
                    cwd=str(self.repo_dir), check=False)

        diff_summary = f"{len(diff)} chars" if diff else "empty"
        fix_log.emit("recovery_diff", worktree=worktree_dir,
                     diff_len=len(diff))
        self._log(label, f"Diff collected: {diff_summary}")

        recovery_prompt = f"""You are an iOS/Swift engineer finishing a memory leak fix that a previous agent started but could not complete (it ran out of context).

## Memory Issue

ISSUE_TITLE: {issue_title}
ROOT_CLASS: {root_class}

CHAIN:
{chain_text}

## Senior Engineer's Decision

{json.dumps(decision, indent=2)}

## Work Done So Far

{"The previous agent made these changes:" if diff else "The previous agent made NO changes before crashing."}

```diff
{diff if diff else "(no diff)"}
```

## Instructions

1. If a worktree already exists at the branch `{branch_name}`, work in it. Otherwise create one off `main`.
2. Review the diff above. Pick up where the previous agent left off.
3. If the fix is already applied, just ensure there's a regression test, commit, and push.
4. If the fix is incomplete, complete it following the senior engineer's decision exactly.
5. Commit and push the branch `{branch_name}`.
6. Remove the worktree when done.

When done, output NOTHING except the word DONE."""

        recovery_session = Session(
            provider=self.provider, model=self.model, tools=ALL_TOOLS,
            cwd=cwd_for_session, event_log=fix_log,
        )
        try:
            recovery_output, _ = recovery_session.send(recovery_prompt)
            fix_log.emit("recovery_done", output=recovery_output[:2000])
            return recovery_session
        except Exception as exc:
            fix_log.emit("recovery_crash", error=str(exc))
            self._log(label, f"⚠ Recovery session also crashed: {exc}")
            return None

    def _get_main_branch(self):
        """Get the main branch name. Subclasses may override."""
        ref = run(f"git -C {self.repo_dir} symbolic-ref refs/remotes/origin/HEAD",
                  check=False)
        return ref.replace("refs/remotes/origin/", "") or "main"
