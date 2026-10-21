"""Command-line entry point for peekseed.

    python -m peekseed init [--workspace PATH]
    python -m peekseed scan [--prune] [--link ID]
    python -m peekseed status | doctor
    python -m peekseed workspace path | move PATH
    python -m peekseed reindex --all --link ID [--dry-run] [--gc]
    python -m peekseed migrate [--apply]
    python -m peekseed serve [--host H] [--port P]
    python -m peekseed index --name "My docs" --file a.pdf   # explicit upload

`serve` is what most people want; the workspace commands exist so a headless
machine can ingest and repair a workspace without opening a browser. Both the CLI
and the API call the same modules, so the two can never drift apart.
"""

import argparse
import os
import sys


def _print_paths():
    from . import config
    paths = config.ensure()
    print("data root: %s" % paths.root)
    for label, value in (("workspace", paths.workspace_root),
                         ("documents", paths.documents_dir),
                         ("config", paths.config_dir),
                         ("datasets", paths.datasets_dir),
                         ("history", paths.history_dir)):
        print("  %-9s %s" % (label + ":", value))


def _progress_printer():
    def report(fraction, message):
        print("  [%3d%%] %s" % (int(max(0.0, min(1.0, fraction)) * 100), message),
              flush=True)
    return report


def _apply_workspace_argument(args):
    """Make ``--workspace`` win over the environment for this process."""
    value = getattr(args, "workspace", None)
    if not value:
        return
    os.environ["PEEKSEED_WORKSPACE"] = str(value)
    try:
        from . import config
        config.reset_cache()
    except Exception:  # pragma: no cover - config is always importable
        pass
    try:
        from . import links
        links.reset_cache()
    except Exception:  # pragma: no cover
        pass


def _active_embed_link(links, link_id=None):
    """Resolve the embed link a command should use, or None with a printed hint."""
    try:
        if link_id:
            link = links.get(link_id)
            if link is None:
                print("no link with id %r" % link_id, file=sys.stderr)
                return None
            if link.get("role") != "embed":
                print("link %r is not an embed link" % link_id, file=sys.stderr)
                return None
            if not link.get("enabled"):
                print("link %r is not enabled" % link_id, file=sys.stderr)
                return None
            return link
        return links.active("embed")
    except links.LinkConfigError as exc:
        print("the model links config is not readable: %s" % exc, file=sys.stderr)
        return None


def cmd_init(args):
    from . import workspace
    _apply_workspace_argument(args)
    report = workspace.init(args.workspace)
    print("peekseed is initialized; put documents in %s" % report["documents_dir"])
    print("then run `python -m peekseed scan`.")
    return 0


def cmd_scan(args):
    from . import links, scan
    _apply_workspace_argument(args)
    link = _active_embed_link(links, args.link)
    if link is None:
        print("no enabled embed link: scanning will chunk documents but cannot "
              "build an index (configure an embed link in the UI).")
    report = scan.scan(args.workspace, prune=args.prune, link=link,
                       progress=_progress_printer() if not args.quiet else None)
    counts = report.get("counts") or {}
    print("scanned %d file(s): %d new, %d changed, %d unchanged, %d missing, "
          "%d error(s)"
          % (report.get("scanned", sum(counts.values())),
             counts.get("new", 0), counts.get("changed", 0),
             counts.get("unchanged", 0), counts.get("missing", 0),
             counts.get("errors", 0)))
    for name, state in sorted((report.get("collections") or {}).items()):
        print("  %-20s %-12s %s source(s), %s chunk(s)%s"
              % (name, state.get("status", "?"), state.get("sources", 0),
                 state.get("chunks", 0),
                 (" - " + "; ".join(state.get("reasons") or []))
                 if state.get("reasons") else ""))
    return 0 if not counts.get("errors") else 1


