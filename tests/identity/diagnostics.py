"""Safe diagnostics for sandbox bootstrap failures (API process, port, hosted issuer).

Everything written here may end up in CI logs or public artifacts. It never carries
secrets, tokens, cookies, protocol query strings, emails or input values.
"""

import os
import re
import socket
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

ISSUER_DIRECT = "http://127.0.0.1:58081"
REALM = "bancaemdia-acceptance"
_TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}
_SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|DATABASE_URL|_DSN|COOKIE", re.IGNORECASE)
_PATTERNS = [
    # DSNs and URLs with credentials, then any query string (codes, states, action tokens).
    (re.compile(r"(\w[\w+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1<credentials>@"),
    (re.compile(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*"), r"\1?<query>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*"), "<jwt>"),
    # Whole header values: a Cookie line can hold several cookies separated by "; ".
    (re.compile(r"(?i)\b((?:set-)?cookie|authorization)(\s*:\s*)[^\n]*"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+\S+"), r"\1 <redacted>"),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|cookie|authorization|session)(\S*\s*[=:]\s*)\S+"
        ),
        r"\1\2<redacted>",
    ),
    # Long opaque runs (keys, codes, hashes); "/" is excluded so paths stay readable.
    (re.compile(r"[A-Za-z0-9_\-+=]{32,}"), "<opaque>"),
]


def sanitize(value: str, env: Mapping[str, str] | None = None) -> str:
    # Exact values first: a configured secret can be shorter than the generic patterns.
    for name, secret in (env or {}).items():
        if _SECRET_NAME.search(name) and secret and len(secret) >= 6:
            value = value.replace(secret, "<" + name.lower() + ">")
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def tail(path: Path, env: Mapping[str, str], lines: int = 40) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return "<no stderr captured>"
    return "\n".join(sanitize(line, env) for line in content) or "<empty stderr>"


def port_holders(port: int) -> list[str]:
    """Sockets currently bound to this local port (Linux /proc), without process arguments."""
    owners: dict[str, str] = {}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            for fd in (proc / "fd").iterdir():
                target = str(fd.readlink())
                if target.startswith("socket:["):
                    owners[target[8:-1]] = (proc / "comm").read_text().strip() + ":" + proc.name
        except OSError:
            continue
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            local, remote, state, inode = fields[1], fields[2], fields[3], fields[9]
            if int(local.rsplit(":", 1)[1], 16) != port:
                continue
            found.append(
                f"{_TCP_STATES.get(state, state)} remote_port={int(remote.rsplit(':', 1)[1], 16)}"
                f" owner={owners.get(inode, 'unknown')}"
            )
    return found


def bind_error(port: int) -> OSError | None:
    # Same SO_REUSEADDR as asyncio/uvicorn: TIME_WAIT is ignored, a live socket is not.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            return error
    return None


class Journal:
    """Append-only bootstrap log next to the test run; one sanitized line per event."""

    def __init__(self, directory: Path, env: Mapping[str, str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.env = env
        self.path = directory / "bootstrap.log"

    def record(self, step: str, detail: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {step}: {sanitize(detail, self.env)}\n"
        with self.path.open("a") as handle:
            handle.write(line)


async def hosted_state(page: Any) -> dict[str, Any]:
    """Where a hosted issuer page stopped: path, safe action name, title, issuer message."""
    url = urlsplit(page.url)
    execution = dict(parse_qsl(url.query)).get("execution", "")
    try:
        title = await page.title()
        controls = await page.locator("input").evaluate_all(
            "nodes=>nodes.filter(n=>n.type!=='hidden').map(n=>n.id||n.name)"
        )
        feedback = await page.locator(
            ".kc-feedback-text, [id^='input-error'], .alert, .pf-v5-c-alert__title"
        ).all_inner_texts()
    except Exception as error:  # Diagnostics must never replace the original failure.
        return {"path": url.path, "unreadable": type(error).__name__}
    return {
        "path": url.path,
        # Keycloak action names (UPDATE_PASSWORD, VERIFY_EMAIL) only; never codes or tokens.
        "execution": execution if re.fullmatch(r"[A-Z_]{3,40}", execution) else "",
        "title": sanitize(title),
        "controls": controls,
        "feedback": [sanitize(item.strip()) for item in feedback if item.strip()][:3],
    }


async def issuer_user(email: str) -> dict[str, Any]:
    """Issuer-side state of one disposable user, read directly (not through fault injection)."""
    try:
        async with httpx.AsyncClient(base_url=ISSUER_DIRECT, timeout=10) as client:
            grant = await client.post(
                "/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": os.environ["KC_BOOTSTRAP_ADMIN_USERNAME"],
                    "password": os.environ["KC_BOOTSTRAP_ADMIN_PASSWORD"],
                },
            )
            grant.raise_for_status()
            headers = {"Authorization": "Bearer " + grant.json()["access_token"]}
            base = f"/admin/realms/{REALM}/users"
            users = (
                await client.get(base, params={"email": email, "exact": "true"}, headers=headers)
            ).json()
            if len(users) != 1:
                return {"users": len(users)}
            user = users[0]
            credentials = (
                await client.get(f"{base}/{user['id']}/credentials", headers=headers)
            ).json()
    except (httpx.HTTPError, KeyError, ValueError) as error:
        return {"issuer_admin": type(error).__name__}
    return {
        "enabled": user.get("enabled"),
        "emailVerified": user.get("emailVerified"),
        "requiredActions": user.get("requiredActions", []),
        "credentials": sorted(c.get("type", "?") for c in credentials),
    }
