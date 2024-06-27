## Database migrations for edx_notes

If you deploy a new edx_notes environment it needs to have the database migrations performed against the edxapp Mariadb instance.

```
docker compose run  -it  notes_api python manage.py migrate --settings="notesserver.settings.yaml_config"
```
