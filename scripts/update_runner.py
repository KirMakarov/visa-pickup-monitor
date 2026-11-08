"""Copy the maintained template into an existing local runner checkout."""

import argparse
import re
import subprocess
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT / "examples/github-actions"
FILES = (
    ".github/workflows/monitor.yml",
    ".gitignore",
    "README.md",
    "config.env.example",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Existing local runner repository")
    parser.add_argument("--monitor-ref", required=True, help="Full SHA of tested, published public code")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing files")
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-fA-F]{40}", args.monitor_ref) is None:
        parser.error("--monitor-ref must be a full 40-character commit SHA.")

    destination = args.destination.expanduser().resolve()
    if not destination.is_dir() or destination.is_relative_to(PROJECT):
        parser.error("Destination must be an existing directory outside the public project.")
    try:
        top = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if Path(top).resolve() != destination:
            parser.error("Destination must be the root of the runner checkout.")
        dirty = subprocess.run(
            ["git", "-C", str(destination), "status", "--porcelain", "--untracked-files=all", "--", *FILES],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        parser.error("Could not inspect the destination Git repository.")
    if dirty:
        parser.error("Template files have local changes. Review and commit or move them before updating.")

    updates = []
    try:
        for name in FILES:
            target = destination / name
            for component in (target, *target.parents):
                if component == destination:
                    break
                if component.is_symlink():
                    parser.error(f"Refusing to update a symlink: {name}")
            payload = (TEMPLATE / name).read_bytes()
            if name == ".github/workflows/monitor.yml":
                payload, count = re.subn(
                    rb"(?m)^  MONITOR_REF: [0-9a-fA-F]{40}$",
                    b"  MONITOR_REF: " + args.monitor_ref.lower().encode("ascii"), payload,
                )
                if count != 1:
                    parser.error("Expected exactly one pinned MONITOR_REF in the template.")
            if target.exists() and not target.is_file():
                parser.error(f"Expected a regular file: {name}")
            if not target.exists() or target.read_bytes() != payload:
                updates.append((name, target, payload))

        for name, target, payload in updates:
            if not args.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            print(f"{'Would update' if args.dry_run else 'Updated'}: {name}")
    except OSError:
        parser.exit(1, "Could not read or write template files; inspect the destination before retrying.\n")
    if not updates:
        print("Template files are already up to date.")


if __name__ == "__main__":
    main()
