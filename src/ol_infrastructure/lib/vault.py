# noqa: WPS226

postgres_sql_statements = {
    'admin': {
        'create': """CREATE USER "{{name}}" WITH PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'
          IN ROLE "rds_superuser" INHERIT CREATEROLE CREATEDB;
          GRANT "{role_name}" TO "{{name}}" WITH ADMIN OPTION;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{{name}}" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{{name}}" WITH GRANT OPTION;""",
        'revoke': """GRANT "{{name}}" TO odldevops WITH ADMIN OPTION;
          REASSIGN OWNED BY "{{name}}" TO "{role_name}";
          DROP OWNED BY "{{name}}";
          REVOKE "{role_name}" FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";
          REVOKE USAGE ON SCHEMA public FROM "{{name}}";
          DROP USER "{{name}}";"""},
    'app': {
        'create': """CREATE USER "{{name}}" WITH PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'
          IN ROLE "{role_name}" INHERIT;
          GRANT "{{name}}" TO odldevops WITH ADMIN OPTION;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{{name}}" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{{name}}" WITH GRANT OPTION;
          ALTER DEFAULT PRIVILEGES FOR USER "{{name}}" IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES
          TO "{role_name}" WITH GRANT OPTION;
          ALTER DEFAULT PRIVILEGES FOR USER "{{name}}" IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES
          TO "{role_name}" WITH GRANT OPTION;""",
        'revoke': """GRANT "{{name}}" TO odldevops WITH ADMIN OPTION;
          REASSIGN OWNED BY "{{name}}" TO "{role_name}";
          DROP OWNED BY "{{name}}";
          REVOKE "{role_name}" FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";
          REVOKE USAGE ON SCHEMA public FROM "{{name}}";
          DROP USER "{{name}}";"""},
    'readonly': {
        'create': """CREATE USER "{{name}}" WITH PASSWORD '{{password}}' VALID UNTIL '{{expiration}}';
          GRANT "{{name}}" TO odldevops;
          GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{{name}}";
          GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{{name}}";
          ALTER DEFAULT PRIVILEGES FOR USER "{{name}}" IN SCHEMA public GRANT SELECT ON TABLES TO "{role_name}"
          WITH GRANT OPTION;
          ALTER DEFAULT PRIVILEGES FOR USER "{{name}}" IN SCHEMA public GRANT SELECT ON SEQUENCES TO "{role_name}"
          WITH GRANT OPTION;""",
        'revoke': """GRANT "{{name}}" TO odldevops WITH ADMIN OPTION;
          REASSIGN OWNED BY "{{name}}" TO "{role_name}";
          DROP OWNED BY "{{name}}";
          REVOKE "{{ app }}" FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";
          REVOKE USAGE ON SCHEMA public FROM "{{name}}";
          DROP USER "{{name}}";"""}
}

mysql_sql_statements = {
    'admin': {
        'create': "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
                  "GRANT ALL ON `%`.* TO '{{name}}'@'%';",
        'revoke': "DROP USER '{{name}}';"
    },
    'app': {
        'create': "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
                  "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES"  # noqa: Q000
                  "CREATE TEMPORARY TABLES, LOCK TABLES ON {{ app }}.* TO '{{name}}'@'%';",
        'revoke': "DROP USER '{{name}}';"
    },
    'readonly': {
        'create': "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
                  "GRANT SELECT, SHOW VIEW ON `%`.* TO '{{name}}'@'%';",
        'revoke': "DROP USER '{{name}}';"
    }
}
