#!/usr/bin/env python3
"""
sloc.py - Smart static analysis of lines of code (LOC / SLOC).

Features:
- Smart analysis: ignores generated lockfiles (package-lock.json, uv.lock).
- Accurate metrics: total, blank, comment, and non-blank lines (SLOC).
- Multi-level breakdown:
    * By functional domain (Backend, Frontend, Infra/DevOps, Config/Docs)
    * By detailed subtype (Domain, Tests, Migrations, Commands, UI, Styles, etc.)
    * By programming language / format
    * File by file
- Software engineering ratios (test coverage by LOC, Backend/Frontend ratio, etc.).
- Available outputs: formatted console (with or without colors), Markdown (--markdown), JSON (--json).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ==============================================================================
# Data model
# ==============================================================================

@dataclass
class FileMetric:
    path: str
    language: str
    domain: str
    subdomain: str
    is_test: bool
    total_lines: int
    blank_lines: int
    comment_lines: int
    non_blank_lines: int  # total_lines - blank_lines
    code_lines: int       # non_blank_lines - comment_lines


# ==============================================================================
# Classification rules
# ==============================================================================

LOCKFILE_NAMES = {
    "package-lock.json",
    "uv.lock",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "composer.lock",
}

IGNORED_DIRECTORIES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".next",
    ".cache",
    "coverage",
}


def detect_language(path: str, filepath: Optional[Path] = None) -> str:
    basename = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()

    if ext == ".py":
        return "Python"
    if ext == ".tsx":
        return "TypeScript (TSX)"
    if ext == ".ts":
        return "TypeScript"
    if ext in (".js", ".mjs", ".cjs"):
        return "JavaScript"
    if ext in (".css", ".scss", ".sass", ".less"):
        return "CSS"
    if ext in (".html", ".htm"):
        return "HTML"
    if ext == ".json":
        return "JSON"
    if ext in (".yaml", ".yml"):
        return "YAML"
    if ext == ".toml":
        return "TOML"
    if ext == ".md":
        return "Markdown"
    if ext in (".sh", ".bash"):
        return "Shell"
    if basename in ("Dockerfile", "compose.yaml", "docker-compose.yml") or basename.startswith(".docker") or basename.startswith(".git") or basename.startswith(".prettier") or basename.startswith(".env"):
        return "DevOps / Config"

    if not ext:
        try:
            with (filepath or Path(path)).open(
                "r", encoding="utf-8", errors="replace"
            ) as source_file:
                first_line = source_file.readline()
        except (OSError, UnicodeError):
            first_line = ""

        if first_line.startswith("#!"):
            interpreter = first_line[2:].strip().split()
            if interpreter:
                executable = os.path.basename(interpreter[0])
                if executable == "env":
                    executable = next(
                        (
                            part
                            for part in interpreter[1:]
                            if not part.startswith("-") and "=" not in part
                        ),
                        "",
                    )
                    executable = os.path.basename(executable)

                if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable):
                    return "Python"
                if executable in ("node", "nodejs", "deno", "bun"):
                    return "JavaScript"
                if executable in ("ts-node", "tsx"):
                    return "TypeScript"
                if executable in ("sh", "bash", "dash", "ksh", "zsh", "fish"):
                    return "Shell"

    return "Other"


def classify_file(rel_path: str) -> Tuple[str, str, bool]:
    """
    Return a tuple: (primary_domain, subdomain, is_test).
    """
    basename = os.path.basename(rel_path)
    norm = rel_path.replace("\\", "/")

    # 1. Backend
    if norm.startswith("backend/"):
        if norm.startswith("backend/cinema/tests/"):
            return ("Backend", "Backend - Tests", True)
        if norm.startswith("backend/cinema/migrations/"):
            return ("Backend", "Backend - Migrations", False)
        if norm.startswith("backend/cinema/management/"):
            return ("Backend", "Backend - CLI Commands", False)
        if norm.startswith("backend/cinema/"):
            return ("Backend", "Backend - Domain & API", False)
        if norm.startswith("backend/config/") or norm == "backend/manage.py":
            return ("Backend", "Backend - Config & Core", False)
        if norm == "backend/pyproject.toml":
            return ("Configuration", "Config - Python Dependencies", False)
        if basename in ("Dockerfile", ".dockerignore"):
            return ("Infra / DevOps", "Docker & Deployment", False)
        return ("Backend", "Backend - Other", False)

    # 2. Frontend
    if norm.startswith("frontend/"):
        if norm.startswith("frontend/src/") and ("test" in norm or "spec" in norm):
            return ("Frontend", "Frontend - Tests", True)
        if norm.startswith("frontend/src/") and norm.endswith(".css"):
            return ("Frontend", "Frontend - Styles & Design", False)
        if norm.startswith("frontend/src/"):
            return ("Frontend", "Frontend - Components & UI", False)
        if norm == "frontend/index.html":
            return ("Frontend", "Frontend - HTML Entrypoint", False)
        if norm == "frontend/package.json":
            return ("Configuration", "Config - Node Dependencies", False)
        if basename in ("Dockerfile", ".dockerignore", ".prettierignore"):
            return ("Infra / DevOps", "Docker & Deployment", False)
        if any(norm.startswith(f"frontend/{prefix}") for prefix in ("vite.config", "vitest.config", "eslint.config", "tsconfig")):
            return ("Frontend", "Frontend - Tooling & Build", False)
        return ("Frontend", "Frontend - Other", False)

    # 3. Root-level infrastructure and DevOps
    if norm in ("compose.yaml", "docker-compose.yml", ".gitignore", ".env.example", ".env"):
        return ("Infra / DevOps", "Docker & Deployment", False)

    # 4. Documentation
    if norm.endswith(".md"):
        return ("Documentation", "Documentation Markdown", False)

    # 5. Root-level configuration
    if basename in ("pyproject.toml", "package.json"):
        return ("Configuration", "Config - Dependencies", False)

    return ("Other", "Miscellaneous Files", False)


# ==============================================================================
# Line and comment analyzer
# ==============================================================================

def analyze_file_content(filepath: Path, language: str) -> Tuple[int, int, int]:
    """
    Analyze a file's contents and return:
    (total_lines, blank_lines, comment_lines)
    """
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0, 0, 0

    lines = content.splitlines()
    total = len(lines)
    blank = 0
    comments = 0

    # Multiline comment state
    in_block_comment = False
    block_delimiter = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank += 1
            continue

        if language == "Python":
            if in_block_comment:
                comments += 1
                if block_delimiter in stripped:
                    in_block_comment = False
                    block_delimiter = ""
                continue

            if stripped.startswith('"""') or stripped.startswith("'''"):
                delim = stripped[:3]
                comments += 1
                # Enter multiline mode if the delimiter does not close on this line
                if stripped.count(delim) < 2 or len(stripped) == 3:
                    in_block_comment = True
                    block_delimiter = delim
                continue

            if stripped.startswith("#"):
                comments += 1
                continue

        elif language in ("TypeScript", "TypeScript (TSX)", "JavaScript", "CSS"):
            if in_block_comment:
                comments += 1
                if "*/" in stripped:
                    in_block_comment = False
                continue

            if stripped.startswith("/*"):
                comments += 1
                if "*/" not in stripped[2:]:
                    in_block_comment = True
                continue

            if stripped.startswith("//"):
                comments += 1
                continue

        elif language == "HTML":
            if in_block_comment:
                comments += 1
                if "-->" in stripped:
                    in_block_comment = False
                continue

            if stripped.startswith("<!--"):
                comments += 1
                if "-->" not in stripped[4:]:
                    in_block_comment = True
                continue

        elif language in ("YAML", "TOML", "Shell", "DevOps / Config"):
            if stripped.startswith("#"):
                comments += 1
                continue

        # JSON and Markdown have no code comment syntax handled here

    return total, blank, comments


