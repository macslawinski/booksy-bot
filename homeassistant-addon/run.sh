#!/bin/sh
# The bot reads .env (re-read on every Booksy request) and the SQLite DB from the working directory.
DATA=/homeassistant/booksy-bot
mkdir -p "$DATA"
cd "$DATA" || exit 1
if [ ! -f .env ]; then
    echo "Brak $DATA/.env – wgraj go (File editor / Samba: config/booksy-bot/.env) i uruchom dodatek ponownie."
    exit 1
fi
exec python -u -m booksy_bot.bot
