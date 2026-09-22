# appfair-apps

The App Fair publishes apps to the App Store and Google Play from this repository. Each app has a
metadata file under `apps/`. A pull request that adds or changes one builds the app and checks the
build; merging it signs the build with the App Fair's keys and uploads it. The app's source stays
in its own repository.

```yaml
# apps/Faire-Games.yaml
token: Faire-Games
title: Fair Games
tag: v2.0.1
commit: 549bac3f78ce937146b56a7acecc35cb361175e3
distribution:
  ios-uikit:
    - apple-app-store
  android-mdc:
    - google-play-store
```

An App Fair app lives at `https://github.com/<token>/<token>`, so the token identifies both the
file and the repository. The pipeline checks out the commit, not the tag, so moving a tag after
review has no effect on what is built or published. `scripts/queue.py resolve --token Faire-Games
--tag v2.0.1` prints the `tag` and `commit` lines.

`distribution` lists the channels for each target. `policy.yaml` declares the channels the queue
can publish to today (`apple-app-store`, `google-play-store`) and the ones it will grow into
(`altstore`, `f-droid`, `samsung-galaxy-store`).

Everything else about the app — its name, icon, version, permissions, store listing and
screenshots — comes from the app's repository at that commit.

## The three stages

Building a submitted app runs code its author wrote: `build.rs`, Gradle and Xcode build phases,
and whatever its dependencies do at build time. Checking the result and signing it require none of
that, so the pipeline splits into three jobs along that boundary.

| stage | what it does | runs app code | holds secrets |
|---|---|---|---|
| **A — build** | checks the app out at the commit, lints it, packs it **unsigned** for each target | yes | no |
| **B — validate** | reads the packages: inventory and digests, permissions against the manifest, provenance against the commit, comparison against the app's own release, virus scan | no | no |
| **C — sign and submit** | signs the package with the App Fair's keys and uploads it to each channel | no | yes |

Stage A hands its packages to stage B, which opens them as archives. Stage C re-signs an archive
and uploads it, with the key material named on the command line, so the submitted app's manifest
cannot influence which credentials are read.

Stages B and C read the app's manifest, store copy and translations through a sparse checkout.
`policy.yaml` lists those paths. Source, build scripts and project files are left behind.

## What each stage checks

**Stage A** runs `day lint` before packing, so a green submission also means the project is a
well-formed Day project: ids resolve, strings are in the catalogue, and the store listing is
complete.

**Stage B** checks five things about the package:

- **Contents.** Every file, with its size and SHA-256, recorded in the run and kept as a report.
- **Identity.** The bundle id, version and build number from the package's manifest, against the
  app's `Day.toml` and, when it carries one, its App Fair flavor.
- **Permissions.** Every permission in the package, against the app's declared permissions mapped
  through day's catalogue, plus the baseline the framework adds.
- **Comparison with the app's own release.** The App Fair builds the app itself, through its
  flavor when it carries one; the app's CI publishes the base app. The comparison normalizes each side's declared package id, version and
  build, display name, URL scheme, bundle and executable names, and package-qualified authorities
  and permissions. Day's launcher icons, signing records and Android debug-symbol sidecars are
  excluded. Resource tables and iOS asset catalogs are decoded, so other resources, permissions,
  components and DEX must match. Day compiles the display name into the app, so the app's own
  binary cannot match the base release; `policy.yaml: expected-differences` names those paths, and
  a difference in one of them is reported and allowed. `compare.json` lists every normalized,
  excluded and expected path. Any other difference blocks publication until a maintainer waives
  it, and the run says what it is: the two builds' tool versions side by side (day, rustc, Xcode,
  NDK), each differing path, and for a property list the keys that disagree with both values.
  Both builds run on the runner image `policy.yaml: runners` names, since a different Xcode
  produces a different `Info.plist` and binary from the same source. Missing or ambiguous base assets,
  unreadable metadata and an identity that disagrees with the manifest always fail, and the App
  Fair's own package is never substituted for the base release. An app with no flavor is built
  exactly as its maintainer released it, so the two sides have nothing to differ about.
- **Safety.** ClamAV over every file, and the provenance and SBOM beside the package checked
  against the commit the submission pins. A build from a dirty checkout is refused.

**Stage C** requests the app's App Store profile from Apple, signs with `day sign apply`, which
re-signs a package without rebuilding it, and uploads through the fastlane lanes `day store stage`
generates from the app's listing. Submitting is what publishing means here, so the
lanes ask Apple for review and Google for a production rollout without a setting to say so;
`submit: false` under a channel is the exception that uploads and stops.

Before signing, each channel asks its store what it already holds: Play refuses a version code
twice, and the App Store refuses a version whose record carries a language without What's New or
screenshots. Both stop the run there, with the remedy named.

Each channel is then held to its own record. App Store Connect has to show the build uploaded,
attached to the version, and the version waiting for review; Google Play has to show the version
code in the production track as a completed or in-progress release. A lane that finished happily
while the store shows otherwise fails the job.