def cmd_status(args):
    from . import workspace
    _apply_workspace_argument(args)
    report = workspace.status(args.workspace)
    paths = report.get("workspace") or {}
    print("workspace: %s" % (paths.get("root") or paths.get("workspace_root")))
    print("  documents: %s" % paths.get("documents_dir"))
    print("  derived:   %s" % paths.get("derived_dir"))
    print("  last scan: %s" % (report.get("last_scan") or "never"))
    collections = report.get("collections") or {}
    if not collections:
        print("  no collections yet; put documents in documents/ and run `scan`")
    for name, state in sorted(collections.items()):
        print("  %-20s %-12s %2d source(s) %4d chunk(s)%s"
              % (name, state.get("status", "?"), state.get("sources", 0),
                 state.get("chunks", 0),
                 (" - " + "; ".join(state.get("reasons") or []))
                 if state.get("reasons") else ""))
    errors = report.get("errors") or []
    for error in errors:
        print("  error: %s" % error, file=sys.stderr)
    return 0


def cmd_doctor(args):
    from . import workspace
    _apply_workspace_argument(args)
    report = workspace.doctor(args.workspace)
    paths = report.get("paths") or {}
    print("workspace: %s" % (paths.get("root") or paths.get("workspace_root")))
    for label in ("documents_dir", "derived_dir", "links_file"):
        if paths.get(label):
            print("  %-12s %s" % (label + ":", paths[label]))
    issues = report.get("issues") or []
    if not issues:
        print("doctor: no issues found")
    for issue in issues:
        print("  issue: %s" % issue)
    return 0 if not issues else 1


def cmd_workspace(args):
    from . import workspace
    _apply_workspace_argument(args)
    if args.action == "path":
        ws = workspace.resolve(args.workspace)
        print(str(ws.root))
        return 0
    if args.action == "move":
        if not args.target:
            print("`workspace move` needs a destination directory", file=sys.stderr)
            return 2
        report = workspace.move(args.target, explicit=args.workspace)
        print("workspace moved to %s" % report["root"])
        return 0
    print("unknown workspace action %r" % args.action, file=sys.stderr)
    return 2


def cmd_reindex(args):
    from . import config, ingest, links, workspace
    _apply_workspace_argument(args)
    link = _active_embed_link(links, args.link)
    if link is None:
        print("reindex needs an enabled embed link (--link ID)", file=sys.stderr)
        return 2
    ws = workspace.resolve(args.workspace)
    datasets_dir = ws.datasets_dir
    if not datasets_dir.is_dir():
        print("no datasets in this workspace; run `scan` first", file=sys.stderr)
        return 2
    directories = sorted(path for path in datasets_dir.iterdir() if path.is_dir())
    if args.collection:
        directories = [path for path in directories if path.name == args.collection]
        if not directories:
            print("no collection named %r" % args.collection, file=sys.stderr)
            return 2
    if not directories:
        print("nothing to reindex", file=sys.stderr)
        return 2
    exit_code = 0
    for directory in directories:
        try:
            report = ingest.reindex(directory, link, dry_run=args.dry_run,
                                    gc=args.gc)
        except ingest.IngestError as exc:
            print("reindex %s failed: %s" % (directory.name, exc), file=sys.stderr)
            exit_code = 1
            continue
        prefix = "dry-run: " if args.dry_run else ""
        print("%s%s: %s chunk(s), space %s -> %s, ~%s embed call(s), ~%s"
              % (prefix, directory.name, report.get("chunks", 0),
                 report.get("current_space") or "-", report.get("target_space"),
                 report.get("embed_calls", 0), report.get("estimate") or ""))
        for reason in report.get("reasons") or []:
            print("  reason: %s" % reason)
    return exit_code


def cmd_migrate(args):
    from . import migrate as migrate_module, workspace
    _apply_workspace_argument(args)
    ws = workspace.resolve(args.workspace)
    report = migrate_module.migrate(ws.derived_dir, workspace_root=ws.root,
                                    dry_run=not args.apply)
    print(migrate_module.format_report(report))
    return 0 if not report["errors"] else 1


