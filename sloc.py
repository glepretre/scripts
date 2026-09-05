#!/usr/bin/env python3
"""
scripts/count_loc.py - Analyse statique intelligente des lignes de code (LOC / SLOC).

Fonctionnalités :
- Analyse intelligente : ignore les lockfiles générés (package-lock.json, uv.lock).
- Mesure précise : Total, Lignes vides, Commentaires et Lignes hors vide (SLOC).
- Ventilation multi-niveaux :
    * Par domaine fonctionnel (Backend, Frontend, Infra/DevOps, Config/Docs)
    * Par sous-type détaillé (Domaine, Tests, Migrations, Commandes, UI, Styles, etc.)
    * Par langage de programmation / format
    * Fichier par fichier
- Calcul des ratios d'ingénierie logicielle (Couverture de tests en LOC, ratio Backend/Frontend, etc.).
- Sorties disponibles : Console formatée (avec ou sans couleurs), Markdown (--markdown), JSON (--json).
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
# Modèle de données
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
# Règles de classification
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

    return "Autre"


def classify_file(rel_path: str) -> Tuple[str, str, bool]:
    """
    Retourne un tuple: (domaine_principal, sous_domaine, est_test)
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
            return ("Backend", "Backend - Commandes CLI", False)
        if norm.startswith("backend/cinema/"):
            return ("Backend", "Backend - Domaine & API", False)
        if norm.startswith("backend/config/") or norm == "backend/manage.py":
            return ("Backend", "Backend - Config & Core", False)
        if norm == "backend/pyproject.toml":
            return ("Configuration", "Config - Dépendances Python", False)
        if basename in ("Dockerfile", ".dockerignore"):
            return ("Infra / DevOps", "Docker & Déploiement", False)
        return ("Backend", "Backend - Autre", False)

    # 2. Frontend
    if norm.startswith("frontend/"):
        if norm.startswith("frontend/src/") and ("test" in norm or "spec" in norm):
            return ("Frontend", "Frontend - Tests", True)
        if norm.startswith("frontend/src/") and norm.endswith(".css"):
            return ("Frontend", "Frontend - Styles & Design", False)
        if norm.startswith("frontend/src/"):
            return ("Frontend", "Frontend - Composants & UI", False)
        if norm == "frontend/index.html":
            return ("Frontend", "Frontend - HTML Entrypoint", False)
        if norm == "frontend/package.json":
            return ("Configuration", "Config - Dépendances Node", False)
        if basename in ("Dockerfile", ".dockerignore", ".prettierignore"):
            return ("Infra / DevOps", "Docker & Déploiement", False)
        if any(norm.startswith(f"frontend/{prefix}") for prefix in ("vite.config", "vitest.config", "eslint.config", "tsconfig")):
            return ("Frontend", "Frontend - Outillage & Build", False)
        return ("Frontend", "Frontend - Autre", False)

    # 3. Infra & DevOps racine
    if norm in ("compose.yaml", "docker-compose.yml", ".gitignore", ".env.example", ".env"):
        return ("Infra / DevOps", "Docker & Déploiement", False)

    # 4. Documentation
    if norm.endswith(".md"):
        return ("Documentation", "Documentation Markdown", False)

    # 5. Configuration racine
    if basename in ("pyproject.toml", "package.json"):
        return ("Configuration", "Config - Dépendances", False)

    return ("Autre", "Fichiers Divers", False)


# ==============================================================================
# Analyseur de lignes et de commentaires
# ==============================================================================

