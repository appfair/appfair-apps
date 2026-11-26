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
| `only` | (all) | Run only these rules, comma-separated. |
| `skip` | (none) | Run every rule but these, comma-separated. |

## The rules

`python3 appfair_lint.py --list` prints them with one line each:

| rule | what it checks |
|---|---|
| `day-project` | The directory holds a `Day.toml`, so there is a Day project to check. |
| `flavor-manifest` | `Day-appfair.toml` is there, which is where the ids the App Fair publishes under live. |
| `license` | The app declares GPL-2.0-or-later and includes the complete GPL version 2 text. |
| `app-ids` | The id each platform builds under is one that platform accepts. Android and HarmonyOS read it as a Java package name, so a hyphen there needs an `[app.android]` / `[app.harmony]` override. |
| `spdx-headers` | Every `.rs` file has a nonempty `// SPDX-License-Identifier:` notice in its first five lines, naming its own license expression. This does not validate the expression or its terms. |

A failure names the file, the line, what is wrong and the text that fixes it, and annotates the
file in Actions. Every rule runs, so one push reports every problem.

## The licence texts

Publish the app’s source under `GPL-2.0-or-later`. Copy
[`LICENSE.txt`](reference/LICENSE.txt) to the repository root. Lint compares it with the
reference text, ignoring whitespace.
Declare the app license in the app’s `Cargo.toml` package metadata. If the package omits the
license field, lint reads the SPDX notice in `src/lib.rs` or `src/main.rs`.

Every direct and transitive dependency needs public source under a license meeting the
[Debian Free Software Guidelines (DFSG)](https://www.debian.org/social_contract.html#guidelines).
Its terms must permit distribution with the app through the requested channels. Preserve its
own license, exceptions and notices.
Binary dependencies, including Maven JARs, are permitted only if the released binary can provably
be reproduced by building the available source. Review the exact source revisions, build
instructions and environment, and verification evidence for each binary used.

The checks run offline. Text and JSON reports include a reminder for reviewers to verify the
rights granted, dependency licenses, compatibility and binary reproducibility against the
[inclusion criteria](https://appfair.org/docs/inclusion-criteria/#license-and-dependencies).

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

`Project` gives the root, the flavor name, and `sources(".rs")` for a walk that skips build output. Cover the rule in
`scripts/test_appfair_lint.py`, which this repository's checks run.
