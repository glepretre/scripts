#!/bin/bash
#
# sync-git-heads.sh
#
# Purpose:
#   Synchronizes the local 'origin/HEAD' reference with the remote's actual default branch.
#   This is useful when a repository changes its default branch (e.g., from 'master' to 'main')
#   and you want your local 'origin/HEAD' to reflect that change across multiple repositories.
#
# Behavior:
#   - Scans specified directories for Git repositories.
#   - Compares local 'refs/remotes/origin/HEAD' with the remote's HEAD.
#   - Updates the local reference using 'git remote set-head origin -a' if they differ.
#

# Default settings
DRY_RUN=false
INTERACTIVE=false
VERBOSE=false
TARGETS=()

usage() {
    echo "Usage: $(basename "$0") [OPTIONS] DIRECTORY [DIRECTORY...]"
    echo
    echo "Synchronize local origin/HEAD with the remote default branch."
    echo "This updates your local Git configuration to track the correct default branch"
    echo "on the remote (e.g., if it moved from 'master' to 'main')."
    echo
    echo "Options:"
    echo "  -n, --dry-run     Show what would be updated without making changes"
    echo "  -i, --interactive Prompt for confirmation before updating each repo"
    echo "  -v, --verbose     Show repositories that are already up to date"
    echo "  -h, --help        Show this help message"
    exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--dry-run) DRY_RUN=true; shift ;;
        -i|--interactive) INTERACTIVE=true; shift ;;
        -v|--verbose) VERBOSE=true; shift ;;
        -h|--help) usage ;;
        -*) echo "Unknown option: $1"; usage ;;
        *) TARGETS+=("$1"); shift ;;
    esac
done

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "Error: At least one target directory is required."
    usage
fi

[[ "$DRY_RUN" == "true" ]] && echo "--- DRY RUN MODE ---"

for base_dir in "${TARGETS[@]}"; do
    if [ ! -d "$base_dir" ]; then
        [[ "$VERBOSE" == "true" ]] && echo "Skipping non-existent directory: $base_dir"
        continue
    fi

    echo "Scanning $base_dir..."
    
    # Find all .git directories and get their parent paths
    find "$base_dir" -name ".git" -type d -prune | while read -r dotgit; do
        repo_dir=$(dirname "$dotgit")
        
        # Check if origin exists
        if ! git -C "$repo_dir" remote | grep -q "^origin$"; then
            continue
        fi

        # Get local origin/HEAD
        local_head=$(git -C "$repo_dir" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
        
        # Get remote HEAD (efficiently)
        remote_head=$(git -C "$repo_dir" ls-remote --symref origin HEAD 2>/dev/null | grep "^ref:" | awk '{print $2}' | sed 's|refs/heads/||')

        if [ -z "$remote_head" ]; then
            [[ "$VERBOSE" == "true" ]] && echo "  [?] $repo_dir: Could not determine remote HEAD"
            continue
        fi

        if [ "$local_head" != "$remote_head" ]; then
            if [ -z "$local_head" ]; then
                msg="unset -> $remote_head"
            else
                msg="$local_head -> $remote_head"
            fi

            if $DRY_RUN; then
                echo "  [*] $repo_dir: Would update ($msg)"
            else
                do_update=true
                if $INTERACTIVE; then
                    echo -n "  [?] Update $repo_dir ($msg)? [y/N] "
                    read -r REPLY < /dev/tty
                    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                        do_update=false
                    fi
                fi

                if $do_update; then
                    if git -C "$repo_dir" remote set-head origin -a > /dev/null 2>&1; then
                        echo "  [✓] $repo_dir: Updated ($msg)"
                    else
                        echo "  [✗] $repo_dir: Failed to update"
                    fi
                fi
            fi
        else
            if $VERBOSE; then
                echo "  [ ] $repo_dir: Up to date ($local_head)"
            fi
        fi
    done
done

echo "Done!"
