"""Command-line entry point for peekseed.

    python -m peekseed init
    python -m peekseed serve [--host H] [--port P]
    python -m peekseed index --name "My docs" --file a.pdf [--file b.pdf]
    python -m peekseed version

`serve` is what most people want; `index` exists so a headless machine can add
a dataset without opening a browser. Both paths use the same modules as the API,
so the two can never drift apart.
"""

import argparse
import os
import sys


def _print_paths():
    from . import config
    paths = config.ensure()
    print("data root: %s" % paths.root)
    for label, value in (("config", paths.config_dir),
                         ("datasets", paths.datasets_dir),
                         ("history", paths.history_dir)):
        print("  %-9s %s" % (label + ":", value))


def cmd_init(args):
    from . import config, links
    config.ensure()
    # Creating the link store on first run makes the empty config explicit and
    # surfaces a malformed one now rather than on the first request.
    links.load()
    _print_paths()
    print("peekseed is initialized; run `python -m peekseed serve`.")
    return 0


def cmd_serve(args):
    from . import config
    config.ensure()
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed: pip install -e .", file=sys.stderr)
        return 1
    from .app import create_app
    host = args.host or config.host()
    port = args.port or config.port()
    print("peekseed: http://%s:%d/  (data root: %s)"
          % (host, port, config.paths().root), flush=True)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def cmd_index(args):
    from . import config, links
    from . import ingest
    config.ensure()
    if not args.file:
        print("at least one --file is required", file=sys.stderr)
        return 2
    link_id = args.link
    if link_id:
        link = links.get(link_id)
        if link is None:
            print("no link with id %r" % link_id, file=sys.stderr)
            return 2
        if link.get("role") != "embed":
            print("link %r is not an embed link" % link_id, file=sys.stderr)
            return 2
        if not link.get("enabled"):
            print("link %r is not enabled" % link_id, file=sys.stderr)
            return 2
    else:
        link = links.active("embed")
        if link is None:
            print("no embed link is enabled; add one in the UI or set "
                  "PEEKSEED_EMBED_BASE/_MODEL", file=sys.stderr)
            return 2

    dataset = ingest.create_dataset(config.paths(), args.name, note=args.note or "")
    for path in args.file:
        if not os.path.isfile(path):
            print("not a file: %s" % path, file=sys.stderr)
            return 2
        ingest.add_source(dataset, path)
    print("dataset %s: %d source file(s)" % (dataset["id"], len(args.file)))

    def progress(frac, message):
        print("  [%3d%%] %s" % (int(max(0.0, min(1.0, frac)) * 100), message),
              flush=True)

    info = ingest.build_index(ingest.dataset_dir(dataset["id"]), link,
                              progress=progress)
    print("indexed %d chunks in space %s (dim %d)"
          % (info.get("n", 0), info.get("space"), info.get("dim", 0)))
    return 0


def cmd_version(args):
    from . import __version__
    print("peekseed %s" % __version__)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="peekseed",
        description="A local-first AI research framework over your own documents.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="create the data root and an empty config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="run the local web server")
    p.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="bind port (default 8030)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("index", help="add PDFs as a dataset and build its index")
    p.add_argument("--name", required=True, help="display name for the dataset")
    p.add_argument("--file", action="append", default=[],
                   help="PDF to add (repeatable)")
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
