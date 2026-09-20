"""Download the selected, fixed local embedding runtime and model."""
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.local-services'
RELEASE = 'b10964'  # Official v0.4.1/nightly-tag.txt target, verified at deployment.
BASE = f'https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}'
FILES = [
    (f'{BASE}/llama-{RELEASE}-bin-win-cuda-12.4-x64.zip', 'downloads/llama-cuda.zip',
     '264f20d7ee3860aecca9ec12418357a9f3e80349a2b186f66c63859ded1a9593'),
    (f'{BASE}/cudart-llama-bin-win-cuda-12.4-x64.zip', 'downloads/cudart.zip',
     '8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6'),
    ('https://huggingface.co/second-state/jina-embeddings-v3-GGUF/resolve/61b5399b6dab4af55fd305574625d88f50241ca8/jina-embeddings-v3-Q8_0.gguf',
     'models/jina-embeddings-v3-Q8_0.gguf',
     'da95bb315ec9766aabfdfa920124a6997a5d9617bd7c9708c4195557136864e1'),
]


def checksum(path):
    h = hashlib.sha256()
    with path.open('rb') as src:
        for chunk in iter(lambda: src.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def download(item):
    url, relative, expected = item
    target = STATE / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (not expected or checksum(target) == expected):
        print(f'Already present: {relative}', flush=True)
    else:
        partial = target.with_suffix(target.suffix + '.part')
        # Resume interrupted transfers using HTTP ranges where supported.
        for attempt in range(4):
            try:
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {'User-Agent': 'AIDB-SQL-local-deploy'}
                if offset:
                    headers['Range'] = f'bytes={offset}-'
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as response:
                    append = offset > 0 and response.status == 206
                    if not append:
                        offset = 0
                    print(f'Downloading {relative} from byte {offset}', flush=True)
                    last = time.monotonic()
                    size = offset
                    with partial.open('ab' if append else 'wb') as dst:
                        while chunk := response.read(2 * 1024 * 1024):
                            dst.write(chunk)
                            size += len(chunk)
                            if time.monotonic() - last > 15:
                                print(f'{relative}: {size / 2**20:.1f} MiB', flush=True)
                                last = time.monotonic()
                digest = checksum(partial)
                if expected and digest != expected:
                    raise ValueError(f'SHA256 mismatch for {relative}: {digest}')
                partial.replace(target)
                break
            except Exception as exc:
                print(f'Attempt {attempt + 1}: {relative}: {type(exc).__name__}: {exc}', flush=True)
                if attempt == 3 or (isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404)) or isinstance(exc, ValueError):
                    raise
                time.sleep(2)
    if target.suffix == '.zip':
        destination = (STATE / 'llama').resolve()
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target) as archive:
            for member in archive.infolist():
                if not (destination / member.filename).resolve().is_relative_to(destination):
                    raise ValueError('Archive path escapes the runtime directory')
            archive.extractall(destination)
    result = {'url': url, 'path': relative, 'bytes': target.stat().st_size, 'sha256': checksum(target)}
    print(f'Verified: {relative}', flush=True)
    return result


if __name__ == '__main__':
    # Archives may contain overlapping runtime files, so extract sequentially.
    results = []
    for entry in FILES:
        results.append(download(entry))
    (STATE / 'downloads.json').write_text(json.dumps({'llama_release': RELEASE, 'files': results}, indent=2), encoding='utf-8')
