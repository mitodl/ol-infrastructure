#!/usr/bin/env bash
# Bring up etcd, then run the test-nginx suite against it.
#
# APISIX.pm boots APISIX's real init path, which connects to etcd regardless of
# what a given test asserts. Starting etcd here keeps that noise out of
# error.log, which test-nginx checks by default.
set -euo pipefail

ETCD_DATA_DIR=${ETCD_DATA_DIR:-/tmp/etcd-data}
ETCD_READY_TIMEOUT=${ETCD_READY_TIMEOUT:-30}

rm -rf "${ETCD_DATA_DIR}"
mkdir -p "${ETCD_DATA_DIR}"

etcd \
    --data-dir "${ETCD_DATA_DIR}" \
    --listen-client-urls 'http://127.0.0.1:2379' \
    --advertise-client-urls 'http://127.0.0.1:2379' \
    --listen-peer-urls 'http://127.0.0.1:2380' \
    --initial-advertise-peer-urls 'http://127.0.0.1:2380' \
    --initial-cluster 'default=http://127.0.0.1:2380' \
    --log-level error \
    >/tmp/etcd.log 2>&1 &
etcd_pid=$!

cleanup() {
    kill "${etcd_pid}" 2>/dev/null || true
    wait "${etcd_pid}" 2>/dev/null || true
}
trap cleanup EXIT

# Poll rather than sleep: APISIX's init fails outright if etcd is not yet
# answering, which would surface as a confusing test failure.
deadline=$((SECONDS + ETCD_READY_TIMEOUT))
until etcdctl --endpoints=http://127.0.0.1:2379 endpoint health >/dev/null 2>&1 \
      || curl -fsS -o /dev/null http://127.0.0.1:2379/version 2>/dev/null; do
    if (( SECONDS >= deadline )); then
        echo "etcd did not become ready within ${ETCD_READY_TIMEOUT}s" >&2
        cat /tmp/etcd.log >&2
        exit 1
    fi
    sleep 0.5
done

cd "${APISIX_HOME}"
exec prove -v "$@" t/oidc_error_callback_recovery.t
