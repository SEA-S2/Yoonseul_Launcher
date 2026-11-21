"""Build the tested appearance/share changes on the published 0.3.0 runtime.

The baseline is bundled and pinned to avoid silently rebuilding unrelated ledger logic
from a different source snapshot. Only the listed GUI and share-text classes change.
Run with Python 3 and Java 25; dependency cache may be shared across CI runs.
"""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '0.3.2-test.1'
BASELINE_SHA256 = '35c922d80f785364dc2d5d51fdc5856e235dc81938cbc12994c4b8765c27c054'
BASELINE = ROOT / 'baseline/yoondding-fabric-0.3.0.jar'
CACHE = Path(os.environ.get('YOONDDING_BUILD_CACHE', ROOT / '.cache')).resolve()
OUT = ROOT / 'build'

def run(*args):
    subprocess.run([str(x) for x in args], check=True)

def download(job):
    url, path, sha1 = job
    if path.is_file() and (not sha1 or hashlib.sha1(path.read_bytes()).hexdigest() == sha1):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                data = response.read()
            if sha1 and hashlib.sha1(data).hexdigest() != sha1:
                raise ValueError(f'Dependency hash mismatch: {path.name}')
            path.write_bytes(data)
            return
        except Exception:
            if attempt == 2:
                raise

def main():
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == BASELINE_SHA256, 'Baseline changed'
    metadata = json.loads((ROOT / 'minecraft26.2.json').read_text())
    jobs = [(metadata['downloads']['client']['url'], CACHE/'minecraft-client.jar', metadata['downloads']['client']['sha1'])]
    for lib in metadata['libraries']:
        artifact = lib.get('downloads', {}).get('artifact')
        if artifact:
            jobs.append((artifact['url'], CACHE/'libraries'/artifact['path'], artifact['sha1']))
    jobs += [
        ('https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.19.3/fabric-loader-0.19.3.jar', CACHE/'fabric-loader.jar', None),
        ('https://maven.fabricmc.net/net/fabricmc/fabric-api/fabric-api/0.161.0+26.2/fabric-api-0.161.0+26.2.jar', CACHE/'fabric-api.jar', None),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(download, jobs))
    modules = CACHE/'fabric-modules'
    modules.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(CACHE/'fabric-api.jar') as archive:
        for name in archive.namelist():
            if name.startswith('META-INF/jars/') and name.endswith('.jar'):
                (modules/Path(name).name).write_bytes(archive.read(name))
    cp = os.pathsep.join(str(p) for p in [BASELINE, CACHE/'minecraft-client.jar', CACHE/'fabric-loader.jar', *sorted((CACHE/'libraries').rglob('*.jar')), *sorted(modules.glob('*.jar'))])
    jdk = Path(os.environ['JAVA_HOME'])/'bin'
    classes, test_classes = OUT/'classes', OUT/'test-classes'
    if OUT.exists():
        shutil.rmtree(OUT)
    classes.mkdir(parents=True)
    test_classes.mkdir()
    run(jdk/'javac', '--release', '25', '-encoding', 'UTF-8', '-cp', cp, '-d', classes, *sorted((ROOT/'src').rglob('*.java')))
    test_cp = str(classes) + os.pathsep + cp
    run(jdk/'javac', '-encoding', 'UTF-8', '-cp', test_cp, '-d', test_classes, ROOT/'ReceiptDesignsCheck.java', ROOT/'GirinCurrencyCheck.java')
    # Verify the compiled API surface, actual widget callbacks, persistence, and copy totals.
    run(jdk/'java', '-Xverify:all', '-cp', str(test_classes)+os.pathsep+test_cp, 'kr.aka.nakin.ui.ReceiptDesignsCheck')
    run(jdk/'java', '-Xverify:all', '-cp', str(test_classes)+os.pathsep+test_cp, 'kr.aka.nakin.core.GirinCurrencyCheck')
    replacement = {p.relative_to(classes).as_posix(): p.read_bytes() for p in classes.rglob('*.class')}
    replacement.update({p.relative_to(ROOT/'resources').as_posix(): p.read_bytes() for p in (ROOT/'resources').rglob('*') if p.is_file()})
    artifact = OUT/f'yoondding-fabric-{VERSION}.jar'
    with zipfile.ZipFile(BASELINE) as old:
        mod = json.loads(old.read('fabric.mod.json'))
        mod['version'] = VERSION
        replacement['fabric.mod.json'] = (json.dumps(mod, ensure_ascii=False, indent=2)+'\n').encode()
        identity = old.read('META-INF/nakin-update.properties').decode().replace('version=0.3.0', f'version={VERSION}')
        replacement['META-INF/nakin-update.properties'] = identity.encode()
        with zipfile.ZipFile(artifact, 'w', zipfile.ZIP_DEFLATED) as new:
            for info in old.infolist():
                new.writestr(info, replacement.pop(info.filename, old.read(info.filename)))
            for name, data in replacement.items():
                new.writestr(name, data)
    with zipfile.ZipFile(BASELINE) as old, zipfile.ZipFile(artifact) as new:
        assert new.testzip() is None
        allowed = ('kr/aka/nakin/ui/GuiTex', 'kr/aka/nakin/ui/LedgerHud', 'kr/aka/nakin/ui/DtStatScreen', 'kr/aka/nakin/ui/TexButton', 'kr/aka/nakin/ui/TabButton', 'kr/aka/nakin/aggregate/ShareText', 'kr/aka/nakin/core/CurrencyParser', 'kr/aka/nakin/config/DtConfig', 'kr/aka/nakin/config/LedgerSettings', 'kr/aka/nakin/DtLedgerClient', 'kr/aka/nakin/watcher/ChatWatcher')
        for name in old.namelist():
            if name not in ('fabric.mod.json', 'META-INF/nakin-update.properties') and not name.startswith(allowed):
                assert new.read(name) == old.read(name), f'Unrelated code changed: {name}'
        assert json.loads(new.read('fabric.mod.json'))['version'] == VERSION
    # Test the final distributable too, rather than only the loose compiler output.
    run(jdk/'java', '-Xverify:all', '-cp', os.pathsep.join((str(test_classes), str(artifact), cp)), 'kr.aka.nakin.ui.ReceiptDesignsCheck')
    run(jdk/'java', '-Xverify:all', '-cp', os.pathsep.join((str(test_classes), str(artifact), cp)), 'kr.aka.nakin.core.GirinCurrencyCheck')
    checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (OUT/f'{artifact.name}.sha256').write_text(f'{checksum}  {artifact.name}\n')
    print(f'Built and checked {artifact.name}: {checksum}')

if __name__ == '__main__':
    main()
