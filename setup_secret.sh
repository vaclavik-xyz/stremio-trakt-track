#!/bin/bash
# Uloží Trakt API přihlašovací údaje do config.json (práva 600).
# Hodnoty se neukládají do historie shellu, nejdou na příkazovou řádku (ps je
# neukáže) a nikam se neposílají — do Pythonu tečou přes stdin.
set -euo pipefail
umask 077

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${TRAKT_TRACKER_HOME:-$DIR}/config.json"

if [ -f "$CFG" ]; then
  read -r -p "config.json už existuje, přepsat? [y/N] " ans
  case "$ans" in [yY]*) ;; *) echo "Zrušeno."; exit 0 ;; esac
fi

read -r -p "Client ID:  " CID
read -r -s -p "Client Secret (Enter = ponechat uložený / přeskočit):  " CSEC
echo
read -r -p "Redirect URI (Enter = urn:ietf:wg:oauth:2.0:oob):  " RURI

# printf je vestavěný příkaz bashe → hodnoty se neobjeví v seznamu procesů
printf '%s\n%s\n%s\n' "$CID" "$CSEC" "$RURI" | python3 -c '
import json, sys
sys.path.insert(0, sys.argv[1])
import common
cid, csec, ruri = (sys.stdin.readline().strip() for _ in range(3))
path = common.DATA_DIR / "config.json"
old = {}
if path.exists():
    try:
        old = json.loads(path.read_text())
    except ValueError:
        old = {}
data = {"client_id": cid, "redirect_uri": ruri or "urn:ietf:wg:oauth:2.0:oob"}
if csec:
    data["client_secret"] = csec
elif old.get("client_secret"):
    # Enter u secretu nesmí smazat uložený — bez něj se token sám neobnoví
    data["client_secret"] = old["client_secret"]
    print("Client Secret ponechán z dřívějška.")
if old.get("settings"):
    data["settings"] = old["settings"]      # vlastní nastavení přepsání přežije
common.atomic_write_json(path, data, indent=2)
print(f"Uloženo do {path} (práva 600).")
' "$DIR"
unset CSEC

echo
echo "Pokračuj:  python3 \"$DIR/track.py\" auth"
