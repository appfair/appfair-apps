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
| `license` | A nonempty, readable license text is present in `LICENSE`, `COPYING`, a suffixed variant, or `LICENSES/*.txt`. DFSG compliance requires review. |
| `app-ids` | The id each platform builds under is one that platform accepts. Android and HarmonyOS read it as a Java package name, so a hyphen there needs an `[app.android]` / `[app.harmony]` override. |
| `spdx-headers` | Every `.rs` file has a nonempty `// SPDX-License-Identifier:` notice in its first five lines, naming its own license expression. This does not validate the expression or its terms. |

A failure names the file, the line, what is wrong and the text that fixes it, and annotates the
file in Actions. Every rule runs, so one push reports every problem.

## The licence texts

Apps may use any license meeting the [Debian Free Software Guidelines (DFSG)](https://www.debian.org/social_contract.html#guidelines).
The app and **all dependent software, including transitive dependencies**, must have publicly
available source under DFSG-compliant licenses, with terms compatible with distribution together.
Binary dependencies, including Maven JARs, are permitted only if the released binary can provably
be reproduced by building the available source. Review the exact source revisions, build
instructions and environment, and verification evidence for each binary used.

The `license` rule checks for license documentation and emits a review reminder in both text and
JSON reports. It does not recognize license texts, certify DFSG compliance, resolve dependency
trees, or prove binary reproducibility. Those are required review steps under the
[inclusion criteria](https://appfair.org/docs/inclusion-criteria/#license-and-dependencies).
Rebuilding an app using the same prebuilt dependency does not verify that dependency's source.

License and notice checks run locally without fetching a reference license or requiring a
particular starter project. Retain all applicable license texts, exceptions and notices;
reviewers assess their terms under the same policy.

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
