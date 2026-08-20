#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./github-daily-report.sh
#   ./github-daily-report.sh 2026-08-19
#
# Dependencies:
#   gh
#   jq
#   GNU date
#
# Private repositories require an authenticated gh token with access to them.
#
# For classic PATs, "repo" is normally sufficient.
# contributionsCollection also needs read:user for private contributions.

DAY="${1:-$(date +%F)}"

# ---------------------------------------------------------------------------
# Dependencies / authentication
# ---------------------------------------------------------------------------

for cmd in gh jq date; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Missing dependency: $cmd" >&2
    exit 1
  }
done

gh auth status >/dev/null 2>&1 || {
  echo "gh is not authenticated. Run: gh auth login" >&2
  exit 1
}

if ! date -d "$DAY" +%F >/dev/null 2>&1; then
  echo "Invalid date: $DAY (expected YYYY-MM-DD)" >&2
  exit 1
fi

LOGIN="$(gh api user --jq .login)"

# ===========================================================================
# Date ranges
# ===========================================================================
#
# IMPORTANT:
#
# We intentionally use TWO different date ranges.
#
# 1. PROFILE_FROM / PROFILE_TO
#
#    Used for GitHub contributionsCollection.
#    This identifies the contribution DAY as displayed by GitHub.
#
# 2. DETAIL_FROM / DETAIL_TO
#
#    Used when retrieving actual commits from the REST API.
#
#    GitHub uses timezone information from the original Git commit timestamp
#    when assigning a commit to a contribution day.
#
#    REST API timestamps, however, are returned normalized to UTC.
#
#    Example:
#
#      Git author timestamp:
#        2026-08-20 00:04:32 +0200
#
#      REST API:
#        2026-08-19T22:04:32Z
#
#    Therefore REST resolution must use the local day's boundaries converted
#    to UTC, while GraphQL contribution counting must NOT use those boundaries.
# ===========================================================================

PROFILE_FROM="${DAY}T00:00:00Z"
PROFILE_TO="${DAY}T23:59:59Z"

NEXT_DAY="$(date -d "$DAY +1 day" +%F)"

DETAIL_FROM_LOCAL="$(
  date -d "$DAY 00:00:00" --iso-8601=seconds
)"

DETAIL_TO_LOCAL="$(
  date -d "$NEXT_DAY 00:00:00" --iso-8601=seconds
)"

DETAIL_FROM="$(
  date -u -d "$DETAIL_FROM_LOCAL" +'%Y-%m-%dT%H:%M:%SZ'
)"

DETAIL_TO="$(
  date -u -d "$DETAIL_TO_LOCAL" +'%Y-%m-%dT%H:%M:%SZ'
)"

# ---------------------------------------------------------------------------
# GraphQL: profile contributions
# ---------------------------------------------------------------------------

QUERY='
query($from: DateTime!, $to: DateTime!) {
  viewer {
    login

    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions

      commitContributionsByRepository(maxRepositories: 100) {
        repository {
          nameWithOwner
          isPrivate

          defaultBranchRef {
            name
          }

          ghPages: ref(qualifiedName: "refs/heads/gh-pages") {
            name
          }
        }

        url

        contributions(first: 100) {
          nodes {
            occurredAt
            commitCount
            url
          }
        }
      }

      pullRequestContributions(first: 100) {
        nodes {
          occurredAt

          pullRequest {
            number
            title
            url

            repository {
              nameWithOwner
            }
          }
        }
      }

      pullRequestReviewContributions(first: 100) {
        nodes {
          occurredAt

          repository {
            nameWithOwner
          }

          pullRequest {
            number
            title
            url

            author {
              login
            }
          }

          pullRequestReview {
            state
            submittedAt
            url
          }
        }
      }

      issueContributions(first: 100) {
        nodes {
          occurredAt

          issue {
            number
            title
            url

            repository {
              nameWithOwner
            }
          }
        }
      }
    }

    issueComments(
      first: 100
      orderBy: {
        field: UPDATED_AT
        direction: DESC
      }
    ) {
      nodes {
        createdAt
        url
        bodyText

        repository {
          nameWithOwner
        }

        issue {
          number
          title
          url
        }

        pullRequest {
          number
          title
          url
        }
      }
    }
  }
}
'

DATA="$(
  gh api graphql \
    -f query="$QUERY" \
    -f from="$PROFILE_FROM" \
    -f to="$PROFILE_TO"
)"

printf '# GitHub report — %s — @%s\n\n' "$DAY" "$LOGIN"

# ===========================================================================
# Commits
# ===========================================================================
#
# GraphQL is the source of truth for the contribution count.
#
# REST is only used to resolve:
#   - SHA
#   - title
#   - URL
#
# The REST date range uses the LOCAL day converted to UTC.
# ===========================================================================