# ==============================================================================
# File collection
# ==============================================================================

def get_tracked_or_all_files(root: Path) -> List[str]:
    """
    Return the list of files to analyze.
    Prefer 'git ls-files' to automatically ignore untracked files and caches.
    Without Git, use os.walk while ignoring standard directories.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
        files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if files:
            return files
    except Exception:
        pass

    # Manual recursive fallback
    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRECTORIES]
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = full.relative_to(root).as_posix()
            collected.append(rel)
    return collected


def collect_metrics(root: Path) -> List[FileMetric]:
    files = get_tracked_or_all_files(root)
    metrics: List[FileMetric] = []

    for rel_path in files:
        if os.path.basename(rel_path) in LOCKFILE_NAMES:
            continue

        full_path = root / rel_path
        if not full_path.is_file():
            continue

        lang = detect_language(rel_path, full_path)
        domain, subdomain, is_test = classify_file(rel_path)
        total, blank, comments = analyze_file_content(full_path, lang)
        non_blank = total - blank
        code = max(0, non_blank - comments)

        metrics.append(
            FileMetric(
                path=rel_path,
                language=lang,
                domain=domain,
                subdomain=subdomain,
                is_test=is_test,
                total_lines=total,
                blank_lines=blank,
                comment_lines=comments,
                non_blank_lines=non_blank,
                code_lines=code,
            )
        )

    return metrics


# ==============================================================================
# Formatting and display
# ==============================================================================

class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def format_table(
    headers: List[str],
    rows: List[List[str]],
    alignments: List[str],
    total_row: Optional[List[str]] = None,
    markdown: bool = False,
    use_color: bool = True,
) -> str:
    """Generate a clean ASCII or Markdown table."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))
    if total_row:
        for i, val in enumerate(total_row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    lines = []

    def format_cell(val: str, idx: int) -> str:
        s = str(val)
        if alignments[idx] == "right":
            return s.rjust(col_widths[idx])
        return s.ljust(col_widths[idx])

    if markdown:
        lines.append("| " + " | ".join(format_cell(h, i) for i, h in enumerate(headers)) + " |")
        sep = []
        for i, align in enumerate(alignments):
            if align == "right":
                sep.append("-" * (col_widths[i] - 1) + ":")
            else:
                sep.append(":" + "-" * (col_widths[i] - 1))
        lines.append("| " + " | ".join(sep) + " |")
        for row in rows:
            lines.append("| " + " | ".join(format_cell(c, i) for i, c in enumerate(row)) + " |")
        if total_row:
            lines.append("| " + " | ".join(format_cell(c, i) for i, c in enumerate(total_row)) + " |")
    else:
        # Terminal styling
        b = Colors.BOLD if use_color else ""
        r = Colors.RESET if use_color else ""
        c_cyan = Colors.CYAN if use_color else ""
        c_green = Colors.GREEN if use_color else ""

        sep_line = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
        lines.append(sep_line)
        lines.append("| " + " | ".join(f"{b}{format_cell(h, i)}{r}" for i, h in enumerate(headers)) + " |")
        lines.append(sep_line)
        for row in rows:
            formatted_cells = [format_cell(c, i) for i, c in enumerate(row)]
            lines.append("| " + " | ".join(formatted_cells) + " |")
        if total_row:
            lines.append(sep_line)
            formatted_tot = [f"{b}{format_cell(c, i)}{r}" for i, c in enumerate(total_row)]
            lines.append("| " + " | ".join(formatted_tot) + " |")
        lines.append(sep_line)

    return "\n".join(lines)


def aggregate_group(metrics: List[FileMetric], group_key_fn) -> Dict[str, Dict[str, int]]:
    grouped: Dict[str, Dict[str, int]] = defaultdict(lambda: {
        "files": 0,
        "total": 0,
        "blank": 0,
        "comments": 0,
        "non_blank": 0,
        "code": 0,
    })
    for m in metrics:
        key = group_key_fn(m)
        g = grouped[key]
        g["files"] += 1
        g["total"] += m.total_lines
        g["blank"] += m.blank_lines
        g["comments"] += m.comment_lines
        g["non_blank"] += m.non_blank_lines
        g["code"] += m.code_lines
    return grouped


# ==============================================================================
# Full report
# ==============================================================================

def generate_report(
    metrics: List[FileMetric],
    mode: str = "type",
    markdown: bool = False,
    use_color: bool = True,
    show_ratios: bool = True,
) -> str:
    tot_total = sum(m.total_lines for m in metrics)
    tot_blank = sum(m.blank_lines for m in metrics)
    tot_comments = sum(m.comment_lines for m in metrics)
    tot_non_blank = sum(m.non_blank_lines for m in metrics)

    output = []
    headers = ["Category / Scope", "Files", "Total", "Blank", "Comments", "Non-Blank (SLOC)", "% Code"]
    alignments = ["left", "right", "right", "right", "right", "right", "right"]

    if mode in ("type", "summary"):
        # Aggregate by primary domain
        domain_data = aggregate_group(metrics, lambda m: m.domain)
        # Sort Backend, Frontend, Infra, Config, Docs, etc.
        order = ["Backend", "Frontend", "Infra / DevOps", "Configuration", "Documentation", "Other"]
        sorted_keys = sorted(domain_data.keys(), key=lambda k: (order.index(k) if k in order else 99, -domain_data[k]["non_blank"]))

        rows = []
        for k in sorted_keys:
            d = domain_data[k]
            pct = (d["non_blank"] / tot_non_blank * 100) if tot_non_blank else 0
            rows.append([
                k,
                str(d["files"]),
                f"{d['total']:,}",
                f"{d['blank']:,}",
                f"{d['comments']:,}",
                f"{d['non_blank']:,}",
                f"{pct:5.1f} %",
            ])

        total_row = [
            "TOTAL SOURCE CODE",
            str(len(metrics)),
            f"{tot_total:,}",
            f"{tot_blank:,}",
            f"{tot_comments:,}",
            f"{tot_non_blank:,}",
            "100.0 %",
        ]
        table_str = format_table(headers, rows, alignments, total_row, markdown=markdown, use_color=use_color)
        output.append(table_str)

    elif mode in ("subtypes", "detailed"):
        sub_data = aggregate_group(metrics, lambda m: m.subdomain)
        sorted_keys = sorted(sub_data.keys(), key=lambda k: -sub_data[k]["non_blank"])

        rows = []
        for k in sorted_keys:
            d = sub_data[k]
            pct = (d["non_blank"] / tot_non_blank * 100) if tot_non_blank else 0
            rows.append([
                k,
                str(d["files"]),
                f"{d['total']:,}",
                f"{d['blank']:,}",
                f"{d['comments']:,}",
                f"{d['non_blank']:,}",
                f"{pct:5.1f} %",
            ])

        total_row = [
            "TOTAL",
            str(len(metrics)),
            f"{tot_total:,}",
            f"{tot_blank:,}",
            f"{tot_comments:,}",
            f"{tot_non_blank:,}",
            "100.0 %",
        ]
        table_str = format_table(["Detailed Subdomain", "Files", "Total", "Blank", "Comments", "Non-Blank", "% Total"],
                                 rows, alignments, total_row, markdown=markdown, use_color=use_color)
        output.append(table_str)

    elif mode == "language":
        lang_data = aggregate_group(metrics, lambda m: m.language)
        sorted_keys = sorted(lang_data.keys(), key=lambda k: -lang_data[k]["non_blank"])

        rows = []
        for k in sorted_keys:
            d = lang_data[k]
            pct = (d["non_blank"] / tot_non_blank * 100) if tot_non_blank else 0
            rows.append([
                k,
                str(d["files"]),
                f"{d['total']:,}",
                f"{d['blank']:,}",
                f"{d['comments']:,}",
                f"{d['non_blank']:,}",
                f"{pct:5.1f} %",
            ])

        total_row = [
            "TOTAL",
            str(len(metrics)),
            f"{tot_total:,}",
            f"{tot_blank:,}",
            f"{tot_comments:,}",
            f"{tot_non_blank:,}",
            "100.0 %",
        ]
        table_str = format_table(["Language / Format", "Files", "Total", "Blank", "Comments", "Non-Blank", "% Total"],
                                 rows, alignments, total_row, markdown=markdown, use_color=use_color)
        output.append(table_str)

    elif mode == "files":
        f_headers = ["File Path", "Domain", "Language", "Total", "Blank", "Comments", "Non-Blank"]
        f_align = ["left", "left", "left", "right", "right", "right", "right"]
        sorted_m = sorted(metrics, key=lambda m: (m.domain, -m.non_blank_lines))
        rows = [
            [
                m.path,
                m.domain,
                m.language,
                f"{m.total_lines:,}",
                f"{m.blank_lines:,}",
                f"{m.comment_lines:,}",
                f"{m.non_blank_lines:,}",
            ]
            for m in sorted_m
        ]
        total_row = [
            "TOTAL",
            "-",
            "-",
            f"{tot_total:,}",
            f"{tot_blank:,}",
            f"{tot_comments:,}",
            f"{tot_non_blank:,}",
        ]
        output.append(format_table(f_headers, rows, f_align, total_row, markdown=markdown, use_color=use_color))

    # Key ratios
    if show_ratios:
        backend_app = sum(m.non_blank_lines for m in metrics if m.subdomain == "Backend - Domain & API")
        backend_tests = sum(m.non_blank_lines for m in metrics if m.subdomain == "Backend - Tests")
        backend_total = sum(m.non_blank_lines for m in metrics if m.domain == "Backend")
        frontend_total = sum(m.non_blank_lines for m in metrics if m.domain == "Frontend")
        frontend_app = sum(m.non_blank_lines for m in metrics if m.subdomain in ("Frontend - Components & UI", "Frontend - Styles & Design"))
        frontend_tests = sum(m.non_blank_lines for m in metrics if m.subdomain == "Frontend - Tests")

        ratios = []
        if backend_app > 0 and backend_tests > 0:
            ratio_bt = (backend_tests / backend_app) * 100
            ratios.append(f"- **Backend Tests / Application Code Ratio**: {ratio_bt:.1f}% ({backend_tests} test lines for {backend_app} application code lines)")
        if frontend_total > 0 and backend_total > 0:
            ratio_bf = (backend_total / (backend_total + frontend_total)) * 100
            ratios.append(f"- **Backend vs Frontend Balance**: {ratio_bf:.1f}% Backend ({backend_total} lines) / {100 - ratio_bf:.1f}% Frontend ({frontend_total} lines)")
        if frontend_app > 0 and frontend_tests > 0:
            ratio_ft = (frontend_tests / frontend_app) * 100
            ratios.append(f"- **Frontend Tests / UI Code Ratio**: {ratio_ft:.1f}% ({frontend_tests} test lines for {frontend_app} UI lines)")

        if ratios:
            output.append("\n### Key Ratios and Indicators:\n" + "\n".join(ratios))

    return "\n".join(output)


# ==============================================================================
# CLI entry point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Smart static analysis of lines of code (LOC / non-blank SLOC)."
    )
    parser.add_argument(
        "--root",
        "-r",
        default=".",
        help="Project root path (default: current directory)",
    )
    parser.add_argument(
        "--by",
        choices=["type", "subtypes", "language", "files", "all"],
        default="type",
        help="Primary aggregation type (default: type)",
    )
    parser.add_argument(
        "--detailed",
        "-d",
        action="store_true",
        help="Show the detailed breakdown by subcategory (equivalent to --by subtypes)",
    )
    parser.add_argument(
        "--language",
        "-l",
        action="store_true",
        help="Show the breakdown by programming language (equivalent to --by language)",
    )
    parser.add_argument(
        "--files",
        "-f",
        action="store_true",
        help="Show file-by-file details (equivalent to --by files)",
    )
    parser.add_argument(
        "--markdown",
        "-m",
        action="store_true",
        help="Format output as Markdown tables",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="Export all raw and aggregated metrics as JSON",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI terminal colors",
    )

    args = parser.parse_args()
    root_path = Path(args.root).resolve()

    if not root_path.is_dir():
        sys.stderr.write(f"Error: directory '{root_path}' does not exist.\n")
        sys.exit(1)

    metrics = collect_metrics(root_path)
    use_color = not args.no_color and sys.stdout.isatty() and not args.markdown

    if args.json:
        result = {
            "root": str(root_path),
            "files_analyzed": len(metrics),
            "files": [asdict(m) for m in metrics],
            "summary": {
                "by_domain": aggregate_group(metrics, lambda m: m.domain),
                "by_subdomain": aggregate_group(metrics, lambda m: m.subdomain),
                "by_language": aggregate_group(metrics, lambda m: m.language),
            }
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # Determine the output mode
    mode = args.by
    if args.detailed:
        mode = "subtypes"
    elif args.language:
        mode = "language"
    elif args.files:
        mode = "files"

    if mode == "all":
        # Show the overview, then language and detailed views
        print("=== 1. DOMAIN OVERVIEW ===")
        print(generate_report(metrics, mode="type", markdown=args.markdown, use_color=use_color, show_ratios=False))
        print("\n=== 2. LANGUAGE AND FORMAT VIEW ===")
        print(generate_report(metrics, mode="language", markdown=args.markdown, use_color=use_color, show_ratios=False))
        print("\n=== 3. DETAILED SUBDOMAIN VIEW ===")
        print(generate_report(metrics, mode="subtypes", markdown=args.markdown, use_color=use_color, show_ratios=True))
    else:
        report = generate_report(metrics, mode=mode, markdown=args.markdown, use_color=use_color)
        print(report)


if __name__ == "__main__":
    main()
