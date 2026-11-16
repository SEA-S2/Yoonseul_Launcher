"""Prepare integrity-checked official Fabric payloads for the launcher build.

The manifest snapshot is committed alongside this script. Updating the snapshot
is an explicit source change; normal launcher scans do not download manifests.
"""
from pathlib import Path
import base64
import hashlib
import io
import json
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets'
MANIFEST = ROOT / 'tools/girin_required_manifest.json'
CATALOG = ROOT / 'tools/girin_required_catalog.json'


def verified(data, entry):
    if len(data) != entry['size']:
        return False
    for algorithm in ('sha256', 'sha512', 'sha1'):
        if entry.get(algorithm):
            return hashlib.new(algorithm, data).hexdigest() == entry[algorithm]
    raise ValueError('Missing checksum: ' + entry['path'])


def payload(entry):
    cache = ROOT / '.girin-download-cache' / entry['path']
    if cache.is_file() and verified(cache.read_bytes(), entry):
        return cache.read_bytes()
    urls = [entry['url']]
    url = urllib.parse.urlparse(entry['url'])
    if url.hostname == 'dl.giraffemc.com' and url.path.startswith('/mods-dist/'):
        # Same official origin used by the updater's configured manifest endpoint.
        urls.append('http://giraffemc.com:8090/' + url.path.removeprefix('/mods-dist/') + ('?' + url.query if url.query else ''))
    failures = []
    for address in urls:
        try:
            request = urllib.request.Request(address, headers={'User-Agent': 'Yoonseul_Launcher-Build/2.7.2'})
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
            if not verified(data, entry):
                raise ValueError('Official payload checksum mismatch')
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
            return data
        except Exception as exc:
            failures.append(str(exc))
    raise RuntimeError(entry['path'] + ': ' + '; '.join(failures))


def main():
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    templates = json.loads(CATALOG.read_text(encoding='utf-8'))
    by_fabric_id = {entry['fabric_id']: entry for entry in templates}
    index = []
    ASSETS.mkdir(exist_ok=True)
    settings = {'options.txt': '설정은 처음 설치할 때만/options.txt',
                'config/emotecraft.json': '설정은 처음 설치할 때만/config/emotecraft.json',
                'config/giraffe-updater.json': 'config/giraffe-updater.json'}
    with zipfile.ZipFile(ASSETS / 'girin-required-seed.zip', 'w', zipfile.ZIP_DEFLATED) as bundle:
        for entry in manifest['files']:
            if entry.get('loader', 'fabric') != 'fabric' or entry.get('overwrite') == 'optional':
                continue
            path = entry['path']
            if path in settings:
                bundle.writestr(settings[path], payload(entry))
                continue
            if not path.startswith('mods/') or not path.endswith('.jar'):
                continue
            data = payload(entry)
            with zipfile.ZipFile(io.BytesIO(data)) as jar:
                metadata = json.loads(jar.read('fabric.mod.json'))
                if jar.testzip() is not None:
                    raise ValueError('Damaged official archive: ' + path)
                fid = metadata['id']
                if fid not in by_fabric_id:
                    raise ValueError('Unreviewed required mod: ' + fid)
                item = dict(by_fabric_id[fid])
                item.update(member=path, filename=Path(path).name, version=metadata['version'],
                            sha256=hashlib.sha256(data).hexdigest(), size_label=f'{len(data)/1024/1024:.1f} MB')
                icon = metadata.get('icon')
                icon = icon if isinstance(icon, str) else next(iter(icon.values()), None) if isinstance(icon, dict) else None
                if icon and icon in jar.namelist():
                    item['icon'] = 'data:image/png;base64,' + base64.b64encode(jar.read(icon)).decode('ascii')
                index.append(item)
            bundle.writestr(path, data)
    if {m['fabric_id'] for m in index} != set(by_fabric_id) or len(index) != len(templates):
        raise ValueError('Required mod set changed or contains duplicates')
    order = {m['id']: i for i, m in enumerate(templates)}
    index.sort(key=lambda m: order[m['id']])
    (ASSETS / 'girin-required-index.json').write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
    print('Verified official required payloads:', ', '.join(m['name'] + ' ' + m['version'] for m in index))


if __name__ == '__main__':
    main()
