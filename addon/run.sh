#!/usr/bin/with-contenv bashio

# As a Home Assistant add-on, the user's options live in /data/options.json; export
# the ones that are set as environment variables so the app reads config the same way
# in both modes. Standalone containers have no options.json and pass these env vars
# directly via Docker.
if bashio::fs.file_exists '/data/options.json'; then
  bashio::config.has_value 'mqtt_host'   && export MQTT_HOST="$(bashio::config 'mqtt_host')"
  bashio::config.has_value 'mqtt_port'   && export MQTT_PORT="$(bashio::config 'mqtt_port')"
  bashio::config.has_value 'mqtt_user'   && export MQTT_USER="$(bashio::config 'mqtt_user')"
  bashio::config.has_value 'mqtt_pass'   && export MQTT_PASS="$(bashio::config 'mqtt_pass')"
  bashio::config.has_value 'ha_base_url' && export HA_BASE_URL="$(bashio::config 'ha_base_url')"
  bashio::config.has_value 'ha_token'    && export HA_TOKEN="$(bashio::config 'ha_token')"
fi

exec python3 /app/main.py
