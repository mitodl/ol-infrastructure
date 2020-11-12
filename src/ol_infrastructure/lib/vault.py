# noqa: WPS226
"""Creation and revocation statements used for Vault role definitions."""

# These strings are passed through the `.format` method so the variables that need to remain in the template
# to be passed to Vault are wrapped in 4 pairs of braces. TMM 2020-09-01
postgres_sql_statements = {
    "approle": {
        "create": "CREATE ROLE {role_name};",
        "revoke": "DROP ROLE {role_name};",
    },
    "admin": {
        "create": """CREATE USER "{{{{name}}}}" WITH PASSWORD '{{{{password}}}}' VALID UNTIL '{{{{expiration}}}}'
          IN ROLE "rds_superuser" INHERIT CREATEROLE CREATEDB;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{{{{name}}}}" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{{{{name}}}}" WITH GRANT OPTION;""",
        "revoke": """GRANT "{{{{name}}}}" TO {role_name} WITH ADMIN OPTION;
          REASSIGN OWNED BY "{{{{name}}}}" TO "{role_name}";
          DROP OWNED BY "{{{{name}}}}";
          REVOKE "{role_name}" FROM "{{{{name}}}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{{{name}}}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{{{name}}}}";
          REVOKE USAGE ON SCHEMA public FROM "{{{{name}}}}";
          DROP USER "{{{{name}}}}";""",
    },
    "app": {
        "create": """CREATE USER "{{{{name}}}}" WITH PASSWORD '{{{{password}}}}' VALID UNTIL '{{{{expiration}}}}'
          IN ROLE "{role_name}" INHERIT;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{{{{name}}}}" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{{{{name}}}}" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{role_name}" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{role_name}" WITH GRANT OPTION;
          SET ROLE "{{{{name}}}}";
          ALTER DEFAULT PRIVILEGES FOR USER "{{{{name}}}}" IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES
          TO "{{{{name}}}}" WITH GRANT OPTION;
          ALTER DEFAULT PRIVILEGES FOR USER "{{{{name}}}}" IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES
          TO "{{{{name}}}}" WITH GRANT OPTION;
          SET ROLE "{role_name}";
          ALTER DEFAULT PRIVILEGES FOR ROLE "{role_name}" IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES
          TO "{role_name}" WITH GRANT OPTION;
          ALTER DEFAULT PRIVILEGES FOR ROLE "{role_name}" IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES
          TO "{role_name}" WITH GRANT OPTION;
          RESET ROLE;""",
        "revoke": """GRANT "{{{{name}}}}" TO {role_name} WITH ADMIN OPTION;
          REASSIGN OWNED BY "{{{{name}}}}" TO "{role_name}";
          DROP OWNED BY "{{{{name}}}}";
          REVOKE "{role_name}" FROM "{{{{name}}}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{{{name}}}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{{{name}}}}";
          REVOKE USAGE ON SCHEMA public FROM "{{{{name}}}}";
          DROP USER "{{{{name}}}}";""",
    },
    "readonly": {
        "create": """CREATE USER "{{{{name}}}}" WITH PASSWORD '{{{{password}}}}' VALID UNTIL '{{{{expiration}}}}';
          GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{{{{name}}}}";
          GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{{{{name}}}}";
          SET ROLE "{{{{name}}}}";
          ALTER DEFAULT PRIVILEGES FOR USER "{{{{name}}}}" IN SCHEMA public GRANT SELECT ON TABLES TO "{{{{name}}}}"
          WITH GRANT OPTION;
          ALTER DEFAULT PRIVILEGES FOR USER "{{{{name}}}}" IN SCHEMA public GRANT SELECT ON SEQUENCES TO "{{{{name}}}}"
          WITH GRANT OPTION;
          RESET ROLE;""",
        "revoke": """GRANT "{{{{name}}}}" TO {role_name} WITH ADMIN OPTION;
          REASSIGN OWNED BY "{{{{name}}}}" TO "{role_name}";
          DROP OWNED BY "{{{{name}}}}";
          REVOKE "{{{{ app }}}}" FROM "{{{{name}}}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{{{name}}}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{{{name}}}}";
          REVOKE USAGE ON SCHEMA public FROM "{{{{name}}}}";
          DROP USER "{{{{name}}}}";""",
    },
}

mysql_sql_statements = {
    "admin": {
        "create": "CREATE USER '{{{{name}}}}'@'%' IDENTIFIED BY '{{{{password}}}}';"
        "GRANT ALL ON `%`.* TO '{{{{name}}}}'@'%';",
        "revoke": "DROP USER '{{{{name}}}}';",
    },
    "app": {
        "create": "CREATE USER '{{{{name}}}}'@'%' IDENTIFIED BY '{{{{password}}}}';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES"  # noqa: Q000
        "CREATE TEMPORARY TABLES, LOCK TABLES ON {{{{ app }}}}.* TO '{{{{name}}}}'@'%';",
        "revoke": "DROP USER '{{{{name}}}}';",
    },
    "readonly": {
        "create": "CREATE USER '{{{{name}}}}'@'%' IDENTIFIED BY '{{{{password}}}}';"
        "GRANT SELECT, SHOW VIEW ON `%`.* TO '{{{{name}}}}'@'%';",
        "revoke": "DROP USER '{{{{name}}}}';",
    },
}
