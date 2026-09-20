#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_CODENAME" != jammy ]]; then
  echo 'This deployment script targets the inspected Ubuntu 22.04 instance.' >&2
  exit 1
fi

install -d /usr/share/postgresql-common/pgdg
curl --fail --show-error --location --retry 3 --connect-timeout 20 \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
cat > /etc/apt/sources.list.d/aidb-pgdg.sources <<'SOURCES'
Types: deb
URIs: https://apt.postgresql.org/pub/repos/apt
Suites: jammy-pgdg
Architectures: amd64
Components: main
Signed-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
SOURCES
apt-get update
apt-get -o DPkg::Lock::Timeout=45 install -y postgresql-16 postgresql-client-16 postgresql-16-pgvector
pg_lsclusters
