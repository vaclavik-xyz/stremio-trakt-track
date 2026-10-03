#!/bin/bash
# Přihlásí se ke Stremio účtu a uloží jen přístupový klíč (ne heslo).
# Heslo se zadává interaktivně, nejde do historie shellu ani do chatu.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Přihlášení ke Stremio účtu (uloží se pouze authKey, práva 600)."
read -r -p "Stremio e-mail: " EMAIL
read -r -s -p "Stremio heslo (nezobrazuje se): " PW
echo

printf '%s' "$PW" | python3 "$DIR/stremio_bridge.py" _login "$EMAIL"
unset PW
echo
echo "Teď můžeš spustit:  python3 \"$DIR/stremio_bridge.py\" fetch"
