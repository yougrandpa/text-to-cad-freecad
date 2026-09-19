#!/usr/bin/env python3
"""Start the HTTP front end.

    python tools/serve.py                                  # 127.0.0.1:8000, ./data
    python tools/serve.py --data-dir .tcad_hand --port 8765
    python tools/serve.py --base-url http://127.0.0.1:8123/v1 --model stub-scripted

Command-line overrides are applied **in memory only** (``persist=False``): a
flag you typed once must not silently rewrite the settings you saved in the UI.
Use the settings dialog, or ``PUT /settings/llm``, to change what persists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--config", default=None, help="YAML config file")
    parser.add_argument("--provider", default=None, help="override the provider preset")
    parser.add_argument("--model", default=None, help="override the model name")
    parser.add_argument("--base-url", default=None, help="override the endpoint")
    parser.add_argument("--no-worker", action="store_true",
                        help="do not start FreeCAD (config/UI work only)")
    args = parser.parse_args()

    import uvicorn

    from tcad.config.loader import (
        REPO_ROOT,
        load_config,
        load_default_config,
        resolve_paths,
    )
    from tcad.core.wiring import build_services
    from tcad.server.app import create_app

    cfg = load_config(args.config) if args.config else load_default_config()
    if args.data_dir:
        cfg.storage.data_dir = args.data_dir
        # the database follows data_dir unless explicitly placed elsewhere
        cfg.storage.sqlite_path = ""
    resolve_paths(cfg, root=REPO_ROOT)

    services = build_services(cfg, start_worker=not args.no_worker)

    if args.provider or args.model or args.base_url:
        from tcad.config.settings import effective
        from tcad.core.wiring import apply_llm_settings

        settings = effective(cfg, cfg.storage.data_dir)
        overrides = {}
        if args.provider:
            overrides["provider"] = args.provider
        if args.model:
            overrides["model"] = args.model
        if args.base_url:
            overrides["base_url"] = args.base_url
        settings.llm = settings.llm.model_copy(update=overrides)
        applied = apply_llm_settings(services, settings, persist=False)
        print(f"override    ->  {applied['provider_label']} / {applied['model']} "
              f"@ {applied['base_url']}  (not persisted)")

    app = create_app(services, config=services.config)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️  绑定到 {args.host}：界面没有任何认证，可被同网段任何人访问。")

    print(f"tcad UI  ->  http://{args.host}:{args.port}/ui/")
    print(f"API docs ->  http://{args.host}:{args.port}/docs")
    print(f"data_dir ->  {services.config.storage.data_dir}")
    model = services.llm.descriptor
    print(f"model    ->  {model['provider_label']} / {model['model']} @ {model['base_url']}")
    print(f"worker   ->  {'running' if services.worker.is_alive() else 'not started'}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
