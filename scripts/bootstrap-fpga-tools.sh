#!/usr/bin/env bash
# Developer/CI setup only. Windows users load the resulting .bit directly.
set -euo pipefail

if [[ ${1:-} == --help ]]; then
  cat <<'HELP'
Usage: bash scripts/bootstrap-fpga-tools.sh [tools-directory] [toolchain-json]

Downloads SHA256-pinned Linux x86-64 OSS CAD Suite and openXC7 bundles.
The openXC7 bundle includes a matching Artix-7 100T chipdb and Project X-Ray
database/converters. Requires Bash, curl, tar, sha256sum, and Python >= 3.11.
No privileged commands, Vivado, fuzzers, C++ compilation or hardware access.
This is a developer/CI script, not a Windows installation prerequisite.
HELP
  exit 0
fi

ARTY_PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ARTY_TOOLS_DIR=${1:-"$ARTY_PROJECT_ROOT/scratch/fpga-tools"}
ARTY_CONFIG_FILE=${2:-"$ARTY_PROJECT_ROOT/build/toolchain.ci.json"}
ARTY_PYTHON=${ARTY_BUILD_PYTHON:-python3}
ARTY_OSS_TAG=2026-03-24
ARTY_XC7_TAG=2026-09-30
ARTY_OSS_FILENAME=oss-cad-suite-linux-x64-20260324.tgz
ARTY_XC7_FILENAME=openxc7-toolchain-linux-x86-64-20260930.tgz
ARTY_OSS_SHA=69f5b5f306c92ea73322cda570242a7a1e4c49e416288750a56cd0feab49c155
ARTY_XC7_SHA=2a4128908d848423005933ec2c5c2960213368f4b4b9a65287a7d898357335da
ARTY_OSS_URL="https://github.com/YosysHQ/oss-cad-suite-build/releases/download/$ARTY_OSS_TAG/$ARTY_OSS_FILENAME"
ARTY_XC7_URL="https://github.com/cavearr/toolchain-openxc7-releases/releases/download/$ARTY_XC7_TAG/$ARTY_XC7_FILENAME"

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  printf 'This developer script requires Linux x86-64. Windows users load the published .bit.\n' >&2
  exit 1
fi
for ARTY_REQUIRED_COMMAND in curl tar sha256sum "$ARTY_PYTHON"; do
  if ! command -v "$ARTY_REQUIRED_COMMAND" >/dev/null; then
    printf 'Missing prerequisite: %s\n' "$ARTY_REQUIRED_COMMAND" >&2
    exit 1
  fi
done
"$ARTY_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python >= 3.11 required"'
mkdir -p -- "$ARTY_TOOLS_DIR" "$(dirname -- "$ARTY_CONFIG_FILE")"
ARTY_TOOLS_DIR=$(cd -- "$ARTY_TOOLS_DIR" && pwd)
ARTY_CONFIG_FILE=$("$ARTY_PYTHON" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$ARTY_CONFIG_FILE")
mkdir -p -- "$ARTY_TOOLS_DIR/downloads" "$ARTY_TOOLS_DIR/openxc7"

download_verified() {
  local url=$1 filename=$2 digest=$3
  local archive="$ARTY_TOOLS_DIR/downloads/$filename"
  if [[ ! -f $archive ]] || ! printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status; then
    curl --fail --location --retry 3 --connect-timeout 20 --max-time 900 \
      --output "$archive.download" "$url"
    printf '%s  %s\n' "$digest" "$archive.download" | sha256sum --check
    mv -- "$archive.download" "$archive"
  fi
  printf '%s  %s\n' "$digest" "$archive" | sha256sum --check
}

download_verified "$ARTY_OSS_URL" "$ARTY_OSS_FILENAME" "$ARTY_OSS_SHA"
download_verified "$ARTY_XC7_URL" "$ARTY_XC7_FILENAME" "$ARTY_XC7_SHA"
tar -xzf "$ARTY_TOOLS_DIR/downloads/$ARTY_OSS_FILENAME" -C "$ARTY_TOOLS_DIR"
tar -xzf "$ARTY_TOOLS_DIR/downloads/$ARTY_XC7_FILENAME" -C "$ARTY_TOOLS_DIR/openxc7"

for ARTY_EXECUTABLE in \
  "$ARTY_TOOLS_DIR/oss-cad-suite/bin/yosys" \
  "$ARTY_TOOLS_DIR/oss-cad-suite/bin/iverilog" \
  "$ARTY_TOOLS_DIR/openxc7/bin/nextpnr-xilinx" \
  "$ARTY_TOOLS_DIR/openxc7/bin/fasm2frames" \
  "$ARTY_TOOLS_DIR/openxc7/bin/xc7frames2bit"; do
  test -x "$ARTY_EXECUTABLE"
done
test -s "$ARTY_TOOLS_DIR/openxc7/chipdb/chipdb-xc7a100t.bin"
test -s "$ARTY_TOOLS_DIR/openxc7/share/nextpnr/external/prjxray-db/artix7/xc7a100tcsg324-1/part.yaml"

"$ARTY_PYTHON" - "$ARTY_TOOLS_DIR" "$ARTY_CONFIG_FILE" "$ARTY_PROJECT_ROOT" \
  "$ARTY_OSS_URL" "$ARTY_OSS_SHA" "$ARTY_XC7_URL" "$ARTY_XC7_SHA" <<'PY'
import json
import pathlib
import subprocess
import sys

tools, config, project = map(pathlib.Path, sys.argv[1:4])
oss_url, oss_sha, xc7_url, xc7_sha = sys.argv[4:]
xc7 = tools / "openxc7"
settings = {
    "yosys": str(tools / "oss-cad-suite/bin/yosys"),
    "yosys_mapping": "abc9",
    "nextpnr_xilinx": str(xc7 / "bin/nextpnr-xilinx"),
    "nextpnr_backend": "himbaechel",
    "fasm2frames": str(xc7 / "bin/fasm2frames"),
    "xc7frames2bit": str(xc7 / "bin/xc7frames2bit"),
    "chipdb": str(xc7 / "chipdb/chipdb-xc7a100t.bin"),
    "prjxray_db": str(xc7 / "share/nextpnr/external/prjxray-db/artix7"),
    "part": "xc7a100tcsg324-1",
    "build_dir": str(project / "build"),
}
config.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
manifest = {
    "oss_cad_suite": {"url": oss_url, "sha256": oss_sha},
    "openxc7": {"url": xc7_url, "sha256": xc7_sha},
    "openxc7_build_info": json.loads((xc7 / "BUILD-INFO.json").read_text()),
    "chipdb_id": (xc7 / "chipdb/chipdb-id.txt").read_text().strip(),
    "yosys_version": subprocess.check_output([settings["yosys"], "-V"], text=True).strip(),
}
(project / "build/tool-versions.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Toolchain configuration: {config}")
print(f"Yosys: {manifest['yosys_version']}")
PY

printf 'Tools ready. Build from the project root with:\n'
printf '  python -m arty_frame_studio.cli build --toolchain "%s"\n' "$ARTY_CONFIG_FILE"