def analyze_file_content(filepath: Path, language: str) -> Tuple[int, int, int]:
    """
    Analyse le contenu d'un fichier et renvoie :
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

    # État pour les commentaires multilignes
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
                # Si le délimiteur ne se ferme pas sur la même ligne
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

        # JSON et Markdown n'ont pas de commentaires syntaxiques de code traités ici

    return total, blank, comments


# ==============================================================================
# Collecte des fichiers
# ==============================================================================

def get_tracked_or_all_files(root: Path) -> List[str]:
    """
    Récupère la liste des fichiers à analyser.
    Privilégie 'git ls-files' pour ignorer d'office les fichiers non-versionnés / caches.
    En l'absence de git, utilise os.walk en ignorant les répertoires standards.
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

    # Fallback récursif manuel
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
# Formatage et Affichage
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
    """Génère un tableau ASCII ou Markdown propre."""
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
        # Style terminal élégant
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
# Rapport complet
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
    headers = ["Catégorie / Périmètre", "Fichiers", "Total", "Vide", "Commentaires", "Hors Vide (SLOC)", "% Code"]
    alignments = ["left", "right", "right", "right", "right", "right", "right"]

    if mode in ("type", "summary"):
        # Agrégation par domaine principal
        domain_data = aggregate_group(metrics, lambda m: m.domain)
        # Tri : Backend, Frontend, Infra, Config, Docs, etc.
        order = ["Backend", "Frontend", "Infra / DevOps", "Configuration", "Documentation", "Autre"]
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
            "TOTAL CODE SOURCE",
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
        table_str = format_table(["Sous-Domaine Détaillé", "Fichiers", "Total", "Vide", "Commentaires", "Hors Vide", "% Total"],
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
        table_str = format_table(["Langage / Format", "Fichiers", "Total", "Vide", "Commentaires", "Hors Vide", "% Total"],
                                 rows, alignments, total_row, markdown=markdown, use_color=use_color)
        output.append(table_str)

    elif mode == "files":
        f_headers = ["Chemin du fichier", "Domaine", "Langage", "Total", "Vide", "Comms", "Hors Vide"]
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

    # Ratios clés
    if show_ratios:
        backend_app = sum(m.non_blank_lines for m in metrics if m.subdomain == "Backend - Domaine & API")
        backend_tests = sum(m.non_blank_lines for m in metrics if m.subdomain == "Backend - Tests")
        backend_total = sum(m.non_blank_lines for m in metrics if m.domain == "Backend")
        frontend_total = sum(m.non_blank_lines for m in metrics if m.domain == "Frontend")
        frontend_app = sum(m.non_blank_lines for m in metrics if m.subdomain in ("Frontend - Composants & UI", "Frontend - Styles & Design"))
        frontend_tests = sum(m.non_blank_lines for m in metrics if m.subdomain == "Frontend - Tests")

        ratios = []
        if backend_app > 0 and backend_tests > 0:
            ratio_bt = (backend_tests / backend_app) * 100
            ratios.append(f"- **Ratio Tests Backend / Code Métier** : {ratio_bt:.1f} % ({backend_tests} lignes de tests pour {backend_app} lignes de code applicatif)")
        if frontend_total > 0 and backend_total > 0:
            ratio_bf = (backend_total / (backend_total + frontend_total)) * 100
            ratios.append(f"- **Équilibre Backend vs Frontend** : {ratio_bf:.1f} % Backend ({backend_total} L) / {100 - ratio_bf:.1f} % Frontend ({frontend_total} L)")
        if frontend_app > 0 and frontend_tests > 0:
            ratio_ft = (frontend_tests / frontend_app) * 100
            ratios.append(f"- **Ratio Tests Frontend / UI Code** : {ratio_ft:.1f} % ({frontend_tests} lignes de tests pour {frontend_app} lignes UI)")

        if ratios:
            output.append("\n### Ratios et Indicateurs Clés :\n" + "\n".join(ratios))

    return "\n".join(output)


# ==============================================================================
# Point d'entrée CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Analyse statique intelligente du nombre de lignes de code (LOC / SLOC hors vide)."
    )
    parser.add_argument(
        "--root",
        "-r",
        default=".",
        help="Chemin de la racine du projet (défaut : répertoire courant)",
    )
    parser.add_argument(
        "--by",
        choices=["type", "subtypes", "language", "files", "all"],
        default="type",
        help="Type d'agrégation principal (défaut : type)",
    )
    parser.add_argument(
        "--detailed",
        "-d",
        action="store_true",
        help="Affiche le découpage détaillé par sous-catégories (équivaut à --by subtypes)",
    )
    parser.add_argument(
        "--language",
        "-l",
        action="store_true",
        help="Affiche le découpage par langage de programmation (équivaut à --by language)",
    )
    parser.add_argument(
        "--files",
        "-f",
        action="store_true",
        help="Affiche le détail fichier par fichier (équivaut à --by files)",
    )
    parser.add_argument(
        "--markdown",
        "-m",
        action="store_true",
        help="Formatte la sortie sous forme de tableaux Markdown",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="Exporte l'ensemble des métriques brutes et agrégées au format JSON",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Désactive la coloration ANSI du terminal",
    )

    args = parser.parse_args()
    root_path = Path(args.root).resolve()

    if not root_path.is_dir():
        sys.stderr.write(f"Erreur : le répertoire '{root_path}' n'existe pas.\n")
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

    # Détermination du mode
    mode = args.by
    if args.detailed:
        mode = "subtypes"
    elif args.language:
        mode = "language"
    elif args.files:
        mode = "files"

    if mode == "all":
        # Affiche la vue globale, puis par langage, puis détaillée
        print("=== 1. VUE SYNTHÉTIQUE PAR DOMAINE ===")
        print(generate_report(metrics, mode="type", markdown=args.markdown, use_color=use_color, show_ratios=False))
        print("\n=== 2. VUE PAR LANGAGE ET FORMAT ===")
        print(generate_report(metrics, mode="language", markdown=args.markdown, use_color=use_color, show_ratios=False))
        print("\n=== 3. VUE DÉTAILLÉE PAR SOUS-DOMAINE ===")
        print(generate_report(metrics, mode="subtypes", markdown=args.markdown, use_color=use_color, show_ratios=True))
    else:
        report = generate_report(metrics, mode=mode, markdown=args.markdown, use_color=use_color)
        print(report)


if __name__ == "__main__":
    main()
