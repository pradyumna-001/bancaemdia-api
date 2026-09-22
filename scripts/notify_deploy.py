"""Send a concise release event to the configured Slack incoming webhook."""

import json
import os
import subprocess
from urllib.request import Request, urlopen


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def main() -> None:
    webhook = os.environ["SLACK_WEBHOOK_URL"]
    sha = os.environ["RELEASE_SHA"]
    event = os.environ["DEPLOY_EVENT"]
    author = git("show", "-s", "--format=%an", sha)
    files = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
    summary = ", ".join(files[:10]) or "(no file changes)"
    if len(files) > 10:
        summary += f" (+{len(files) - 10} more)"
    payload = json.dumps({
        "text": (
            f"{event} | {os.environ['GITHUB_REPOSITORY']} | {sha} | "
            f"author: {author} | files: {summary} | "
            f"run: https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
            f"{os.environ['GITHUB_RUN_ID']}"
        )
    }).encode()
    request = Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"Slack notification failed: HTTP {response.status}")


if __name__ == "__main__":
    main()
