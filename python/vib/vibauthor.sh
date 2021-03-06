#!/bin/bash -xe
if hash vibauthor &> /dev/null; then
  vibauthor "$@"
  exit $?
fi
if ! hash docker &> /dev/null; then
  echo "Either vibauthor or Docker is required"
  exit 1
fi
docker pull lamw/vibauthor || true
top=$(git rev-parse --show-toplevel)
docker run -it -v "$top:$top" -w "$PWD" lamw/vibauthor vibauthor "$@"
