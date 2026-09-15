#!/usr/bin/env python3
"""
sloc.py - Smart static analysis of lines of code (LOC / SLOC).

Usage:

# Full report (all views combined, default)
./sloc.py

# Summary by type
./sloc.py --by type

# Detailed breakdown by subcategory
./sloc.py --detailed

# Breakdown by programming language
./sloc.py --language

# File-by-file details
./sloc.py --files

# Full report (explicit form)
./sloc.py --by all

# Markdown output (convenient for pasting into a PR or documentation)
./sloc.py --markdown

# Full JSON export for CI/CD tooling
./sloc.py --json

Features:
- Smart analysis: ignores generated lockfiles (package-lock.json, uv.lock).
- Binary files (images, fonts, archives, etc.) are excluded from line counts.
- Accurate metrics: total, blank, comment, non-blank, and code lines (SLOC).
- Multi-level breakdown:
    * By functional domain
      (Backend, Frontend, Tests, Content, Infra/DevOps, Config/Docs)
    * By detailed subtype
      (Domain, Tests, Migrations, Commands, UI, Styles, etc.)
    * By programming language / format
    * File by file
- Software engineering ratios
  (test coverage by LOC, Backend/Frontend ratio, etc.).
- Available outputs: formatted console, Markdown (--markdown), JSON (--json).
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
    code_lines: int  # non_blank_lines - comment_lines


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
    "flake.lock",
}

IGNORED_FILE_SUFFIXES = {
    ".7z",
    ".avi",
    ".bmp",
    ".class",
    ".crt",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".log",
    ".mov",
    ".mp3",
    ".mp4",
    ".otf",
    ".pdf",
    ".png",
    ".pub",
    ".pyc",
    ".svg",
    ".tar",
    ".tgz",
    ".ttf",
    ".key",
    ".wasm",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}

IGNORED_FILE_ENDINGS = (".min.css", ".min.js", ".tsbuildinfo")

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


def read_shebang(filepath: Path) -> str:
    try:
        with filepath.open(
            "r", encoding="utf-8", errors="replace"
        ) as source_file:
            first_line = source_file.readline()
    except (OSError, UnicodeError):
        return ""
    return first_line if first_line.startswith("#!") else ""


def detect_language(
    path: str,
    filepath: Optional[Path] = None,
    shebang: Optional[str] = None,
) -> str:
    basename = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()

    if ext == ".py":
        return "Python"
    if ext == ".tsx":
        return "TypeScript (TSX)"
    if ext == ".jsx":
        return "JavaScript (JSX)"
    if ext == ".ts":
        return "TypeScript"
    if ext in (".js", ".mjs", ".cjs"):
        return "JavaScript"
    if ext in (".css", ".scss", ".sass", ".less"):
        return "CSS"
    if ext in (".html", ".htm"):
        return "HTML"
    if ext == ".vue":
        return "Vue"
    if ext in (".jinja", ".jinja2"):
        return "Jinja"
    if ext == ".mako":
        return "Mako"
    if ext == ".sql":
        return "SQL"
    if ext == ".nix":
        return "Nix"
    if ext == ".json":
        return "JSON"
    if ext in (".yaml", ".yml"):
        return "YAML"
    if ext == ".toml":
        return "TOML"
    if ext in (".md", ".mdx"):
        return "Markdown"
    if ext in (".sh", ".bash"):
        return "Shell"
    if basename.lower() in ("makefile", "gnumakefile") or basename.endswith(
        ".Makefile"
    ):
        return "Make"
    if basename == "justfile" or basename.endswith(".justfile"):
        return "Just"
    if ext in (".cfg", ".ini", ".properties") or basename in (
        ".babelrc",
        ".prettierrc",
        ".style.yapf",
        "setup.cfg",
        "tox.ini",
    ):
        return "Configuration"
    if ext in (".txt", ".in"):
        return "Text"
    if (
        basename in ("Dockerfile", "compose.yaml", "docker-compose.yml")
        or basename.startswith(".docker")
        or basename.startswith(".git")
        or basename.startswith(".prettier")
        or basename.startswith(".env")
    ):
        return "DevOps / Config"

    if not ext:
        if shebang is None:
            shebang = read_shebang(filepath or Path(path))

        if shebang:
            interpreter = shebang[2:].strip().split()
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

                if re.fullmatch(
                    r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable
                ):
                    return "Python"
                if executable in ("node", "nodejs", "deno", "bun"):
                    return "JavaScript"
                if executable in ("ts-node", "tsx"):
                    return "TypeScript"
                if executable in ("sh", "bash", "dash", "ksh", "zsh", "fish"):
                    return "Shell"

    return "Other"


def classify_file(
    rel_path: str,
    language: str,
    has_shebang: bool,
) -> Tuple[str, str, bool]:
    """
    Return a tuple: (primary_domain, subdomain, is_test).
    """
    basename = os.path.basename(rel_path)
    norm = rel_path.replace("\\", "/")
    lower_basename = basename.lower()
    parts = tuple(part.lower() for part in Path(norm).parts)

    test_directories = {"test", "tests", "__tests__", "e2e", "cypress"}
    is_test = bool(test_directories.intersection(parts)) or bool(
        re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", lower_basename)
    )
    if is_test:
        if "e2e" in parts or "cypress" in parts:
            subtype = "Tests - End-to-End"
        elif language in ("Python", "SQL"):
            subtype = "Tests - Backend"
        else:
            subtype = "Tests - Frontend"
        return ("Tests", subtype, True)

    if "articles" in parts and language == "Markdown":
        return ("Content", "Editorial Content", False)

    if language == "Markdown" or lower_basename.startswith(
        ("readme", "changelog")
    ):
        return ("Documentation", "Documentation", False)
    if lower_basename.startswith(("license", "copying")):
        return ("Documentation", "License", False)

    dependency_files = {
        "package.json",
        "pyproject.toml",
        "pipfile",
        "setup.py",
        "setup.cfg",
    }
    if lower_basename in dependency_files or "requirements" in parts:
        return ("Configuration", "Dependencies", False)

    config_names = {
        "alembic.ini",
        "jsconfig.json",
        "tsconfig.json",
        "next-env.d.ts",
    }
    config_prefixes = (
        "babel.config",
        "eslint.config",
        "jest.config",
        "next.config",
        "vite.config",
        "vitest.config",
        "webpack.config",
    )
    if lower_basename in config_names or lower_basename.startswith(
        config_prefixes
    ):
        return ("Configuration", "Tooling Configuration", False)

    if (
        language in ("Make", "Just", "Nix", "DevOps / Config")
        or basename == "Dockerfile"
        or any(part in parts for part in (".github", ".gitlab"))
        or lower_basename
        in ("compose.yaml", "docker-compose.yml", ".gitlab-ci.yml")
    ):
        return ("Infra / DevOps", "Build & Deployment", False)

    if any(part in parts for part in ("demo", "demos", "example", "examples")):
        return ("Examples", f"Examples - {language}", False)

    vendored_directories = {
        "third-party",
        "third_party",
        "tarteaucitron",
        "stork",
        "vendor",
        "vendors",
    }
    if vendored_directories.intersection(parts):
        return ("Vendor / Third-Party", f"Vendor - {language}", False)

    if any(part in parts for part in ("migration", "migrations", "alembic")):
        return ("Backend", "Backend - Migrations", False)

    if (
        any(part in parts for part in ("script", "scripts", "bin"))
        or has_shebang
    ):
        return ("Scripts", f"Scripts - {language}", False)

    frontend_languages = {
        "CSS",
        "HTML",
        "JavaScript",
        "JavaScript (JSX)",
        "Jinja",
        "Mako",
        "TypeScript",
        "TypeScript (TSX)",
        "Vue",
    }
    if language in frontend_languages:
        if language == "CSS":
            subtype = "Frontend - Styles"
        elif language in ("HTML", "Jinja", "Mako") or "templates" in parts:
            subtype = "Frontend - Templates"
        else:
            subtype = "Frontend - Application"
        return ("Frontend", subtype, False)

    if language in ("Python", "SQL") or any(
        part in parts for part in ("backend", "server", "supabase")
    ):
        return ("Backend", "Backend - Application", False)

    if language in ("JSON", "TOML", "YAML", "Configuration", "Text"):
        return ("Configuration", "Project Configuration", False)

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
                # Enter multiline mode if the delimiter does not close here.
                if stripped.count(delim) < 2 or len(stripped) == 3:
                    in_block_comment = True
                    block_delimiter = delim
                continue

            if stripped.startswith("#"):
                comments += 1
                continue

        elif language in (
            "TypeScript",
            "TypeScript (TSX)",
            "JavaScript",
            "JavaScript (JSX)",
            "CSS",
            "Vue",
        ):
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

        elif language in ("HTML", "Jinja", "Mako"):
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

        elif language == "SQL":
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
            if stripped.startswith("--"):
                comments += 1
                continue

        elif language in (
            "YAML",
            "TOML",
            "Shell",
            "Nix",
            "Make",
            "Just",
            "Configuration",
            "DevOps / Config",
        ):
            if stripped.startswith("#"):
                comments += 1
                continue

        # JSON and Markdown have no code comment syntax handled here

    return total, blank, comments


# ==============================================================================
# File collection
# ==============================================================================


def is_binary_file(filepath: Path) -> bool:
    """Use Git's NUL-byte heuristic to distinguish binary files from text."""
    try:
        with filepath.open("rb") as source_file:
            return b"\0" in source_file.read(8192)
    except OSError:
        return False