printf '## Commits\n\n'

COMMIT_REPOS="$(
  jq \
    '.data.viewer.contributionsCollection.commitContributionsByRepository' \
    <<<"$DATA"
)"

commit_repos_count="$(jq 'length' <<<"$COMMIT_REPOS")"

if (( commit_repos_count == 0 )); then

  printf '_None._\n\n'

else

  while IFS=$'\t' read -r \
    repo \
    expected \
    is_private \
    default_branch \
    gh_pages_branch
  do

    private_label=""

    if [[ "$is_private" == "true" ]]; then
      private_label=" [private]"
    fi

    printf '### %s — %s commit(s)%s\n\n' \
      "$repo" \
      "$expected" \
      "$private_label"

    tmp="$(mktemp)"

    branches=()

    if [[ -n "$default_branch" && "$default_branch" != "null" ]]; then
      branches+=("$default_branch")
    fi

    if [[
      -n "$gh_pages_branch" &&
      "$gh_pages_branch" != "null" &&
      "$gh_pages_branch" != "$default_branch"
    ]]; then
      branches+=("$gh_pages_branch")
    fi

    # -----------------------------------------------------------------------
    # Resolve commits using the local contribution day's UTC boundaries.
    #
    # Example in Europe/Paris during summer:
    #
    #   DAY=2026-08-20
    #
    # becomes:
    #
    #   since = 2026-08-19T22:00:00Z
    #   until = 2026-08-20T22:00:00Z
    #
    # This correctly catches a Git commit authored at:
    #
    #   2026-08-20 00:04:32 +0200
    #
    # which REST exposes as:
    #
    #   2026-08-19T22:04:32Z
    # -----------------------------------------------------------------------

    for branch in "${branches[@]}"; do

      if ! gh api \
        --paginate \
        -X GET \
        "repos/$repo/commits" \
        -f sha="$branch" \
        -f author="$LOGIN" \
        -f since="$DETAIL_FROM" \
        -f until="$DETAIL_TO" \
        -F per_page=100 \
      | jq -c '
          .[]
          | {
              sha,
              url: .html_url,
              date: .commit.author.date,
              title: (.commit.message | split("\n")[0])
            }
        ' >>"$tmp"
      then
        echo \
          "Warning: failed to resolve commits for $repo on branch $branch" \
          >&2
      fi

    done

    RESOLVED="$(
      jq -s '
        unique_by(.sha)
        | sort_by(.date)
      ' "$tmp"
    )"

    rm -f "$tmp"

    found="$(jq 'length' <<<"$RESOLVED")"

    if (( found == 0 )); then

      printf -- \
        '- GitHub counts %s contribution commit(s), but commit details were not resolved.\n' \
        "$expected"

    else

      jq -r '
        .[]
        | "- `\(.sha[0:7])` \(.title) — \(.url)"
      ' <<<"$RESOLVED"

    fi

    if (( found != expected )); then
      printf '\n> Note: profile count = %s, detailed commits found = %s.\n' \
        "$expected" \
        "$found"
    fi

    printf '\n'

  done < <(
    jq -r '
      .data.viewer.contributionsCollection.commitContributionsByRepository[]
      | [
          .repository.nameWithOwner,
          ([.contributions.nodes[].commitCount] | add // 0),
          .repository.isPrivate,
          (.repository.defaultBranchRef.name // ""),
          (.repository.ghPages.name // "")
        ]
      | @tsv
    ' <<<"$DATA"
  )

fi

# ===========================================================================
# Pull requests created
# ===========================================================================

printf '## Pull requests created\n\n'

PR_CREATED="$(
  jq \
    '.data.viewer.contributionsCollection.pullRequestContributions.nodes' \
    <<<"$DATA"
)"

if [[ "$(jq 'length' <<<"$PR_CREATED")" -eq 0 ]]; then

  printf '_None._\n\n'

else

  jq -r '
    .[]
    | "- \(.pullRequest.repository.nameWithOwner)#\(.pullRequest.number) — \(.pullRequest.title)\n  \(.pullRequest.url)"
  ' <<<"$PR_CREATED"

  printf '\n'

fi

# ===========================================================================
# Pull request reviews
# ===========================================================================

printf '## Pull request reviews\n\n'

REVIEWS="$(
  jq \
    '.data.viewer.contributionsCollection.pullRequestReviewContributions.nodes' \
    <<<"$DATA"
)"

if [[ "$(jq 'length' <<<"$REVIEWS")" -eq 0 ]]; then

  printf '_None._\n\n'

else

  jq -r '
    .[]
    | "- [\(.pullRequestReview.state)] \(.repository.nameWithOwner)#\(.pullRequest.number) — \(.pullRequest.title)\n  \(.pullRequestReview.url // .pullRequest.url)"
  ' <<<"$REVIEWS"

  printf '\n'

fi

# ===========================================================================
# My pull requests merged
#
# PR authored by me whose merge date is DAY.
# ===========================================================================

printf '## My pull requests merged\n\n'

MERGED_PRS="$(
  gh search prs \
    --author "$LOGIN" \
    --merged-at "$DAY" \
    --limit 100 \
    --json repository,number,title,url
)"

