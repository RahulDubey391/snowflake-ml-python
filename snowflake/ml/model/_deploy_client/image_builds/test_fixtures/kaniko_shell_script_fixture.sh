#!/bin/sh

# Set the file path to monitor
REGISTRY_CRED_PATH="/kaniko/.docker/config.json"
SESSION_TOKEN_PATH="/snowflake/session/token"

# Function to gracefully terminate the file monitoring job
cleanup() {
  echo "Stopping file monitoring job..."
  trap - INT TERM # Remove the signal handlers
  kill -- -$$ # Kill the entire process group. Extra $ to escape, the generated shell script should have two $.
}

generate_registry_cred() {
  AUTH_TOKEN=$(printf '0auth2accesstoken:%s' "$(cat ${SESSION_TOKEN_PATH})" | base64);
  echo '{"auths":{"mock_image_repo":{"auth":"'"$AUTH_TOKEN"'"}}}' | tr -d '\n' > $REGISTRY_CRED_PATH;
}

on_session_token_change() {
  # Get the initial checksum of the file
  CHECKSUM=$(md5sum "${SESSION_TOKEN_PATH}" | awk '{ print $1 }')
  # Run the command once before the loop
  echo "Monitoring session token changes in the background..."
  (
    while true; do
      # Get the current checksum of the file
      CURRENT_CHECKSUM=$(md5sum "${SESSION_TOKEN_PATH}" | awk '{ print $1 }')
      if [ "${CURRENT_CHECKSUM}" != "${CHECKSUM}" ]; then
        # Session token file has changed, regenerate registry credential.
        echo "Session token has changed. Regenerating registry auth credentials."
        generate_registry_cred
        CHECKSUM="${CURRENT_CHECKSUM}"
      fi
      # Wait for a short period of time before checking again
      sleep 1
    done
  )
}

run_kaniko() {
  # Run the Kaniko command in the foreground
  echo "Starting Kaniko command..."

  # Set cache ttl to a large value as snowservice registry doesn't support deleting cache anyway.
  # Compression level set to 1 for fastest compression/decompression speed at the cost of compression ration.
  /kaniko/executor \
    --dockerfile Dockerfile \
    --context dir:///stage/models/id/context \
    --destination=org-account.registry.snowflakecomputing.com/db/schema/repo/image:latest \
    --cache=true \
    --compressed-caching=false \
    --cache-copy-layers=false \
    --use-new-run \
    --snapshot-mode=redo \
    --cache-repo=mock_image_repo/cache \
    --cache-run-layers=true \
    --cache-ttl=8760h \
    --push-retry=3 \
    --image-fs-extract-retry=5 \
    --compression=zstd \
    --compression-level=1 \
    --log-timestamp
}

setup() {
  tar -C "/stage/models/id" -xf "/stage/models/id/context.tar.gz";
  generate_registry_cred
  # Set up the signal handlers
  trap cleanup TERM
}

setup

# Running kaniko job on the foreground and session token monitoring on the background. When session token changes,
# overwrite the existing registry cred file with the new session token.
on_session_token_change &
run_kaniko

# Capture the exit code from the previous kaniko command.
KANIKO_EXIT_CODE=$?
# Exit with the same exit code as the Kaniko command. This then triggers the cleanup function.
exit $KANIKO_EXIT_CODE