def should_ignore_file(rel_path: str) -> bool:
    """Exclude generated artifacts, lockfiles, and non-source assets."""
    basename = os.path.basename(rel_path)
    lower_path = rel_path.lower()
    suffix = Path(lower_path).suffix
    top_level = Path(lower_path).parts[0]
    return (
        basename in LOCKFILE_NAMES
        or top_level in ("certs", "keys")
        or suffix == ".lock"
        or suffix in IGNORED_FILE_SUFFIXES
        or lower_path.endswith(IGNORED_FILE_ENDINGS)
    )


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
        files = [
            line.strip() for line in proc.stdout.splitlines() if line.strip()
        ]
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
        if should_ignore_file(rel_path):
            continue

        full_path = root / rel_path
        if not full_path.is_file():
            continue
        if is_binary_file(full_path):
            continue

        shebang = read_shebang(full_path)
        lang = detect_language(rel_path, full_path, shebang)
        domain, subdomain, is_test = classify_file(
            rel_path, lang, bool(shebang)
        )
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
        lines.append(
            "| "
            + " | ".join(format_cell(h, i) for i, h in enumerate(headers))
            + " |"
        )
        sep = []
        for i, align in enumerate(alignments):
            if align == "right":
                sep.append("-" * (col_widths[i] - 1) + ":")
            else:
                sep.append(":" + "-" * (col_widths[i] - 1))
        lines.append("| " + " | ".join(sep) + " |")
        for row in rows:
            lines.append(
                "| "
                + " | ".join(format_cell(c, i) for i, c in enumerate(row))
                + " |"
            )
        if total_row:
            lines.append(
                "| "
                + " | ".join(format_cell(c, i) for i, c in enumerate(total_row))
                + " |"
            )
    else:
        # Terminal styling
        b = Colors.BOLD if use_color else ""
        r = Colors.RESET if use_color else ""

        sep_line = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
        lines.append(sep_line)
        lines.append(
            "| "
            + " | ".join(
                f"{b}{format_cell(h, i)}{r}" for i, h in enumerate(headers)
            )
            + " |"
        )
        lines.append(sep_line)
        for row in rows:
            formatted_cells = [format_cell(c, i) for i, c in enumerate(row)]
            lines.append("| " + " | ".join(formatted_cells) + " |")
        if total_row:
            lines.append(sep_line)
            formatted_tot = [
                f"{b}{format_cell(c, i)}{r}" for i, c in enumerate(total_row)
            ]
            lines.append("| " + " | ".join(formatted_tot) + " |")
        lines.append(sep_line)

    return "\n".join(lines)


