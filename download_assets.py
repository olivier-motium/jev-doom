"""Download a pinned, freely licensed game from the official upstream release."""
from pathlib import Path
from urllib.request import urlopen
import hashlib, io, zipfile
ROOT = Path(__file__).resolve().parent
URL = 'https://github.com/freedoom/freedoom/releases/download/v0.13.0/freedoom-0.13.0.zip'
if all((ROOT/'assets'/p).exists() for p in ('freedoom2.wad','FREEDOOM-COPYING.txt','FREEDOOM-CREDITS.txt')):
    print('Freedoom already installed.'); raise SystemExit
print('Downloading Freedoom 0.13.0 from the official project…')
with urlopen(URL, timeout=120) as response: archive = response.read(100_000_000)
if hashlib.sha256(archive).hexdigest() != '3f9b264f3e3ce503b4fb7f6bdcb1f419d93c7b546f4df3e874dd878db9688f59':
    raise RuntimeError('Freedoom archive checksum mismatch')
with zipfile.ZipFile(io.BytesIO(archive)) as z:
    for name in ('freedoom2.wad', 'COPYING.txt', 'CREDITS.txt'):
        matches = [p for p in z.namelist() if p.rsplit('/',1)[-1] == name]
        if len(matches) != 1: raise RuntimeError(f'Missing or ambiguous {name} in upstream release')
        target = 'freedoom2.wad' if name.endswith('.wad') else f'FREEDOOM-{name}'
        (ROOT/'assets'/target).write_bytes(z.read(matches[0]))
print('Freedoom ready; its license and credits are in assets/.')
