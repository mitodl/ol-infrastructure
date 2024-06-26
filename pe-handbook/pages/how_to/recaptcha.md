# reCAPTCHA Errors

```
ERROR for site owner: Invalid domain for site key.
```

1. Need to login to google with the mitx devops username and password (vault-production -> `platform-secrets/google`). It will need to do an email verification code to one of our lists if you don't have an active session.
2. Make your way to the reCAPTCHA console located [here](https://www.google.com/u/1/recaptcha/admin).
3. There is a dropdown on the top left that lets you see which site/application configuration that you're working with.
4. Once you're on the site that you care about, there is a gear icon on the top right. Click that for the settings.
5. Three things to verify:
    1. Does the site key match what is listed in app configuration + vault?
    2. Does the secret key match what is listed in app configuration + vault?
    3. Is the list of domains correct?
    4. NOTE: When checking keys, look at the end of the string rather than the start. They all seem to start the same.