def aggregate_group(
    metrics: List[FileMetric], group_key_fn
) -> Dict[str, Dict[str, int]]:
    grouped: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {
            "files": 0,
            "total": 0,
            "blank": 0,
            "comments": 0,
            "non_blank": 0,
            "code": 0,
        }
    )
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
    tot_code = sum(m.code_lines for m in metrics)

    output = []
    headers = [
        "Category / Scope",
        "Files",
        "Total",
        "Blank",
        "Comments",
        "Code (SLOC)",
        "% Codebase",
    ]
    alignments = ["left", "right", "right", "right", "right", "right", "right"]

    if mode in ("type", "summary"):
        # Aggregate by primary domain
        domain_data = aggregate_group(metrics, lambda m: m.domain)
        # Sort Backend, Frontend, Infra, Config, Docs, etc.
        order = [
            "Backend",
            "Frontend",
            "Tests",
            "Scripts",
            "Examples",
            "Infra / DevOps",
            "Configuration",
            "Documentation",
            "Content",
            "Vendor / Third-Party",
            "Other",
        ]
        sorted_keys = sorted(
            domain_data.keys(),
            key=lambda k: (
                order.index(k) if k in order else 99,
                -domain_data[k]["code"],
            ),
        )

        rows = []
        for k in sorted_keys:
            d = domain_data[k]
            pct = (d["code"] / tot_code * 100) if tot_code else 0
            rows.append(
                [
                    k,
                    str(d["files"]),
                    f"{d['total']:,}",
                    f"{d['blank']:,}",
                    f"{d['comments']:,}",
                    f"{d['code']:,}",
                    f"{pct:5.1f} %",
                ]
            )

        total_row = [
            "TOTAL ANALYZED",
            str(len(metrics)),
            f"{tot_total:,}",
            f"{tot_blank:,}",
            f"{tot_comments:,}",
            f"{tot_code:,}",
            "100.0 %",
        ]
        table_str = format_table(
            headers,
            rows,
            alignments,
            total_row,
            markdown=markdown,
            use_color=use_color,
        )
        output.append(table_str)

    elif mode in ("subtypes", "detailed"):
        sub_data = aggregate_group(metrics, lambda m: m.subdomain)
        sorted_keys = sorted(
            sub_data.keys(), key=lambda k: -sub_data[k]["code"]
        )

        rows = []
        for k in sorted_keys:
            d = sub_data[k]
            pct = (d["code"] / tot_code * 100) if tot_code else 0
            rows.append(
                [
                    k,
                    str(d["files"]),
                    f"{d['total']:,}",
                    f"{d['blank']:,}",
                    f"{d['comments']:,}",
                    f"{d['code']:,}",
                    f"{pct:5.1f} %",
                ]
            )

        total_row = [
            "TOTAL",
            str(len(metrics)),
            f"{tot_total:,}",
            f"{tot_blank:,}",
            f"{tot_comments:,}",
            f"{tot_code:,}",
            "100.0 %",
        ]
        table_str = format_table(
            [
                "Detailed Subdomain",
                "Files",
                "Total",
                "Blank",
                "Comments",
                "Code (SLOC)",
                "% Codebase",
            ],
            rows,
            alignments,
            total_row,
            markdown=markdown,
            use_color=use_color,
        )
        output.append(table_str)

    elif mode == "language":
        lang_data = aggregate_group(metrics, lambda m: m.language)
        sorted_keys = sorted(
            lang_data.keys(), key=lambda k: -lang_data[k]["code"]
        )

        rows = []
        for k in sorted_keys:
            d = lang_data[k]
            pct = (d["code"] / tot_code * 100) if tot_code else 0
            rows.append(
                [
                    k,
                    str(d["files"]),
                    f"{d['total']:,}",
                    f"{d['blank']:,}",
                    f"{d['comments']:,}",
                    f"{d['code']:,}",
                    f"{pct:5.1f} %",
                ]
            )

        total_row = [
            "TOTAL",
            str(len(metrics)),
            f"{tot_total:,}",
            f"{tot_blank:,}",
            f"{tot_comments:,}",
            f"{tot_code:,}",
            "100.0 %",
        ]
        table_str = format_table(
            [
                "Language / Format",
                "Files",
                "Total",
                "Blank",
                "Comments",
                "Code (SLOC)",
                "% Codebase",
            ],
            rows,
            alignments,
            total_row,
            markdown=markdown,
            use_color=use_color,
        )
        output.append(table_str)

    elif mode == "files":
        f_headers = [
            "File Path",
            "Domain",
            "Language",
            "Total",
            "Blank",
            "Comments",
            "Code (SLOC)",
        ]
        f_align = ["left", "left", "left", "right", "right", "right", "right"]
        sorted_m = sorted(metrics, key=lambda m: (m.domain, -m.code_lines))
        rows = [
            [
                m.path,
                m.domain,
                m.language,
                f"{m.total_lines:,}",
                f"{m.blank_lines:,}",
                f"{m.comment_lines:,}",
                f"{m.code_lines:,}",
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
            f"{tot_code:,}",
        ]
        output.append(
            format_table(
                f_headers,
                rows,
                f_align,
                total_row,
                markdown=markdown,
                use_color=use_color,
            )
        )

    # Key ratios
    if show_ratios:
        backend_app = sum(
            m.code_lines
            for m in metrics
            if m.subdomain == "Backend - Application"
        )
        backend_tests = sum(
            m.code_lines for m in metrics if m.subdomain == "Tests - Backend"
        )
        backend_total = sum(
            m.code_lines for m in metrics if m.domain == "Backend"
        )
        frontend_total = sum(
            m.code_lines for m in metrics if m.domain == "Frontend"
        )
        frontend_app = frontend_total
        frontend_tests = sum(
            m.code_lines
            for m in metrics
            if m.subdomain in ("Tests - Frontend", "Tests - End-to-End")
        )
        application_total = backend_app + frontend_app
        test_total = sum(m.code_lines for m in metrics if m.domain == "Tests")

        ratios = []
        if application_total > 0 and test_total > 0:
            ratio_tests = (test_total / application_total) * 100
            ratios.append(
                "- **Tests / Application Code Ratio**: "
                f"{ratio_tests:.1f}% ({test_total} test lines for "
                f"{application_total} application lines)"
            )
        if backend_app > 0 and backend_tests > 0:
            ratio_bt = (backend_tests / backend_app) * 100
            ratios.append(
                "- **Backend Tests / Application Code Ratio**: "
                f"{ratio_bt:.1f}% ({backend_tests} test lines for "
                f"{backend_app} application code lines)"
            )
        if frontend_total > 0 and backend_total > 0:
            ratio_bf = (backend_total / (backend_total + frontend_total)) * 100
            ratios.append(
                "- **Backend vs Frontend Balance**: "
                f"{ratio_bf:.1f}% Backend ({backend_total} lines) / "
                f"{100 - ratio_bf:.1f}% Frontend ({frontend_total} lines)"
            )
        if frontend_app > 0 and frontend_tests > 0:
            ratio_ft = (frontend_tests / frontend_app) * 100
            ratios.append(
                "- **Frontend Tests / UI Code Ratio**: "
                f"{ratio_ft:.1f}% ({frontend_tests} test lines for "
                f"{frontend_app} UI lines)"
            )

        if ratios:
            output.append(
                "\n### Key Ratios and Indicators:\n" + "\n".join(ratios)
            )

    return "\n".join(output)


# ==============================================================================
# CLI entry point
# ==============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Smart static analysis of lines of code (LOC / SLOC)."
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
        default="all",
        help="Primary aggregation type (default: all)",
    )
    parser.add_argument(
        "--detailed",
        "-d",
        action="store_true",
        help=(
            "Show the detailed breakdown by subcategory "
            "(equivalent to --by subtypes)"
        ),
    )
    parser.add_argument(
        "--language",
        "-l",
        action="store_true",
        help=(
            "Show the breakdown by programming language "
            "(equivalent to --by language)"
        ),
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
            },
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
        print(
            generate_report(
                metrics,
                mode="type",
                markdown=args.markdown,
                use_color=use_color,
                show_ratios=False,
            )
        )
        print("\n=== 2. LANGUAGE AND FORMAT VIEW ===")
        print(
            generate_report(
                metrics,
                mode="language",
                markdown=args.markdown,
                use_color=use_color,
                show_ratios=False,
            )
        )
        print("\n=== 3. DETAILED SUBDOMAIN VIEW ===")
        print(
            generate_report(
                metrics,
                mode="subtypes",
                markdown=args.markdown,
                use_color=use_color,
                show_ratios=True,
            )
        )
    else:
        report = generate_report(
            metrics, mode=mode, markdown=args.markdown, use_color=use_color
        )
        print(report)


if __name__ == "__main__":
    main()
