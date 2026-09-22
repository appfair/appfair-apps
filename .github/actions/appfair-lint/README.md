# appfair-lint

The checks an App Fair app's repository has to pass. The catalog runs them on every submission,
and an app runs the same rules on every push:

```yaml
jobs:
  appfair-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: appfair/appfair-apps/.github/actions/appfair-lint@main
```

## Inputs

| input | default | what it is |
|---|---|---|
| `path` | `.` | The project directory, the one holding `Day.toml`. |
| `flavor` | `appfair` | The build flavor whose manifest carries the identity the catalog publishes the app under. |
| `only` | — | Run only these rules, comma-separated. |
| `skip` | — | Run every rule but these, comma-separated. |

## The rules

`python3 appfair_lint.py --list` prints them with one line each:

| rule | what it checks |
|---|---|
| `day-project` | The directory holds a `Day.toml`, so there is a Day project to check. |
| `flavor-manifest` | `Day-appfair.toml` is there, which is where the ids the App Fair publishes under live. |
| `license` | `LICENSE.txt` is the GNU AGPL 3.0 text, compared line by line against the copy in `reference/`. |
| `license-exception` | `LICENSE-EXCEPTIONS.txt` is the App Fair Distribution Exception, the same way. |
| `spdx-headers` | Every `.rs` file names its licence in its first five lines. |

A failure names the file, the line, what is wrong and the text that fixes it, and in Actions it
also annotates the file. Every rule runs, so one push reports every problem rather than the first.

## Adding a rule

One function in `appfair_lint.py`:

```python
@rule("store-listing", "The store listing carries every field the App Store takes")
def store_listing(project: Project) -> Iterable[Finding]:
    if not project.path("store", "en", "description.txt").is_file():
        yield Finding(
            "store-listing",
            "store/en/description.txt is missing, and the App Store listing is built from it",
            "write the description the store shows, up to 4000 characters",
            path="store/en/description.txt",
        )
```

`Project` gives the root, the flavor name, `sources(".rs")` for a walk that skips build output,
and `reference/` for material a rule compares against. Cover it in
`scripts/test_appfair_lint.py`, which `python3 -m unittest discover -s scripts` runs in this
repository's own checks.
