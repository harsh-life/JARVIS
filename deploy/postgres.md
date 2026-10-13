# PostgreSQL for the JARVIS pilot (F3, H-1)

The pilot's runtime store is PostgreSQL on the same host as the server
(`docs/DECISION_REGISTER.md` §2I, H-1). SQLite stays for development and
tests: it has one writer, so PRD #32 (fairness under ~10-device load) is not
met on it.

## 1. Install and create the role and database

```bash
sudo apt-get install -y postgresql            # Debian/Ubuntu; any supported major works (CI uses 16)
sudo systemctl enable --now postgresql

sudo -u postgres createuser --no-superuser --no-createdb --no-createrole jarvis
sudo -u postgres createdb --owner=jarvis jarvis
sudo -u postgres psql -d jarvis -c 'REVOKE ALL ON DATABASE jarvis FROM PUBLIC'
sudo -u postgres psql -d jarvis -c 'REVOKE CREATE ON SCHEMA public FROM PUBLIC'
```

The `jarvis` role owns exactly one database and has no other privilege: it
cannot create roles or databases, and no other role can connect to its
database unless you grant that. Run these before `deploy/bootstrap.sh`, which
migrates the database but never creates it. (The OS user `jarvis` is created
by `deploy/bootstrap.sh`; `createuser` creates only the *database* role of the
same name, so either order works.)

## 2. Authentication: peer, over the Unix socket — no password

The pilot configuration connects over the local socket:

```yaml
database_url: "postgresql+asyncpg://jarvis@/jarvis?host=/run/postgresql"
```

Debian's default `pg_hba.conf` already has `local all all peer`: the server
process runs as the OS user `jarvis`, PostgreSQL asks the kernel which user is
on the other end of the socket, and admits it as the role `jarvis`. **No
database password exists**, so there is none to leak, rotate or back up.

Check it the way the service will connect:

```bash
sudo -u jarvis psql -h /run/postgresql -d jarvis -c 'select current_user'
```

## 3. A database on another host (not the pilot default)

If PostgreSQL is not on this host, use TCP with TLS and a password that never
sits in configuration (config refuses one — SECRET-004):

```yaml
database_url: "postgresql+asyncpg://jarvis@db.internal:5432/jarvis?ssl=require"
```

and put the password in `/var/lib/jarvis/.pgpass` (the `jarvis` user's home),
owned by `jarvis`, mode `0600`:

```
db.internal:5432:jarvis:jarvis:<password>
```

`PGPASSWORD` in the environment also works, but a file the service alone can
read is preferable to an environment variable.

## 4. Migrations

Migration is a bootstrap step, never a startup side effect (15 §3).
`deploy/bootstrap.sh` runs `alembic upgrade head` as `jarvis`; after an
upgrade of the code, run it again (or `alembic upgrade head` by hand as
`jarvis` with `HYPERMIND_CONFIG_PATH=/etc/jarvis/config.yaml`) **before**
restarting the service. Until the store is at the code's head,
`GET /api/v1/ready` answers `503` with `"migrations": false`.

CI's `postgres` job runs every suite and the migration round-trip on
PostgreSQL 16 on each push.
