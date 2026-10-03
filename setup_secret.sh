#!/bin/bash
# Uloží Trakt API přihlašovací údaje do config.json (práva 600).
# Hodnoty se neukládají do historie shellu a nikam se neposílají.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$DIR/config.json"

if [ -f "$CFG" ]; then
  read -r -p "config.json už existuje, přepsat? [y/N] " ans
  case "$ans" in [yY]*) ;; *) echo "Zrušeno."; exit 0 ;; esac
fi

read -r -p "Client ID:  " CID
read -r -s -p "Client Secret (Enter = přeskočit):  " CSEC
echo
read -r -p "Redirect URI (Enter = urn:ietf:wg:oauth:2.0:oob):  " RURI

python3 - "$CFG" "$CID" "$CSEC" "$RURI" <<'PY'
import json, os, pathlib, stat, sys
cfg_path, cid, csec, ruri = sys.argv[1], sys.argv[2].strip(), sys.argv[3].strip(), sys.argv[4].strip()
data = {"client_id": cid,
        "redirect_uri": ruri or "urn:ietf:wg:oauth:2.0:oob"}
if csec:
    data["client_secret"] = csec
p = pathlib.Path(cfg_path)
p.write_text(json.dumps(data, indent=2) + "\n")
os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
print(f"Uloženo do {p} (práva 600).")
PY

echo
echo "Pokračuj:  python3 \"$DIR/track.py\" auth"
