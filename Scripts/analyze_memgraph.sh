#!/usr/bin/env bash
#
# analyze_memgraph.sh — Produce a comprehensive plain-text diagnostic report
# from a macOS/iOS .memgraph file.
#
# Usage:  ./analyze_memgraph.sh /path/to/file.memgraph > report.txt
#
# Requirements: macOS with Xcode command-line tools (provides leaks, vmmap, heap)
# Deterministic, non-interactive. Writes all output to stdout.

analyze_memgraph() {
    local MEMGRAPH="$1"
    local MAX_DEPTH=12             # Max tree depth per root in Section 4C
    local MAX_LINES_PER_ROOT=80    # Hard line cap per root tree in Section 4C

    if [ ! -f "$MEMGRAPH" ]; then
        echo "ERROR: File not found: $MEMGRAPH" >&2
        return 1
    fi

    echo "================================================================================"
    echo "MEMGRAPH ANALYSIS REPORT"
    echo "File: $(basename "$MEMGRAPH")"
    echo "Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "================================================================================"

    # ── SECTION 1: Virtual Memory Summary ────────────────────────────────────────
    # Shows process metadata, physical footprint, per-region-type breakdown
    # (sorted by dirty+swapped size), and per-malloc-zone stats with fragmentation.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 1: VIRTUAL MEMORY SUMMARY                                         ║"
    echo "║  (vmmap -summary -sortBySize)                                               ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    vmmap -summary -sortBySize "$MEMGRAPH" 2>&1

    # ── SECTION 2: Heap — Top Classes by Total Bytes ─────────────────────────────
    # Heap class table sorted by cumulative byte usage (descending).
    # Shows the ~100 most memory-hungry allocation types on the heap.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 2: HEAP — TOP CLASSES BY TOTAL BYTES                               ║"
    echo "║  (heap -s -H — sorted by cumulative size, top ~100)                         ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    local heap_by_size
    heap_by_size="$(heap -q -s -H "$MEMGRAPH" 2>&1)"
    echo "$heap_by_size" | head -120

    # ── SECTION 3: Heap — Top Classes by Instance Count ──────────────────────────
    # Same heap data, but sorted by instance count. Surfaces proliferating objects
    # that may individually be small but collectively consume significant memory.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 3: HEAP — TOP CLASSES BY INSTANCE COUNT                            ║"
    echo "║  (heap -H — sorted by instance count, top ~100)                             ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    local heap_by_count
    heap_by_count="$(heap -q -H "$MEMGRAPH" 2>&1)"
    echo "$heap_by_count" | head -120

    # ══════════════════════════════════════════════════════════════════════════════
    # SECTION 4: Leak Detection (three sub-parts: 4A, 4B, 4C)
    #
    # Captures the full `leaks --groupByType` output once, then presents it as:
    #   4A — Summary table of ALL distinct leak roots
    #   4B — Extracted retain-cycle chains (root → … → CYCLE BACK TO root)
    #   4C — Per-root depth-limited trees for full context
    # ══════════════════════════════════════════════════════════════════════════════
    local leak_output
    leak_output="$(leaks -q --groupByType "$MEMGRAPH" 2>&1 || true)"

    # ── 4A: Leak Summary & Root Inventory ────────────────────────────────────────
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 4A: LEAK SUMMARY & ROOT INVENTORY                                  ║"
    echo "║  (every distinct ROOT CYCLE and ROOT LEAK from leaks --groupByType)         ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    # Print the process/leak header lines
    echo "$leak_output" | head -5
    echo ""
    # Root inventory table
    echo "  #   TYPE    NODES     SIZE   ROOT NAME"
    echo "  --- ------  ------  -------  ---------"
    echo "$leak_output" | awk '
    /^      [0-9].* ROOT (CYCLE|LEAK):/ {
        n++
        line = $0
        match(line, /[0-9]+/)
        count = substr(line, RSTART, RLENGTH)
        match(line, /\([^)]+\)/)
        size = substr(line, RSTART+1, RLENGTH-2)
        if (line ~ /ROOT CYCLE:/) {
            type = "CYCLE"
            name = line; sub(/.*ROOT CYCLE: /, "", name)
        } else {
            type = "LEAK"
            name = line; sub(/.*ROOT LEAK: /, "", name)
        }
        printf "  %-3d %-6s  %6s  %7s  %s\n", n, type, count, size, name
    }'
    echo ""
    echo "  Cycle Closure Points:"
    echo "$leak_output" | awk '
    /^      [0-9].* ROOT CYCLE:/ {
        name = $0; sub(/.*ROOT CYCLE: /, "", name); current_root = name
    }
    /CYCLE BACK TO/ {
        target = $0; sub(/.*CYCLE BACK TO /, "", target)
        printf "    %s  →  CYCLE BACK TO %s\n", current_root, target
    }'

    # ── 4B: Retain Cycle Chains ──────────────────────────────────────────────────
    # For each ROOT CYCLE, extract ONLY the reference chain that forms the cycle:
    # the path from the root down to the "CYCLE BACK TO" line. This is the most
    # actionable information for breaking retain cycles.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 4B: RETAIN CYCLE CHAINS                                            ║"
    echo "║  (extracted reference path from each ROOT CYCLE to its CYCLE BACK TO)       ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "$leak_output" | awk '
    BEGIN { cycle_num = 0 }
    !/^ / { next }
    {
        match($0, /^[ ]*/)
        depth = (RLENGTH - 6) / 3
        if (depth < 0) next

        # Maintain ancestor stack: overwrite at current depth
        stack[depth] = $0

        if (/ROOT CYCLE:/ && depth == 0) {
            name = $0; sub(/.*ROOT CYCLE: /, "", name)
            current_root = name
        }

        if (/CYCLE BACK TO/) {
            cycle_num++
            printf "  ── Cycle #%d: %s ──\n", cycle_num, current_root
            for (i = 0; i <= depth; i++) {
                line = stack[i]
                gsub(/^[ ]+/, "", line)
                printf "    %*s%s\n", i * 2, "", line
            }
            printf "\n"
        }
    }'

    # ── 4C: Per-Root Depth-Limited Trees ─────────────────────────────────────────
    # Full groupByType trees, but each root is capped at MAX_DEPTH levels of
    # nesting and MAX_LINES_PER_ROOT lines. CYCLE BACK TO lines are always shown
    # regardless of depth/line limits. This ensures every root is visible while
    # keeping the output bounded.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 4C: LEAK TREES — DEPTH-LIMITED                                     ║"
    echo "║  (per-root: max depth ${MAX_DEPTH}, max ${MAX_LINES_PER_ROOT} lines; CYCLE BACK TO always shown)       ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "$leak_output" | awk -v max_depth="$MAX_DEPTH" -v max_lines="$MAX_LINES_PER_ROOT" '
    /^Process/ || /^ *[0-9]+ .* << TOTAL >>/ { print; next }

    /^      [0-9].* ROOT (CYCLE|LEAK):/ {
        if (in_root && total_omitted > 0) {
            printf "         ... [%d lines omitted]\n", total_omitted
        }
        if (in_root) printf "\n"
        in_root = 1
        printed = 0
        total_omitted = 0
    }

    /^ / {
        if (!in_root) next
        match($0, /^[ ]*/)
        depth = (RLENGTH - 6) / 3
        if (depth < 0) next

        if ($0 ~ /CYCLE BACK TO/ || (depth <= max_depth && printed < max_lines)) {
            print
            printed++
        } else {
            total_omitted++
        }
    }

    END {
        if (in_root && total_omitted > 0) {
            printf "         ... [%d lines omitted]\n", total_omitted
        }
    }'

    # ── SECTION 5: Leak Type Frequency ───────────────────────────────────────────
    # Flat count of how many leaked allocations exist per type.
    # Derived by post-processing `leaks --list` output. Includes the summary
    # header (total leaks count and bytes) followed by the frequency table.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 5: LEAK TYPE FREQUENCY (top 50 types by occurrence count)          ║"
    echo "║  (derived from leaks --list)                                                ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    local list_output
    list_output="$(leaks -q --list "$MEMGRAPH" 2>&1 || true)"
    echo "$list_output" | head -4
    echo ""
    echo "  COUNT  TYPE"
    echo "  -----  ----"
    echo "$list_output" | grep '^Leak:' | \
        sed 's/.*zone: [^ ]*//' | sed 's/^  *//' | \
        sort | uniq -c | sort -rn | head -50

    # ── SECTION 6: Leaks by Module ───────────────────────────────────────────────
    # Aggregates leaked allocations by their source binary/framework. Helps
    # identify which module is responsible for the most leaked memory.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 6: LEAKS BY MODULE (top 30 source binaries)                        ║"
    echo "║  (derived from leaks --list — which library owns each leaked allocation)    ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "  COUNT   TOTAL SIZE  MODULE"
    echo "  -----   ----------  ------"
    echo "$list_output" | grep '^Leak:' | awk '{
        size = 0
        for (i = 1; i <= NF; i++) {
            if ($i ~ /^size=/) { size = substr($i, 6) + 0; break }
        }
        # Find content after "zone: <name>"
        line = $0
        match(line, /zone: [^ ]+/)
        rest = substr(line, RSTART + RLENGTH)
        gsub(/^[ ]+/, "", rest)
        if (rest == "") {
            module = "(untyped)"
        } else {
            n = split(rest, parts, /  +/)
            module = parts[n]
            if (module == "") module = parts[n-1]
        }
        mod_count[module]++
        mod_bytes[module] += size
    }
    END {
        for (m in mod_count) {
            b = mod_bytes[m]
            if (b >= 1048576) s = sprintf("%.1fM", b / 1048576.0)
            else if (b >= 1024) s = sprintf("%.1fK", b / 1024.0)
            else s = sprintf("%dB", b)
            printf "  %5d   %8s  %s\n", mod_count[m], s, m
        }
    }' | sort -rn | head -30

    # ── SECTION 7: Largest Individual Leaked Allocations ────────────────────────
    # The 30 single largest leaked allocations by byte size. Useful for spotting
    # large data buffers, arrays, or images that are leaked.
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 7: LARGEST INDIVIDUAL LEAKED ALLOCATIONS (top 30 by size)          ║"
    echo "║  (derived from leaks --list, sorted by allocation size descending)          ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "$list_output" | grep '^Leak:' | \
        sort -t= -k2 -rn | head -30

    # ── SECTION 8: Autorelease Pool Summary ─────────────────────────────────────
    # Instead of dumping raw pool entries (which can be tens of thousands of
    # lines), this shows:
    #   - The aggregate summary block (total entries, memory held)
    #   - A per-thread breakdown (entry count and pool-only count)
    #   - Top pool-only types by frequency (types held ONLY by pools)
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════════╗"
    echo "║  SECTION 8: AUTORELEASE POOL SUMMARY                                       ║"
    echo "║  (leaks --autoreleasePools — items marked ** are held ONLY by pools)        ║"
    echo "╚══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
    local autorelease_output
    autorelease_output="$(leaks -q --autoreleasePools "$MEMGRAPH" 2>&1 || true)"

    # Aggregate summary block (everything before the first Thread line)
    echo "$autorelease_output" | awk '/^Thread / { exit } { print }'
    echo ""

    # Per-thread breakdown
    echo "  Per-Thread Breakdown:"
    echo "  THREAD                                                       ENTRIES  POOL-ONLY"
    echo "  -------                                                      -------  ---------"
    echo "$autorelease_output" | awk '
    /^Thread / {
        if (thread != "") printf "  %-60s %7d  %9d\n", thread, total_ct, pool_ct
        thread = $0; total_ct = 0; pool_ct = 0
    }
    /^ *\*\* *[0-9]+:/ { pool_ct++; total_ct++; next }
    /^ *[0-9]+:/ { total_ct++ }
    END {
        if (thread != "") printf "  %-60s %7d  %9d\n", thread, total_ct, pool_ct
    }'
    echo ""

    # Top pool-only types by frequency
    echo "  Top Pool-Only Types (held ONLY by autorelease pools, top 20):"
    echo "  COUNT  TYPE"
    echo "  -----  ----"
    echo "$autorelease_output" | grep '^ *\*\*' | \
        sed -n 's/.*<\([A-Za-z_][A-Za-z0-9_]*\) 0x.*/\1/p' | \
        sort | uniq -c | sort -rn | head -20

    echo ""
    echo "================================================================================"
    echo "END OF REPORT"
    echo "================================================================================"
}

analyze_memgraph "$1"
