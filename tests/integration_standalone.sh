#!/usr/bin/env bash
# Integration check of docker-compose.standalone.yml against a real Mosquitto.
# Needs Docker with Compose v2.24+ and a pulled or pullable eclipse-mosquitto:2 image.
# Uses its own project name and host ports 18830/18765, and removes everything it creates.
#
#   bash tests/integration_standalone.sh
#
# Proves, through the app's own API: a new device login connects without restarting
# Mosquitto, a client that stays connected is not dropped, and a removed login is refused.
set -u
cd "$(dirname "$0")/.."

export COMPOSE_PROJECT_NAME=plit-it AUTH_TOKEN="$(openssl rand -hex 16)" MQTT_USER=petlibro MQTT_PASS="$(openssl rand -hex 8)"
C="docker compose -f docker-compose.standalone.yml -f tests/integration.override.yml"
NET=${COMPOSE_PROJECT_NAME}_default
COOKIES="$(pwd)/.integration-cookies"
FAILED=0

cleanup() {
  docker rm -f plit-it-sub >/dev/null 2>&1
  $C down -v >/dev/null 2>&1
  docker rmi plit-it-app >/dev/null 2>&1
  rm -f "$COOKIES"
}
trap cleanup EXIT

check() {  # check "description" expected_exit actual_exit
  if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected exit $2, got $3)"; FAILED=1; fi
}
pub() {  # pub user password message
  docker run --rm --network "$NET" eclipse-mosquitto:2 mosquitto_pub -h mosquitto -u "$1" -P "$2" -t it/x -m "$3" >/dev/null 2>&1
}
api() { curl -s -o /dev/null -w '%{http_code}' -b "$COOKIES" "$@"; }

$C up -d --build || exit 1
started=$(docker inspect -f '{{.State.StartedAt}}' "${COMPOSE_PROJECT_NAME}-mosquitto-1")

for _ in $(seq 30); do pub "$MQTT_USER" "$MQTT_PASS" ready && break; sleep 1; done
pub "$MQTT_USER" "$MQTT_PASS" app-login; check "the app's own login was created at startup" 0 $?
pub devuser1 devpass1 x; check "unknown device login is refused" 5 $?

curl -s -o /dev/null -c "$COOKIES" -X POST localhost:18765/api/login -H 'Content-Type: application/json' -d "{\"token\":\"$AUTH_TOKEN\"}"
check "Auto Setup fails fast with the built-in broker" 409 "$(api -X POST localhost:18765/api/capture/start)"

docker run -d --name plit-it-sub --network "$NET" eclipse-mosquitto:2 \
  mosquitto_sub -h mosquitto -u "$MQTT_USER" -P "$MQTT_PASS" -t 'it/#' >/dev/null
sleep 1

check "add device" 200 "$(api -X POST localhost:18765/api/devices -H 'Content-Type: application/json' \
  -d '{"serial":"AF03IT000001","name":"IT","device_type":"granary","model":"PLAF203","mqtt_user":"devuser1","mqtt_pass":"devpass1"}')"
sleep 1
pub devuser1 devpass1 from-device; check "new device login connects without a Mosquitto restart" 0 $?
pub devuser1 wrong x; check "wrong password is still refused" 5 $?

check "delete device and its login" 200 "$(api -X DELETE 'localhost:18765/api/devices/AF03IT000001?remove_creds=1')"
sleep 1
pub devuser1 devpass1 x; check "removed login is refused after the reload" 5 $?
pub "$MQTT_USER" "$MQTT_PASS" after-reload; check "other logins keep working" 0 $?
sleep 1
docker logs plit-it-sub 2>&1 | grep -qx after-reload; check "a client connected through both reloads still receives" 0 $?

[ "$(docker inspect -f '{{.State.StartedAt}}' "${COMPOSE_PROJECT_NAME}-mosquitto-1")" = "$started" ]
check "Mosquitto was never restarted" 0 $?

exit $FAILED
