#!/bin/sh
# Forward-only migration runner. The compose stack starts this as a one-shot
# service before any application service, which is also how staging applies
# migrations. shortcut: one migrator at a time; concurrent runners are not
# coordinated beyond the schema_migrations checks.
set -eu

: "${DATABASE_URL:?DATABASE_URL is required}"

attempt=0
until pg_isready -d "$DATABASE_URL" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "migrate: database did not become ready" >&2
    exit 1
  fi
  sleep 1
done

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q <<'SQL'
create table if not exists public.schema_migrations (
  filename   text primary key,
  sha256     text not null,
  applied_at timestamptz not null default now()
);
SQL

applied=0
for file in /migrations/*.sql; do
  [ -e "$file" ] || continue
  name=$(basename "$file")
  checksum=$(sha256sum "$file" | awk '{print $1}')

  existing=$(psql "$DATABASE_URL" -Atq -c \
    "select sha256 from public.schema_migrations where filename = '$name'")
  if [ -n "$existing" ]; then
    if [ "$existing" != "$checksum" ]; then
      echo "migrate: $name was modified after it was applied" >&2
      exit 1
    fi
    continue
  fi

  echo "migrate: applying $name"
  {
    cat "$file"
    printf "\ninsert into public.schema_migrations (filename, sha256) values ('%s', '%s');\n" \
      "$name" "$checksum"
  } | psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -q
  applied=$((applied + 1))
done

total=$(psql "$DATABASE_URL" -Atq -c "select count(*) from public.schema_migrations")
echo "migrate: $applied applied, $total recorded"
