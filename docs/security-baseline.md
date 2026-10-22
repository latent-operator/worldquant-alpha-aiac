# Public Git history baseline

The upstream repository tracked `keys.txt` in commit `c432aa5`. Its reviewed
contents are a `Dataset Keys` list of 17 identifier-shaped dataset names, with
no API token or password. The exact reviewed Git blob is
`9a3ce503dd74e397ed75f652e132ed97347a0b25`. The release gate checks that
blob identity so a different historical file cannot silently inherit this
assessment. The filename alone is not evidence that credentials were exposed.

The current repository hygiene check examines all tracked files and all
nonignored new files. It rejects new credential-shaped content. This historical
file assessment does not cover every other commit or account configuration;
review any distinct suspected exposure on its own evidence.
