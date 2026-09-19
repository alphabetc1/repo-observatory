"""Temporary real HTTP server backed by recorded public GitHub fixtures."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from access import AccessStore
from cacheboard import atomic_json, normalize, now, stamp
from server import Handler, ThreadingHTTPServer
from sync import build_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        Handler.data_dir = Path(directory)
        records = {}
        for number in (38485, 38494):
            item = normalize(json.loads((Path(__file__).parent / 'fixtures' / f'{number}.json').read_text()))
            item['created_at'] = item['updated_at'] = stamp()
            records[str(number)] = item
        snapshot = build_snapshot(records, {}, now(), {})
        atomic_json(Handler.data_dir / 'snapshot.json', snapshot)
        atomic_json(Handler.data_dir / 'status.json', {'ok': True})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        origin = f'http://127.0.0.1:{server.server_port}'
        result = {'origin': origin}
        if args.auth:
            Handler.access = AccessStore(Handler.data_dir / 'access.sqlite', origin)
            result['invite'] = Handler.access.bootstrap('owner')['url']
        print(json.dumps(result), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
