#!/bin/sh
# Create the password file if it is missing, so Mosquitto can start before
# Petlibro Local has added any login. Mosquitto drops to the "mosquitto" user
# after start and can only read a password file that user owns.
set -e

mkdir -p /mosquitto/auth
[ -f /mosquitto/auth/passwd ] || install -m 0600 /dev/null /mosquitto/auth/passwd
chown mosquitto:mosquitto /mosquitto/auth /mosquitto/auth/passwd

exec /docker-entrypoint.sh "$@"
