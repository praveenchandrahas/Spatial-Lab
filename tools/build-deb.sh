#!/usr/bin/env bash
# Build an architecture-independent .deb of Spatial-Lab (pure Python, uses the system python3).
# Usage: tools/build-deb.sh [outdir]      (default outdir: dist)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-dist}"
VER="$(python3 -c 'import re;print(re.search(r"__version__ = \"(.+?)\"",open("spatial_lab/__init__.py").read()).group(1))')"
PKG="spatial-lab_${VER}_all"
ROOT="$(mktemp -d)/$PKG"
trap 'rm -rf "$(dirname "$ROOT")"' EXIT

install -d "$ROOT/DEBIAN" "$ROOT/usr/lib/python3/dist-packages" "$ROOT/usr/bin" "$ROOT/usr/share/doc/spatial-lab"
cp -r spatial_lab "$ROOT/usr/lib/python3/dist-packages/"
find "$ROOT" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$ROOT/usr/lib" -type d -exec chmod 755 {} + ; find "$ROOT/usr/lib" -type f -exec chmod 644 {} +

cat > "$ROOT/usr/bin/spatial-lab" <<'EOS'
#!/usr/bin/python3
import sys
from spatial_lab.cli import main
sys.exit(main())
EOS
chmod 755 "$ROOT/usr/bin/spatial-lab"
cp README.md "$ROOT/usr/share/doc/spatial-lab/README.md"
cp LICENSE "$ROOT/usr/share/doc/spatial-lab/copyright"

cat > "$ROOT/DEBIAN/control" <<EOC
Package: spatial-lab
Version: $VER
Section: sound
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-numpy, python3-scipy, python3-aiohttp, python3-yaml
Recommends: ffmpeg, pipewire-bin
Suggests: python3-matplotlib, camilladsp
Maintainer: Praveen Chandrahas <praveenchandrahas@users.noreply.github.com>
Homepage: https://github.com/praveenchandrahas/Spatial-Lab
Description: Interactive 3D audio test bench for a multichannel room
 Drag a sound object around a model of your room in a browser page and hear it
 on your real 5.1.2 speakers: VBAP panning with height, distance cues, phantom
 imaging, delay and polarity labs, paths and speaker identification. Sound is
 produced by a local Python engine through PipeWire; the web page only draws and
 sends commands. Starts muted and listens on 127.0.0.1 only.
EOC
mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$ROOT" "$OUT/$PKG.deb" >/dev/null
echo "$OUT/$PKG.deb"