if [[ "$(jq 'length' <<<"$MERGED_PRS")" -eq 0 ]]; then

  printf '_None._\n\n'

else

  jq -r '
    .[]
    | "- \(.repository.nameWithOwner)#\(.number) — \(.title)\n  \(.url)"
  ' <<<"$MERGED_PRS"

  printf '\n'

fi

# ===========================================================================
# Issues created
# ===========================================================================

printf '## Issues created\n\n'

ISSUES_CREATED="$(
  jq \
    '.data.viewer.contributionsCollection.issueContributions.nodes' \
    <<<"$DATA"
)"

if [[ "$(jq 'length' <<<"$ISSUES_CREATED")" -eq 0 ]]; then

  printf '_None._\n\n'

else

  jq -r '
    .[]
    | "- \(.issue.repository.nameWithOwner)#\(.issue.number) — \(.issue.title)\n  \(.issue.url)"
  ' <<<"$ISSUES_CREATED"

  printf '\n'

fi

# ===========================================================================
# My issues closed
#
# Issue authored by me whose close date is DAY.
# ===========================================================================

printf '## My issues closed\n\n'

CLOSED_ISSUES="$(
  gh search issues \
    --author "$LOGIN" \
    --closed "$DAY" \
    --limit 100 \
    --json repository,number,title,url
)"

if [[ "$(jq 'length' <<<"$CLOSED_ISSUES")" -eq 0 ]]; then

  printf '_None._\n\n'

else

  jq -r '
    .[]
    | "- \(.repository.nameWithOwner)#\(.number) — \(.title)\n  \(.url)"
  ' <<<"$CLOSED_ISSUES"

  printf '\n'

fi

# ===========================================================================
# Issue / PR comments
#
# Comments are not contribution graph objects, so use the local day's actual
# time boundaries converted to UTC.
# ===========================================================================

printf '## Comments\n\n'

COMMENTS="$(
  jq \
    --arg from "$DETAIL_FROM" \
    --arg to "$DETAIL_TO" \
    '[
      .data.viewer.issueComments.nodes[]
      | select(
          .createdAt >= $from
          and
          .createdAt < $to
        )
    ]' \
    <<<"$DATA"
)"

if [[ "$(jq 'length' <<<"$COMMENTS")" -eq 0 ]]; then

  printf '_None._\n\n'

else

  jq -r '
    .[]

    | (
        .bodyText
        | gsub("[\r\n]+"; " ")
        | if length > 120
          then .[0:117] + "..."
          else .
          end
      ) as $body

    | if .pullRequest then
        "- PR comment: \(.repository.nameWithOwner)#\(.pullRequest.number) — \($body)\n  \(.url)"
      else
        "- Issue comment: \(.repository.nameWithOwner)#\(.issue.number) — \($body)\n  \(.url)"
      end
  ' <<<"$COMMENTS"

  printf '\n'

fi

# ===========================================================================
# Summary
# ===========================================================================

printf '## Summary\n\n'

printf -- '- Commits: %s\n' \
  "$(
    jq -r \
      '.data.viewer.contributionsCollection.totalCommitContributions' \
      <<<"$DATA"
  )"

printf -- '- PRs created: %s\n' \
  "$(
    jq -r \
      '.data.viewer.contributionsCollection.totalPullRequestContributions' \
      <<<"$DATA"
  )"

printf -- '- PR reviews: %s\n' \
  "$(
    jq -r \
      '.data.viewer.contributionsCollection.totalPullRequestReviewContributions' \
      <<<"$DATA"
  )"

printf -- '- PRs merged (authored by me): %s\n' \
  "$(jq 'length' <<<"$MERGED_PRS")"

printf -- '- Issues created: %s\n' \
  "$(
    jq -r \
      '.data.viewer.contributionsCollection.totalIssueContributions' \
      <<<"$DATA"
  )"

printf -- '- Issues closed (authored by me): %s\n' \
  "$(jq 'length' <<<"$CLOSED_ISSUES")"

printf -- '- Comments: %s\n' \
  "$(jq 'length' <<<"$COMMENTS")"
