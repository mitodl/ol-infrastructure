# PKI Overview

The meat and potatoes of the PKI code actually resides in `src/ol_infrastructure/components/services/vault.py`.

```
AWS Private CA --(sign)--> pki-intermediate-ca (ci/qa/production) --(sign)--> pki-intermediate-ca-mitx
                                                                              pki-intermediate-ca-ocw
                                                                              ...
```

Avoid using `pki-intermediate-ca` for issuing end-entity certificates. Use `pki-intermediate-ca-{env}`.
