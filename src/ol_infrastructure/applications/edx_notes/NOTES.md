## Database migrations for edx_notes

If you deploy a new edx_notes environment it needs to have the database migrations performed against the edxapp Mariadb instance.

```
docker compose run  -it  notes_api python manage.py migrate --settings="notesserver.settings.yaml_config"
```

## Certificates with ACM
If you're using a certificate that is imported into ACM, for instance `*.xpro.mit.edu`, that certificate needs to be importered with the complete chain, not just the cert + kay pair. If edxapp gets a response back from the notes server that doesn't include the entire chain, it will fail to verify the response and cause the app to throw a 500 and notes will only partially work. Users will be able to create notes and view them overlaid on content but they won't be able to see the complete listing of notes under the notes tab in edxapp.
