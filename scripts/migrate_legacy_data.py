#!/usr/bin/env python3
"""One-time migration of legacy gigabot data (per-guild settings YAML files and
playlists.yml) into neo-gigabot's SQLite storage.

Usage:
    python scripts/migrate_legacy_data.py [--source PATH] [--dry-run]

Run this once, by hand, during cutover - it is not invoked automatically on
bot startup. Safe to re-run: settings migration overwrites by guild ID
(idempotent), and playlist migration re-imports duplicates as new entries with
a numeric suffix rather than overwriting, so avoid running it twice against
the same live storage unless you want that.

The equivalent is also available as a live bot command (/owner migrate) for
convenience, but running it here first with --dry-run is the safer way to
preview what would happen against your real data before touching anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.migration import default_legacy_path, migrate_all  # noqa: E402
from utils.storage import ensure_storage_layout  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Root directory of the old gigabot install (contains storage/settings/, storage/playlists.yml). "
        "Defaults to a sibling 'gigabot' directory next to this project.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=PROJECT_ROOT / "storage",
        help="neo-gigabot storage directory to migrate into. Defaults to this project's own storage/.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be migrated without writing anything.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source or default_legacy_path(PROJECT_ROOT)

    if not source.is_dir():
        print(f"Source directory does not exist: {source}", file=sys.stderr)
        return 1

    settings_dir = source / "storage" / "settings"
    playlists_file = source / "storage" / "playlists.yml"
    reaction_roles_file = source / "storage" / "reactionroles.yml"
    if not settings_dir.is_dir() and not playlists_file.is_file() and not reaction_roles_file.is_file():
        print(
            f"None of {settings_dir}, {playlists_file}, or {reaction_roles_file} exist - is --source correct?",
            file=sys.stderr,
        )
        return 1

    if not args.dry_run:
        ensure_storage_layout(args.target)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Migrating from {source} into {args.target}\n")

    summary = migrate_all(source, args.target, dry_run=args.dry_run)

    print(f"Guild settings: {summary.settings.migrated} migrated")
    if summary.settings.skipped_files:
        print(f"  Skipped (not a guild file): {', '.join(summary.settings.skipped_files)}")
    if summary.settings.failed:
        print(f"  FAILED ({len(summary.settings.failed)}):")
        for name, error in summary.settings.failed:
            print(f"    - {name}: {error}")

    print()
    print(f"Playlists: {summary.playlists.playlists_migrated} migrated, {summary.playlists.songs_migrated} songs total")
    if summary.playlists.failed:
        print(f"  FAILED ({len(summary.playlists.failed)}):")
        for name, error in summary.playlists.failed:
            print(f"    - {name}: {error}")

    print()
    print(f"Reaction roles: {summary.reaction_roles.mappings_migrated} mappings migrated")
    if summary.reaction_roles.empty_messages_skipped:
        print(f"  Skipped {summary.reaction_roles.empty_messages_skipped} message(s) with no mappings left.")
    if summary.reaction_roles.failed:
        print(f"  FAILED ({len(summary.reaction_roles.failed)}):")
        for name, error in summary.reaction_roles.failed:
            print(f"    - {name}: {error}")

    print()
    if summary.ok:
        print("Done, no errors." if not args.dry_run else "Dry run complete, no errors. Re-run without --dry-run to apply.")
        return 0

    print("Done, but some items failed - see above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
