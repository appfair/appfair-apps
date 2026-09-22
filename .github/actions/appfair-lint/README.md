# appfair-lint

The catalog's rules for an app's repository. A pull request in this repository runs them on the
submitted commit, and an app runs the same rules on every push:

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
| `template` | `appfair/day-appfair@main` | The App Fair app template the licence texts are compared against, `owner/repo@ref`. |
| `only` | (all) | Run only these rules, comma-separated. |
| `skip` | (none) | Run every rule but these, comma-separated. |

## The rules

`python3 appfair_lint.py --list` prints them with one line each:

| rule | what it checks |
|---|---|
| `day-project` | The directory holds a `Day.toml`, so there is a Day project to check. |
| `flavor-manifest` | `Day-appfair.toml` is there, which is where the ids the App Fair publishes under live. |
| `license` | `LICENSE.txt` matches the app template's, character for character. |
| `license-exception` | `LICENSE-EXCEPTIONS.txt` matches the app template's, the same way. |
| `app-ids` | The id each platform builds under is one that platform accepts. Android and HarmonyOS read it as a Java package name, so a hyphen there needs an `[app.android]` / `[app.harmony]` override. |
| `spdx-headers` | Every `.rs` file names its licence in its first five lines. |

A failure names the file, the line, what is wrong and the text that fixes it, and annotates the
file in Actions. Every rule runs, so one push reports every problem.

## The licence texts

The two licence rules read
[LICENSE.txt](https://github.com/appfair/day-appfair/blob/main/LICENSE.txt) and
[LICENSE-EXCEPTIONS.txt](https://github.com/appfair/day-appfair/blob/main/LICENSE-EXCEPTIONS.txt)
from the app template over HTTPS and compare them character for character, so the text an app is
scaffolded with is the text it is held to.

`reference/` holds a copy of each, used when the template cannot be reached, which keeps a
network failure from becoming an app's lint failure. The run says so when it happens, and
`appfair_lint.py --check-template` compares the copies with the template; this repository's
checks run it, daily among other times.

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
and `template` for the app template's own files. Cover the rule in
`scripts/test_appfair_lint.py`, which this repository's checks run.
