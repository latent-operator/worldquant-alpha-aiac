"""Conservative classification: executable files and agent policy require full checks."""
import argparse
import os
import subprocess


def docs_only(paths: list[str]) -> bool:
    return bool(paths) and all(
        (path.startswith(("docs/", "openspec/")) and path.endswith(".md"))
        or (path.startswith("openspec/changes/") and path.endswith("/.openspec.yaml"))
        for path in paths)


def changed_paths(base: str | None = None) -> list[str]:
    commands = [["git", "diff", "--name-only", base, "HEAD"]] if base else [
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"]]
    return sorted({path for command in commands for path in
                   subprocess.check_output(command, text=True).splitlines() if path})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    args = parser.parse_args()
    # CI dispatch without an explicit PR comparison must run the full suite.
    result = docs_only(changed_paths(args.base)) if args.base else False
    line = "docs_only=" + str(result).lower()
    print(line)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as file:
            file.write(line + "\n")


if __name__ == "__main__":
    main()
