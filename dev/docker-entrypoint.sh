#!/bin/sh
set -e
mkdir -p /data
chown -R 1000:1000 /data
export HOME=/tmp
exec setpriv --reuid=1000 --regid=1000 --clear-groups "$@"
