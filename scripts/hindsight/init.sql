-- What the deployed database gives Hindsight: pgvector created by a superuser,
-- and a login that is a member of the app role the Vault "app" statements build
-- (src/ol_infrastructure/lib/vault.py, plus the two additions in the Hindsight
-- stack). The harness connects as that login, so a migration that needs more
-- privilege fails here the way it would on RDS.
CREATE EXTENSION vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE hindsight;
GRANT CREATE ON DATABASE hindsight TO hindsight;
GRANT CREATE ON SCHEMA public TO hindsight WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO hindsight WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO hindsight WITH GRANT OPTION;
SET ROLE hindsight;
ALTER DEFAULT PRIVILEGES FOR ROLE hindsight IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO hindsight WITH GRANT OPTION;
ALTER DEFAULT PRIVILEGES FOR ROLE hindsight IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO hindsight WITH GRANT OPTION;
RESET ROLE;

CREATE USER "v-harness-app" WITH PASSWORD 'harness-only-password' IN ROLE hindsight INHERIT;
ALTER ROLE "v-harness-app" SET ROLE hindsight;
