#!/bin/bash

set -exuo pipefail

ARTIFACT_TYPE=${1:-wheel}

if [[ "$ARTIFACT_TYPE" != "wheel" && "$ARTIFACT_TYPE" != "jar" ]]; then
  echo "Invalid artifact type: $ARTIFACT_TYPE"
  exit 1
fi

if [[ "$ARTIFACT_TYPE" == "wheel" ]]; then
  BRANCH_DESTINATION="branch_wheels"
  MASTER_DESTINATION="wheels"
  ARTIFACT_PATH=".whl"
else
  BRANCH_DESTINATION="branch_jars"
  MASTER_DESTINATION="jars"
  ARTIFACT_PATH=".jar/linux"
fi

export PATH=/opt/python/cp38-cp38/bin:$PATH
pip install -q aws_requests_auth boto3
./ci/env/env_info.sh

# Sync the directory to buildkite artifacts
rm -rf /artifact-mount/"$ARTIFACT_PATH" || true
mkdir -p /artifact-mount/"$ARTIFACT_PATH"
cp -r "$ARTIFACT_PATH" /artifact-mount/"$ARTIFACT_PATH"
chmod -R 777 /artifact-mount/"$ARTIFACT_PATH"

# Don't upload to artifacts if this is a pull request.
if [ "$BUILDKITE_PULL_REQUEST" != "false" ]; then exit 0; fi

# Upload to branch directory.
python .buildkite/copy_files.py --destination "$BRANCH_DESTINATION" --path "./$ARTIFACT_PATH"

# Upload to latest directory.
if [ "$BUILDKITE_BRANCH" == "master" ]; then python .buildkite/copy_files.py --destination "$MASTER_DESTINATION" --path "./$ARTIFACT_PATH"; fi
