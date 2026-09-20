# appfair-apps

The App Fair's submission queue: one file per app, one pull request per release.

A maintainer who wants their app published, or an existing app updated, changes a file in `apps/`.
Opening the pull request builds their app, inspects what was built, and compares it with the
release they published from the same tag. Merging it signs that build with the App Fair's keys and
hands it to the App Store and Google Play. The app stays in the maintainer's repository, and this
repository is the queue in front of the stores.

```yaml
# apps/Faire-Games.yaml
token: Faire-Games
title: Fair Games
repo: https://github.com/Faire-Games/Faire-Games
tag: v1.9.0
flavor: appfair
distribution:
  ios-uikit:
    - apple-app-store
  android-mdc:
    - google-play-store
```

`distribution` is where the app goes, under the target that builds for it: one build per target,
one submission per channel, and room for the channels the catalog is headed for (AltStore,
F-Droid, the Samsung store) as each one gains an arm. `policy.yaml` declares them.

That is the whole submission. Everything else the build needs — the app's name, icon, version,
permissions, store listing and screenshots — is read out of the app's own repository at that tag,
where its maintainer already keeps it.

## The three stages

Building a submitted app means running code its author wrote: `build.rs`, Gradle and Xcode build
phases, whatever its dependencies do while compiling. Checking that build, and signing it, must
not. So the pipeline is three stages with a line drawn between them, and the line is where the
jobs end:

| stage | what it does | runs app code | holds secrets |
|---|---|---|---|
| **A — build** | checks the app out at the tag, lints it, and packs it **unsigned** for each target | yes | no |
| **B — validate** | reads those packages: inventory and digests, permissions against the manifest, provenance against the tag, a comparison against the maintainer's release, a virus scan | no | no |
| **C — sign and submit** | puts the App Fair's signature on the package and uploads it to the store | no | yes |

Stage A hands stage B files and nothing else. Stage B opens them as archives. Stage C re-signs an
archive and uploads it, with the key material named on its own command line, so nothing the app
declares decides what is read. A job that runs a submitted app's build therefore never sees a key,
and the job that holds the keys never runs a line the app wrote.

What stages B and C read from the app's repository is its manifest, its store copy and its
translations, through a sparse checkout that `policy.yaml` lists path by path. No source, no build
scripts, no project files.

## What each stage checks

**Stage A** runs `day lint` before packing, so a submission also says that the project is a
well-formed Day project: ids resolve, every string is in the catalogue, the store listing is
complete.

**Stage B** answers five questions about the package in hand:

- What is in it? Every file, with its size and SHA-256, recorded in the run and kept as a report.
- Is it this app? The bundle id, version and build number, read from the package's own manifest,
  against what the app's `Day.toml` and its App Fair flavor say they should be.
- Does it ask for more than it declared? Every permission in the package, against the app's
  declared permissions mapped through day's own catalogue, plus the baseline the framework adds.
- Does it match the maintainer's release? The App Fair ships its own build; the maintainer's
  release of the same tag should contain the same files. Differences outside the signature block
  the submission, and a maintainer can waive one with the `allow-mismatch` label once the cause is
  understood.
- Is it clean? Every file scanned with ClamAV, and the provenance and SBOM beside the package
  checked against the commit the tag actually points at, with a build from a dirty checkout
  refused.

**Stage C** signs with `day sign apply`, which re-signs a package without rebuilding it, and
uploads through the fastlane lanes `day store stage` writes from the app's listing.

## What each side supplies