## The reviewer's comment

A submission that updates an app moves two lines: its `tag` and its `commit`. The checks turn that
into the range between the commit already published and the one proposed, and post it on the pull
request:

> ### Fair Games — `Faire-Games`
>
> `v2.0.0` → `v2.0.1`
>
> - [The source changes between the two commits](#) — 4 commit(s), 21 file(s), +62 −78
> - [Release notes for v2.0.1](#)
>
> Build and packaging files in that range:
>
> - `Cargo.toml`
> - `build.rs`
> - `.github/workflows/ci.yml`

`policy.yaml: review.highlight-paths` decides which paths are listed: what runs during a build,
what pulls in a dependency, and what sets the app's identity or permissions. A first submission
gets a link to the source at the commit instead, since there is nothing to compare against. When
the proposed commit is not a continuation of the published one, the comment says so, with how far
the two have diverged.

The comment is rewritten on each push, so the thread holds the current range rather than a
history of every force-push. `scripts/queue.py review --app <token> --offline` prints the same
text locally.

Posting it is a separate workflow (`comment.yml`). A pull request opened from a fork gets a
read-only token, so the run that writes the summary cannot post it; `comment.yml` runs afterwards
from the default branch, downloads the summary and comments. It checks out nothing from the
submission.

## What each side supplies

**The maintainer** supplies a public Day project with a release tag, the canonical bundle id and a
build number above what the stores already have, a store listing, and an app that meets the
[inclusion criteria](https://appfair.org/docs/inclusion-criteria/). The base release used for the
comparison comes from the app's own CI, so its existing release workflow is enough and the App
Fair's own packages never have to be published. The queue derives the base artifact name from the
unflavored manifest at the pinned commit, including Day's unsigned-IPA suffix. The tag names the
version being published, so `v2.0.2` publishes 2.0.2 on both stores; a flavor that states a
`version` of its own is rejected unless the tag agrees with it.

**The App Fair** supplies the developer accounts, the signing material, the review, and this
repository. Its secrets live in the publish workflow's `store` environment and are reachable only
from the signing job, so a pull request opened from a fork cannot get at them.

An app is published under the id its build resolves to, which the queue reads from the app's own
manifest — through its `Day-appfair.toml` when it carries one — rather than deriving: the store is
asked about that id before signing, and the upload goes to its record. Every such id starts with
`org.appfair.app.` ([background](https://appfair.org/docs/building/#bundle-id)); what follows is
the app's own business. The convention for a new app is `org.appfair.app.<token>` on iOS and the
same with hyphens as underscores on Google Play, and an app that came to the catalog with records
under another spelling keeps them. An app whose own builds go out
under the maintainer's id carries the App Fair's in a
[Day build flavor](https://daybrite.dev/docs/flavors), a `Day-appfair.toml` beside its `Day.toml`;
one whose `Day.toml` already states the App Fair id needs no flavor, and the queue then builds the
project as it stands. The flavor is named after this repository, so a fork called
`gamesfair-apps` builds each app's `gamesfair` flavor without any edit.

A submission may state `id:` (and `android-id:` for Play) to pin the records it publishes to.
Nothing requires it: the pin exists so that an app which changes the id it builds under is caught
by the checks instead of at the store. Two submissions may not publish under one id.

`Day-appfair.toml` is how an app keeps its own identity separate from the one it publishes under
here: the queue builds, lints, validates and signs every stage through that flavor when the file
is there, so those are the ids the checks read and the stores receive.

## Submitting

[CONTRIBUTING.md](CONTRIBUTING.md) documents every key in the file and every rule applied to it.
The short version:

1. If the app is new to the catalog, propose it in a
   [discussion](https://github.com/orgs/appfair/discussions) first.
2. Tag a release of the app. The tag names the version published, and the build number has to
   climb past what the stores already have.
3. Write the metadata file and open a pull request:

   ```sh
   scripts/queue.py add Faire-Games      # new app, from its latest release
   scripts/queue.py update Faire-Games   # existing app, moved to its latest release
   ```

   Each writes `apps/<token>.yaml` and validates it. Review the result before opening the pull
   request: the title comes from the app's store listing and the channel list covers everything
   the catalog publishes to, so both may need editing.
4. Watch the checks. The same jobs run on merge, so a green pull request will publish.

## Running the checks locally

The checks are in one script and need Python and PyYAML.

```sh
python3 scripts/queue.py validate --all         # every submission against policy.yaml
python3 scripts/queue.py selftest               # the rules against their own cases
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/queue.py wiring                 # every workflow against the actions it calls
python3 scripts/queue.py plan --app Faire-Games # what the workflows would build
python3 scripts/queue.py review --app Faire-Games --offline   # the reviewer's comment
python3 scripts/queue.py resolve --token Faire-Games --tag v2.0.1
python3 scripts/queue.py add Faire-Games
python3 scripts/queue.py update Faire-Games
```

The validation steps also run against a package you already have:

```sh
python3 scripts/queue.py inspect --package fair-games-android-mdc.aab \
  --sibling fair-games-android-mdc.apk --out report.json
python3 scripts/queue.py compare --ours fair-games-android-mdc.aab \
  --theirs release/day-games-android-mdc.aab \
  --metadata metadata.json --reference-metadata base-metadata.json --target android-mdc
python3 scripts/queue.py audit --app Faire-Games --report report.json \
  --metadata metadata.json --target android-mdc --sidecars . --expect-commit <sha>
```

`metadata.json` is `day --flavor appfair metadata --json` for an app that carries
`Day-appfair.toml` and plain `day metadata --json` for one that does not; `base-metadata.json` is
always the unflavored read. Both come from a checkout of the app's manifest paths.

## Repository secrets

The publish workflow reads these from the `store` environment. A missing secret stops the upload
that needs it, and the run reports which one was missing.

| secret | what it is |
|---|---|
| `DAY_APPLE_CERT_P12`, `DAY_APPLE_CERT_PASSWORD` | the iOS distribution certificate stage C signs with |
| `DAY_APPLE_IDENTITY` | the name on that certificate, such as `iPhone Distribution: The App Fair Project Inc (25KG25YA3R)`. `codesign` matches it as a prefix, and Apple has issued both `iPhone Distribution` and `Apple Distribution` certificates |
| `DAY_APPLE_TEAM` | the Apple Developer team id used for the provisioning profile |
| `DAY_ASC_KEY_ID`, `DAY_ASC_ISSUER`, `DAY_ASC_KEY_B64` | the App Store Connect API key for uploads and listings |
| `DAY_ANDROID_KEYSTORE_B64`, `DAY_ANDROID_KEY_ALIAS`, `DAY_KS_PASS`, `DAY_KEY_PASS` | the Play upload keystore |
| `DAY_PLAY_JSON_KEY` | the Google Play service-account key for `supply` |

Apple issues a provisioning profile per bundle id. This repository requests one during the run:
`fastlane sigh` asks for a profile for the app being published, against the certificate that will
sign it. There is then no profile secret per app to store or renew, and no wildcard profile over
the namespace. An app that requires a specific profile names the secret holding it in its
`apple-app-store: profile-secret` setting.

The Play upload key must be the key registered for the existing listing. Play rejects an upload
signed with a different key.

Two of the organization's secrets carry several fields at once, and the queue accepts them:
`KEYSTORE_PROPERTIES` (base64 Java properties) holds the keystore alias and both passwords, and
`APPLE_APPSTORE_APIKEY` (base64 fastlane key file) holds the App Store Connect id, issuer and key.
An explicit `DAY_*` secret takes precedence over the bundle that duplicates it, so the newer names
can be adopted one at a time. Decoding happens only in the signing job: files are written under
`RUNNER_TEMP` readable by that job alone, and values parsed out of a secret are masked before use.

## Setting the repository up

Four settings are not in any file:

- **Actions policy.** The stages call composite actions from `daybrite/actions` for the toolchain
  and the day CLI. Under *Settings → Actions → General*, "Allow actions and reusable workflows"
  must admit `daybrite/*`.
- **A `store` environment.** The signing job names it, which keeps the store secrets off the
  repository and allows reviewers or a wait timer in front of them.
- **Branch protection on `main`.** A merge publishes, so `main` should accept pull requests only,
  with review from the owners in `.github/CODEOWNERS`. The `record` job pushes
  `state/published.json` to `main`; either allow the Actions bot to bypass the rule for that path,
  or drop the job and read the state from the run.
- **An `allow-mismatch` label**, named in `policy.yaml`. A maintainer applies it to a pull request
  whose comparison cannot pass for a known reason.
- **Write access for workflows.** Under *Settings → Actions → General*, "Workflow permissions"
  must allow the `GITHUB_TOKEN` to write, so `comment.yml` can post the reviewer's summary.
  GitHub runs a `workflow_run` workflow from the default branch, so the comment appears once
  `comment.yml` is on `main`.

`workflow_dispatch` on the publish workflow republishes any app by token after a store-side
failure, without a new commit.

## Layout

```text
apps/<token>.yaml                one metadata file per app
policy.yaml                      channels, submission rules, and stage B's settings
schema/app.schema.json           the same shape, for editors
scripts/queue.py                 add, update, validate, plan, review, verify, inspect, compare,
                                 audit, authorize, wiring, record, resolve, selftest
scripts/signing_inputs.py        credential normalization for the signing stage
scripts/package_compare.py       the package comparison stage B runs
state/published.json             what has been published, written by the publish workflow
.github/actions/build-app        stage A
.github/actions/validate-package stage B
.github/actions/sign-submit      stage C
.github/workflows/               checks, pr (A + B), publish (A + B + C + record),
                                 comment (the reviewer's summary)
```