def cmd_serve(args):
    from . import config, workspace
    _apply_workspace_argument(args)
    config.ensure()
    ws = workspace.resolve(args.workspace)
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed: pip install peekseed", file=sys.stderr)
        return 1
    from .app import create_app
    host = args.host or config.host()
    port = args.port or config.port()
    print("peekseed: http://%s:%d/  (workspace: %s)" % (host, port, ws.root),
          flush=True)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def cmd_index(args):
    """Explicit upload path (kept for compatibility with the pre-workspace UI)."""
    from . import config, links
    from . import ingest
    _apply_workspace_argument(args)
    config.ensure()
    if not args.file:
        print("at least one --file is required", file=sys.stderr)
        return 2
    link = _active_embed_link(links, args.link)
    if link is None:
        return 2
    dataset = ingest.create_dataset(config.paths(), args.name, note=args.note or "")
    for path in args.file:
        if not os.path.isfile(path):
            print("not a file: %s" % path, file=sys.stderr)
            return 2
        ingest.add_source(dataset, path)
    print("dataset %s: %d source file(s)" % (dataset["id"], len(args.file)))
    info = ingest.build_index(ingest.dataset_dir(dataset["id"]), link,
                              progress=_progress_printer())
    print("indexed %d chunks in space %s (dim %d)"
          % (info.get("n", 0), info.get("space"), info.get("dim", 0)))
    return 0


def cmd_version(args):
    from . import __version__
    print("peekseed %s" % __version__)
    return 0


def _add_workspace_argument(parser):
    parser.add_argument("--workspace", default=None, metavar="PATH",
                        help="workspace root (default: $PEEKSEED_WORKSPACE or ./workspace)")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="peekseed",
        description="A local-first AI research framework over your own documents.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="create the workspace tree and gitignore guard")
    _add_workspace_argument(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("scan", help="reconcile documents/ with the manifest")
    _add_workspace_argument(p)
    p.add_argument("--prune", action="store_true",
                   help="remove missing sources and their chunks (data loss)")
    p.add_argument("--link", default=None, help="embed link id (default: active)")
    p.add_argument("--quiet", action="store_true", help="no per-stage progress lines")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("status", help="workspace, collections and last scan")
    _add_workspace_argument(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="paths, gitignore/checkout, links, schemas")
    _add_workspace_argument(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("workspace", help="inspect or move the workspace")
    _add_workspace_argument(p)
    actions = p.add_subparsers(dest="action")
    actions.add_parser("path", help="print the resolved absolute workspace root")
    move = actions.add_parser("move", help="move the whole workspace tree")
    move.add_argument("target", help="new workspace directory")
    p.set_defaults(func=cmd_workspace)

    p = sub.add_parser("reindex", help="rebuild an index in a new embedding space")
    _add_workspace_argument(p)
    p.add_argument("--all", action="store_true", help="every collection")
    p.add_argument("--collection", default=None, help="one collection id")
    p.add_argument("--link", default=None, help="embed link id (default: active)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the scope and write nothing")
    p.add_argument("--gc", action="store_true",
                   help="remove generations older than CURRENT")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("migrate", help="upgrade older data schemas (dry-run default)")
    _add_workspace_argument(p)
    p.add_argument("--apply", action="store_true", help="write the upgrades")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("serve", help="run the local web server")
    _add_workspace_argument(p)
    p.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="bind port (default 8030)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("index", help="add files as a dataset and build its index")
    _add_workspace_argument(p)
    p.add_argument("--name", required=True, help="display name for the dataset")
    p.add_argument("--file", action="append", default=[], help="PDF to add (repeatable)")
    p.add_argument("--note", default="", help="optional note")
    p.add_argument("--link", default=None, help="embed link id (default: active)")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("version", help="print the version")
    p.set_defaults(func=cmd_version)
    return parser


def main(argv=None):
    # CLI runs read the project's .env as defaults; the script launcher also
    # exports it, but `python -m peekseed ...` should work on its own.
    from . import config
    config.load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