**The maintainer** supplies a public Day project with a release tag, an App Fair flavor carrying
the canonical bundle id and a version that climbs past what the stores already have, a store
listing (`store-appfair/`), and an app that meets the
[inclusion criteria](https://appfair.org/docs/inclusion-criteria/). Their own CI publishes the
release this queue compares against.

**The App Fair** supplies the developer accounts, the signing material, the review, and this
queue. Its secrets live in one job on one stage: a pull request opened from a fork reaches none of
them, and the publish workflow's `store` environment is what holds them.

The identity an app is published under belongs to the App Fair: `org.appfair.app.<token>` on iOS,
`org.appfair.app.<token with hyphens as underscores>` on Google Play
([why](https://appfair.org/docs/building/#bundle-id)). An app carries that identity in a
[Day build flavor](https://daybrite.dev/docs/flavors) — a `Day-appfair.toml` beside its `Day.toml`
— which keeps the maintainer's own builds under the maintainer's own id.

## Submitting

[CONTRIBUTING.md](CONTRIBUTING.md) is the reference for every key in the file and every rule the
checks apply. In short:

1. Open a [discussion](https://github.com/orgs/appfair/discussions) proposing the app, if it is
   new to the catalog.
2. Tag a release of the app, with its App Fair flavor's version and build number raised.
3. Add or edit `apps/<token>.yaml` in a pull request.
4. Watch the checks. They are the same ones that run on merge, so a green pull request is a
   submission that will publish.

## Running the checks locally

Everything the workflows check is in one script, which needs Python and PyYAML:

```sh
python3 scripts/queue.py validate --all         # every submission against policy.yaml
python3 scripts/queue.py selftest               # the rules against their own cases
python3 scripts/queue.py wiring                 # every workflow against the actions it calls
python3 scripts/queue.py plan --app Faire-Games # what the workflows would build

# The stages, by hand, against a package you already have.
python3 scripts/queue.py inspect --package fair-games-android-mdc.aab \
  --sibling fair-games-android-mdc.apk --out report.json
python3 scripts/queue.py compare --ours fair-games-android-mdc.aab --theirs release/fair-games-android-mdc.aab
python3 scripts/queue.py audit --app Faire-Games --report report.json \
  --metadata metadata.json --target android-mdc --sidecars . --expect-commit <sha>
```

`metadata.json` is `day --flavor appfair metadata --json`, read from a checkout of the app's
manifest paths.

## Repository secrets

The publish workflow reads these from the `store` environment. When one is missing, the upload it
belongs to stops and the run says which it was.

| secret | what it is |
|---|---|
| `DAY_APPLE_CERT_P12`, `DAY_APPLE_CERT_PASSWORD` | the iOS distribution certificate stage C signs with |
| `DAY_IOS_PROFILE_B64` | the App Store provisioning profile; an app with one of its own names a different secret in its `apple-app-store.profile-secret` |
| `DAY_ASC_KEY_ID`, `DAY_ASC_ISSUER`, `DAY_ASC_KEY_B64` | the App Store Connect API key that uploads and manages listings |
| `DAY_ANDROID_KEYSTORE_B64`, `DAY_ANDROID_KEY_ALIAS`, `DAY_KS_PASS`, `DAY_KEY_PASS` | the Play upload keystore |
| `DAY_PLAY_JSON_KEY` | the Google Play service-account key `supply` uploads with |

Apple issues a provisioning profile per bundle id, so the catalog keeps one profile secret per app
unless a wildcard profile covers the namespace. One Play upload key serves every app, because Play
App Signing lets a single upload key sign for many listings.

## Setting the repository up

Four settings decide whether any of this runs, and none of them lives in a file:

- **Actions policy.** The stages call composite actions from another organization
  (`daybrite/actions`, for the toolchain and the day CLI). Under *Settings → Actions → General*,
  "Allow actions and reusable workflows" has to admit `daybrite/*`.
- **A `store` environment.** The publish workflow's signing job names it, so the store secrets can
  live there rather than on the repository, with whatever reviewers or wait timer the project
  wants in front of them.
- **Branch protection on `main`.** A merge publishes, so `main` takes pull requests only, with
  review from the owners `.github/CODEOWNERS` names. The `record` job pushes one file
  (`state/published.json`) straight to `main`; allow the Actions bot to bypass the rule for that
  path, or drop the job and read the state from the run.
- **An `allow-mismatch` label.** Its name is in `policy.yaml`. A maintainer adds it to a pull
  request whose comparison cannot pass for a reason they have understood.

`workflow_dispatch` on the publish workflow is the manual door: a maintainer can republish any app
by token after a store-side failure, with no new commit.

## Layout

```text
apps/<token>.yaml               one submission per app — the file most pull requests touch alone
policy.yaml                     the channels, what the catalog accepts, and what stage B checks
schema/app.schema.json          the same shape for editors
scripts/queue.py                validate, plan, verify, authorize, inspect, compare, audit,
                                wiring, record, selftest — one program, PyYAML its one dependency
state/published.json            what has been published, written by the publish workflow
.github/actions/build-app       stage A: the app's own build, unsigned, with no secrets in reach
.github/actions/validate-package stage B: reads what A built, runs none of it
.github/actions/sign-submit     stage C: the App Fair's signature, and the store
.github/workflows/              checks (rules), pr (A + B), publish (A + B + C + record)
```
