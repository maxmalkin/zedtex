"""Release packaging only; never run by extension users."""
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen
import zipfile

target = os.environ["TECTONIC_TARGET"]
url = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0/tectonic-0.17.0-" + target
destination = Path("dist/latex-rendering")
with tempfile.TemporaryDirectory() as temporary:
    archive = Path(temporary) / target
    for attempt in range(4):
        try:
            with urlopen(url, timeout=120) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            break
        except OSError:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
    if target.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            # Only the known compiler executable is extracted from the upstream archive.
            name = next(name for name in bundle.namelist() if name.endswith("tectonic.exe"))
            (destination / "tectonic.exe").write_bytes(bundle.read(name))
    else:
        with tarfile.open(archive) as bundle:
            member = next(member for member in bundle.getmembers() if Path(member.name).name == "tectonic")
            (destination / "tectonic").write_bytes(bundle.extractfile(member).read())
            (destination / "tectonic").chmod(0o755)
shutil.copy("LICENSE", destination / "LICENSE")
shutil.copytree(Path(sys.prefix) / "share/jupyter/nbconvert", destination / "_internal/share/jupyter/nbconvert", dirs_exist_ok=True)
# The extension supplies Zed's shared Node runtime to Playwright.
for name in ("node", "node.exe"):
    (destination / "_internal/playwright/driver" / name).unlink(missing_ok=True)
archive = Path("dist") / ("latex-rendering-" + os.environ["BUNDLE_PLATFORM"] + ".zip")
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            bundle.write(path, path.relative_to(destination))
print(f"Bundle: {archive.stat().st_size:,} bytes compressed; "
      f"{sum(path.stat().st_size for path in destination.rglob('*') if path.is_file()):,} bytes unpacked.")
