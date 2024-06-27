#!/bin/bash -e

#export MONGOHQ_URL=mongodb://"${MONGODB_AUTH}""${MONGODB_HOST}":"${MONGODB_PORT}"/"${MONGODB_DATABASE}"
PROTO="$(echo "${MONGOHQ_URL}" | grep :// | sed -e's,^\(.*://\).*,\1,g')"
URL="${MONGOHQ_URL/${PROTO}/}"
HOSTS=$(echo "${URL}" | perl -ne 'print $1 if /@(.*)\//')
MONGODB_HOST=$(echo "${HOSTS}" | cut -d ":" -f 1)
MONGODB_PORT=${MONGODB_PORT:-27107}

# the search server variable was renamed after the upgrade to elasticsearch 7
export SEARCH_SERVER_ES7="${SEARCH_SERVER}"

# make sure that there is an actual authentication mechanism in place, if necessary
if [ -n "${MONGODB_AUTH}" ]
then
    export MONGOID_AUTH_MECH=":scram"
fi

echo "Waiting for mongodb/elasticsearch..."
dockerize -wait tcp://"${MONGODB_HOST}":"${MONGODB_PORT}" -wait "${SEARCH_SERVER}" -wait-retry-interval 5s -timeout 600s

exec "$@"
