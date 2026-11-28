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
| `appfair-module` | The app takes `day-piece-appfair` from crates.io, commits a `Cargo.lock` that locks a crates.io release the catalog accepts, and passes its provider to the settings builder. See [The App Fair module](#the-app-fair-module). |

A failure names the file, the line, what is wrong and the text that fixes it, and annotates the
file in Actions. Every rule runs, so one push reports every problem.

## The App Fair module

Every App Fair app shows an App Fair group in its settings. The group comes from the
`day-piece-appfair` crate. An app adopts it with one dependency in `Cargo.toml`:

```toml
[dependencies]
day-piece-appfair = "0.*"
```

and one line where it declares its settings, `.provider(&day_piece_appfair::APP_FAIR)` on the
`Preferences` builder of `day-piece-settings`:

```rust
Preferences::new()
    .about(about)
    .group(general)
    .provider(&day_piece_appfair::APP_FAIR)
    .install();
```

An app's CI can run this rule alone with the `only` input, which suits an app that does not pass
the other rules yet:

```yaml
- uses: actions/checkout@v7
- uses: appfair/appfair-apps/.github/actions/appfair-lint@main
  with:
    only: appfair-module
```

A pull request in this repository runs every rule on the submitted commit, whatever the app's CI
selects.

The `appfair-module` rule reports each of these:

- `Cargo.toml` has no `day-piece-appfair` under `[dependencies]`, or the entry is not a crates.io
  version requirement. A string passes, and so does a table with `version`. A `path` or `git`
  source is reported, because a tagged release builds from crates.io. A dependency renamed with
  `package = "day-piece-appfair"` counts, and the call then names the crate as the app knows it.
- The app commits no `Cargo.lock`, or the lock has no `day-piece-appfair` entry.
- The lock's `day-piece-appfair` entry does not come from crates.io. The entry's `source` has to
  be `registry+https://github.com/rust-lang/crates.io-index`, with the `checksum` cargo wrote
  beside it. An entry without a `source` is a local checkout, which a `.cargo/config.toml` patch
  table (`day patch --local`) or a path dependency leaves in the lock. A git source and another
  registry are reported the same way.
- The lock's entry is a release older than the minimum.
- No file under `src/` contains `.provider(&day_piece_appfair::APP_FAIR)`. The rule reads past
  whitespace and line breaks, skips comments, and accepts `.provider(&APP_FAIR)` in a file that
  has `use day_piece_appfair::APP_FAIR;` or names the constant in a `use` brace list.
- A Rust source calls `.without_provider("appfair")`, which takes the group out again.
- A Rust source declares a settings group of its own under the module's id, with
  `Group::new("appfair", …)` or the crate's `GROUP_ID`. The id and its `settings/appfair` route
  belong to the App Fair group. `day-piece-settings` keeps the provider's group when two groups
  share an id, so the app's group would not show.

A lock written during local development against a patched checkout fails this rule. Before a
commit that the catalog will read, write the lock with the patch table out of the way, and check
it. `cargo update --workspace` locks the crates that came from a checkout at their crates.io
releases and leaves every other package where it is:

```sh
mv .cargo/config.toml .cargo/config.toml.local
cargo update --workspace
cargo metadata --locked --format-version 1 > /dev/null
```

The minimum release is `APPFAIR_MODULE_MINIMUM` in `appfair_lint.py`, and raising it is an edit to
that line. The crate leaves the bytes `day-piece-appfair/<version>` and a NUL in the compiled app,
and the catalog's validation stage holds that marker in every built package to the same minimum.

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
