# Submitting an app

A submission is one file, `apps/<token>.yaml`. Adding it publishes an app; changing its `tag` and
`commit` publishes a new version. This page documents that file and the rules applied to it.

## Before a first submission

Open a [discussion](https://github.com/orgs/appfair/discussions) describing the app, who maintains
it, and how it meets the [inclusion criteria](https://appfair.org/docs/inclusion-criteria/). The
App Fair is the publisher of record for the catalog, so a first submission starts there. Updates
go straight to a pull request.

## What the app needs

Every app is published as `org.appfair.app.<token>`, where the token is the app's GitHub
organization and repository name. The maintainer's own builds keep the maintainer's identity; the
App Fair build uses the catalog's. In a Day project that separation is a
[build flavor](https://daybrite.dev/docs/flavors): a `Day-appfair.toml` beside `Day.toml`.

The flavor is named after this repository, so every app here carries `Day-appfair.toml` and a
submission does not choose the name. A fork called `gamesfair-apps` would build each app's
`gamesfair` flavor with no other change.

```toml
# Day-appfair.toml, in the app's repository
store = "store-appfair"
resources = "resource-appfair"   # optional: the icon the catalog build ships

[app]
id = "org.appfair.app.Faire-Games"
title = "Fair Games"
version = "1.9.0"
build = 36
artifact = "fair-games"
targets = ["ios-uikit", "android-mdc"]

[app.android]
id = "org.appfair.app.Faire_Games"   # Java package segments take no hyphen

[signing.ios]
team = "${DAY_APPLE_TEAM}"
key-id = "${DAY_ASC_KEY_ID}"
issuer = "${DAY_ASC_ISSUER}"
key-path = "${DAY_ASC_KEY}"

[signing.android]
keystore = "${DAY_ANDROID_KEYSTORE}"
key-alias = "${DAY_ANDROID_KEY_ALIAS}"
store-pass = "${DAY_KS_PASS}"
key-pass = "${DAY_KEY_PASS}"
```

The signing tables hold environment references, filled on the queue's runners from the App Fair's
secrets, so nothing secret is committed to the app.

The store listing — name, description, keywords, screenshots, release notes, icon — is the app's
`store-appfair/` directory and its `resource-appfair/` overlay. The queue reads them at the
submitted commit and does not modify them.

Raise the flavor's `version` and `build` before cutting the tag. Both stores order releases by
those two numbers and reject anything that repeats or lowers them.

The tag also needs a release carrying the packages the app's own CI built: an `.aab` for Android,
an `.ipa` for iOS. These are the **base app's** packages, so the app's existing release workflow
is enough and the flavor's packages never have to be published. The comparison covers executable
content and non-branding resources while normalizing identities and launcher icons, and the
flavor's package id, version, permissions and provenance are checked against its own manifest.

The source tag may be `v2.0.1` while the store flavor is version `1.9.0`: the tag pins the source
commit, and the flavor's version sequence belongs to its store listing. An app built with the
shared Day workflow already publishes the base packages.

## The file

```yaml
token: Faire-Games                                 # required
title: Fair Games                                  # required
tag: v2.0.1                                        # required
commit: 549bac3f78ce937146b56a7acecc35cb361175e3   # required
summary: Classic puzzle and arcade games, offline. # optional

distribution:                                      # required
  ios-uikit:
    - apple-app-store
  android-mdc:
    - google-play-store

apple-app-store:                                   # optional, per channel
  profile-secret: IOS_PROFILE_FAIRE_GAMES_B64
  submit: false
```

`scripts/queue.py add <token>` writes this file from the app's latest release, or from its
highest tag when GitHub reports no release. It takes the title from the app's store listing and
lists every channel the catalog publishes to. `scripts/queue.py update <token>` moves an existing
submission to the latest release, rewriting the `tag` and `commit` lines and leaving the rest of
the file alone. Both take `--tag` for a particular release, and both stop after writing and
validating the file. They read the app's tags over the network and touch nothing else.

To edit the file by hand, `scripts/queue.py resolve --token Faire-Games --tag v2.0.1` prints the
two lines.

| key | what it is |
|---|---|
| `token` | The app's GitHub organization and repository name, the name of this file, and the last segment of its bundle id. The app lives at `https://github.com/<token>/<token>`. It holds for the life of the app. |
| `title` | The name on the home screen and in the store, up to 30 characters. Unique in this catalog, and distinct from well-known apps elsewhere. |
| `tag` | The released tag, `vX.Y.Z`. The release assets hang off it. |
| `commit` | What that tag points at, in full and in lower case. Every stage checks this out. |
| `summary` | One line for people reading the catalog. The store listing is the app's own. |
| `distribution` | The channels for each target. Each target is built once; each channel under it is a submission. |
| `<channel>` | Settings for one channel, named after it, for a channel this app distributes to. |

The build flavor is set by the catalog and cannot be named in a submission.

### Why a commit and a tag

Tags can be moved. If a submission were reviewed at one commit and published from another, the
review would count for nothing, so the build, the validation and the signing all check out the
pinned commit.

The tag remains because release assets hang off it and because it names the version. When the tag
no longer points at the pinned commit, the checks report it, and the submission is either updated
or taken up with the maintainer.

### The channels

`policy.yaml` declares them. Today:

| channel | target | what it does | settings |
|---|---|---|---|
| `apple-app-store` | `ios-uikit` | uploads to App Store Connect | `submit`, `profile-secret` |
| `google-play-store` | `android-mdc` | uploads to the Play internal track | `submit` |
| `altstore`, `f-droid`, `samsung-galaxy-store` | | declared but not yet implemented; a submission naming one is rejected with that message | |

`submit` defaults to `false`, which uploads the build and stops. Set it to `true` to run the lane
that requests App Review, or on Play promotes the build to production.

`profile-secret` names a repository secret holding an App Store provisioning profile, for an app
that needs a particular one. Otherwise the run requests a profile from Apple for the app it is
publishing, against the certificate that will sign it.

Adding a channel means an entry in `policy.yaml`, a property in `schema/app.schema.json`, and an
arm in `.github/actions/sign-submit`. The selftest checks that the first two agree.

Unknown keys are errors: the file is strict so that a misspelled key cannot silently do nothing.
`policy.yaml` holds the patterns and lists that the checks apply, and editing it changes what the
catalog accepts.

The `# yaml-language-server: $schema=` line at the top of each submission points editors at
`schema/app.schema.json` for completion and validation.

This repository keeps no maintainer list. The checks ask GitHub instead: a pull request from
someone with write access to the app's repository, or a public member of its organization, passes
without comment, and anything else leaves a warning for the reviewer. A list kept here would go
out of date as soon as an app changed hands.

## What the checks do

A pull request runs the stages a merge runs, stopping before the signing.

1. **The rules against themselves** (`scripts/queue.py selftest`), so a change to the checks is
   checked too.
2. **The file**: the shape above, the namespace, the tag pattern, the channels and their targets,
   and the title's uniqueness across the catalog.
3. **The app against the file**: the pinned commit's manifest, read through a sparse checkout that
   excludes the source. `day metadata --json` must report `org.appfair.app.<token>`, a version
   matching the tag, and the targets the file asks for, and the tag must still point at the pinned
   commit.
4. **The build** (stage A): `day lint`, then `day pack --no-sign` for each target. This is the only
   stage that runs code from the app, and it holds no credentials, so a pull request cannot
   publish anything or reach a key.
5. **The validation** (stage B), on a runner that never saw the app's source:

   | check | what fails it |
   |---|---|
   | inventory | nothing; it records every file with its digest |
   | identity | a bundle id, version or build number that disagrees with the manifest |
   | permissions | a permission the app never declared |
   | comparison | content differing from the app's own release of the same commit |
   | provenance | a missing SBOM, a commit other than the pinned one, or a build from a dirty checkout |
   | scan | ClamAV finding something |

A red check is a submission that would fail on merge; the annotation names the file and line to
change.

### When the comparison differs

The App Fair publishes the build it made, checked against the release the app's CI published from
the same commit. A difference means one of the two is not reproducible from that source: a
timestamp baked into an asset, a dependency resolved differently, a different toolchain version.
The run lists the differing files.

Either fix the cause and tag again, or, once a maintainer of this repository understands and
accepts the difference, apply the `allow-mismatch` label and re-run. The label is recorded in the
run, so a waived comparison stays visible afterwards.

## What merging does

Merging is the approval. The publish workflow repeats stages A and B, then:

- **Stage C** signs the package stage A built using `day sign apply`, which re-signs the archive
  without rebuilding it, and uploads it through the fastlane lanes `day store stage` generates
  from the app's listing. The key material is named on the command line from this repository's
  secrets, so the app's own `[signing]` tables are not read.
- **The record** is written to `state/published.json`: what was published, from which tag, by which
  run.

The stores take it from there: Apple's review, Google's rollout. A rejection goes to the app's
maintainer, who fixes it in their repository, tags again, and opens a new pull request here.

To publish an app again without changing its file, after a store-side failure or a transient
upload error, a maintainer runs the `publish` workflow manually with the app token.

## Updating an app

Run `scripts/queue.py update <token>`. By hand it is two lines:

```diff
-tag: v1.9.0
-commit: 026ae1d62a8c49b1b0793aed8b5a2a0064ba95b6
+tag: v1.9.1
+commit: 8b03beeedf19b241954b31678ac8e3fbc816dcc7
```

`scripts/queue.py resolve --token <token> --tag <tag>` prints both lines for a particular tag.

Everything else follows from the app's repository at the new commit. An app that changes its title
changes this file too, since the uniqueness rule applies to the displayed title.

## When something fails

| where | what to do |
|---|---|
| the file's checks | read the annotation, fix the file, push to the same branch |
| the app's checks | the app and the submission disagree, usually about the flavor's id or version, or the tag has moved off the pinned commit. Fix it in the app, tag again, and update `tag` and `commit` here |
| the build | the app does not build at that commit on that target. Fix it in the app's repository |
| identity or permissions | the package and the manifest disagree; both come from the app, so the fix is there |
| the comparison | reproduce the difference, or have a maintainer waive it with the label |
| the scan | a maintainer will contact you; nothing is published after a scan finds something |
| the upload | open a [publication problem](https://github.com/appfair/appfair-apps/issues/new?template=publication-problem.yml) with the run link |

## Running the checks yourself

```sh
python3 scripts/queue.py add Faire-Games          # or `update Faire-Games`
python3 scripts/queue.py validate apps/Faire-Games.yaml
python3 scripts/queue.py selftest
python3 scripts/queue.py wiring
```

These need Python and PyYAML. `inspect`, `compare` and `audit` run against a package you already
have; the README lists those commands, which are the ones the workflows call.
