#!/bin/sh
# Create the simulation topic + push subscription on the Pub/Sub emulator.
# Idempotent: 409 (already exists) is treated as success so this can run on
# every `docker compose up`.
#
# Required env: EMULATOR_HOST, PROJECT, TOPIC, SUBSCRIPTION, PUSH_ENDPOINT
set -e

: "${EMULATOR_HOST:?EMULATOR_HOST not set}"
: "${PROJECT:?PROJECT not set}"
: "${TOPIC:?TOPIC not set}"
: "${SUBSCRIPTION:?SUBSCRIPTION not set}"
: "${PUSH_ENDPOINT:?PUSH_ENDPOINT not set}"

EMU="http://${EMULATOR_HOST}"
TOPIC_PATH="projects/${PROJECT}/topics/${TOPIC}"
SUB_PATH="projects/${PROJECT}/subscriptions/${SUBSCRIPTION}"

echo "Waiting for Pub/Sub emulator at ${EMU}..."
i=0
while :; do
  status=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "${EMU}/v1/${TOPIC_PATH}" || echo "000")
  case "$status" in
    200|409) break ;;
  esac
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then
    echo "Pub/Sub emulator never became ready (last status=$status)" >&2
    exit 1
  fi
  sleep 1
done
echo "Topic ready: ${TOPIC_PATH} (status=$status)"

status=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "${EMU}/v1/${SUB_PATH}" \
  -H 'Content-Type: application/json' \
  -d "{\"topic\":\"${TOPIC_PATH}\",\"pushConfig\":{\"pushEndpoint\":\"${PUSH_ENDPOINT}\"},\"ackDeadlineSeconds\":600}")
case "$status" in
  200|409) echo "Subscription ready: ${SUB_PATH} (status=$status)" ;;
  *) echo "Failed to create subscription: status=$status" >&2; exit 1 ;;
esac
