#!/usr/bin/env bash
#
# Seed the root user's personal access token with the fixed value in
# secrets.yaml, so apps can be configured against this GitLab before anyone
# has logged into it.
#
# Idempotent: re-running updates the existing token rather than adding
# another. Safe to trigger from the Tilt UI at any time.
set -euo pipefail

NAMESPACE=gitlab
TOKEN=$(kubectl -n "${NAMESPACE}" get secret gitlab-secrets \
    -o jsonpath='{.data.api_token}' | base64 -d)

if [[ -z "${TOKEN}" ]]; then
    echo "gitlab-secrets has no api_token" >&2
    exit 1
fi

echo "Seeding the root personal access token..."

# validate: false because GitLab rejects a token whose plaintext we set
# ourselves; the digest it stores is what authenticates, and the format check
# is the only thing in the way.
kubectl -n "${NAMESPACE}" exec statefulset/gitlab -- gitlab-rails runner "
  user = User.find_by_username('root')
  abort('root user not found; is GitLab finished migrating?') if user.nil?

  token = user.personal_access_tokens.find_by(name: 'local-dev')
  token ||= user.personal_access_tokens.new(name: 'local-dev')
  token.scopes = ['api', 'write_repository']
  token.expires_at = 365.days.from_now
  token.set_token('${TOKEN}')
  token.save!(validate: false)

  puts \"seeded token id=#{token.id} scopes=#{token.scopes.join(',')} expires=#{token.expires_at}\"
"
