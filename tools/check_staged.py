"""Check staged content before local commits; never print matched secret values."""
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
    failures = []
    patterns = [rb"sk-or-v1-[A-Za-z0-9_-]{20,}",
                rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                rb"(?m)^[ \t]*(?:OPENROUTER_API_KEY|OKX_API_KEY|OKX_API_SECRET|OKX_API_PASSPHRASE)[ \t]*=[ \t]*\S"]
    for path in filter(None, paths):
        if (path.startswith(("runtime/", "LocalSettings/", "data/"))
                or path.endswith("JEV_QUICKSTART_PRIVATE.md")
                or (path.split("/")[-1].startswith(".env") and path not in {".env.example", ".env.okx.example"})):
            failures.append((path, "private_path"))
            continue
        content = git("show", ":" + path)
        if any(re.search(pattern, content) for pattern in patterns):
            failures.append((path, "credential_pattern"))
    for path, reason in failures:
        print(reason + ": " + path)
    if failures:
        return 1
    print("Staged secret/path checks passed (pattern scan; not a universal secret detector).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
