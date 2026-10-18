import argparse
import os
from pathlib import Path
from .store import Store


def load_env():
    p = Path('.env')
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('\"').strip("'"))


def main():
    load_env()
    parser = argparse.ArgumentParser(description='Venture Radar: local investment intelligence')
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8765)
    serve.add_argument('--strictPort', action='store_true', help=argparse.SUPPRESS)
    serve.add_argument('--empty', action='store_true', help='Do not load the historical starter dataset')
    sub.add_parser('refresh', help='Run discovery once; suitable for OS scheduling when the server is stopped')
    backup = sub.add_parser('backup')
    backup.add_argument('path')
    sub.add_parser('init', help='Initialize storage with sourced starter records')
    args = parser.parse_args()
    store = Store(os.getenv('RADAR_DATA_DIR', './data'))
    if args.command == 'backup':
        store.backup(args.path)
        print('Backup saved:', args.path)
        return
    if args.command == 'init' or (args.command == 'serve' and not args.empty):
        store.seed()
    from .lock import runner_lock
    with runner_lock(store.directory):
        if args.command == 'serve':
            from .server import create_server
            from .worker import Worker
            # The data-directory lock prevents competing scheduler/worker owners.
            server = create_server(store, args.host, args.port)
            worker = Worker(store)
            worker.start()
            print(f'Venture Radar: http://localhost:{args.port} | Data: {store.path.resolve()}', flush=True)
            print('OpenAI configured.' if os.getenv('OPENAI_API_KEY') else 'Viewing mode. Add OPENAI_API_KEY to .env for research.', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                worker.stop_event.set()
                server.server_close()
        elif args.command == 'refresh':
            from .research import run_research
            import json
            ident = store.enqueue()
            # No recovery here: do not reset another process\'s running job.
            job = store.claim(ident)
            if job:
                try:
                    result = run_research(store, job)
                    store.finish(job['id'], result=result)
                    print(json.dumps(result, indent=2))
                except Exception as e:
                    store.finish(job['id'], error=str(e)[:800])
                    raise SystemExit(str(e)) from None
        else:
            print('Initialized:', store.path)


if __name__ == '__main__':
    main()
