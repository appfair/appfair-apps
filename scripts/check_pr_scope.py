"""Reject pull requests that change more than one app submission before planning builds."""

import argparse
import html
import os
from pathlib import Path
import subprocess


def changed_submissions(base: str) -> list[str]:
    # Use the event's pinned base, not a branch that can move while the run is starting.
    # Disabling rename detection includes both submissions when a file is renamed; deletions
    # count too. Do not fall back to the last commit, which can hide earlier PR changes.
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD", "--", "apps/"],
        check=True, capture_output=True, text=True,
    )
    return sorted({path for path in result.stdout.split("\0")
                   if path.startswith("apps/") and path.endswith((".yaml", ".yml"))})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--out", type=Path, default=Path("review"))
    args = parser.parse_args(argv)
    try:
        paths = changed_submissions(args.base)
    except subprocess.CalledProcessError as error:
        print("::error::Cannot determine the PR's changed submissions; refusing to plan builds.")
        print(error.stderr)
        return 1
    if len(paths) <= 1:
        print(f"Submission scope OK: {len(paths)} app submission(s) changed.")
        return 0

    message = (
        "<!-- appfair-review -->\n"
        "### Submission blocked: more than one app changed\n\n"
        f"This pull request changes **{len(paths)} app submission files**. "
        "Each submission PR must update only one app.\n\n"
        + "\n".join(f"- <code>{html.escape(path)}</code>" for path in paths)
        + "\n\nKeep only the intended app's changes in this PR. Restore the other submission "
        "files to their base-branch contents and submit those app updates in separate PRs. "
        "The check uses the entire PR diff, not its title or its latest commit; "
        "additions, edits, and deletions all count.\n\n"
        "No app builds or store submissions were started. Push the corrected diff to run "
        "the checks again.\n"
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "review.md").write_text(message)
    (args.out / "review-pr.txt").write_text(f"{args.pr}\n")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.write("blocked=true\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(message)
    print(f"::error::This PR changes {len(paths)} app submissions; only one app is allowed per PR. "
          "See the PR comment for the affected files and how to split the updates.")
    print(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
