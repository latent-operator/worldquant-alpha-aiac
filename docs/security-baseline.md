# Public Git history baseline

The upstream repository previously tracked `keys.txt`. The relevant Git history includes commit `c432aa5`, and local baseline commit `1d65640` removed the file from the current index. Deleting it from HEAD did not remove it from public history.

The current repository hygiene check examines all tracked files and all nonignored new files. It does not silently allow any new credential-shaped content. Historical public commits remain outside that current-tree check; this is an exact legacy exception for the already published `keys.txt` history, not a permission to add more secrets.

Before a public fork, push, or local release, the key owner must verify each historical credential is revoked or rotated at its provider. Record only `{"historical_keys_revoked": true, "reviewed_at": "YYYY-MM-DD"}` in ignored `ops/secret-attestation.json`. Do not put keys, hashes of keys, or account identifiers in that file or in a PR. If status is unknown, keep the fork and release gates closed.
