# Submitting an app

A submission is one file, `apps/<token>.yaml`. Adding it publishes an app; changing its `tag` and
`commit` publishes a new version. This page documents that file and the rules applied to it.

## Before a first submission

Open a [discussion](https://github.com/orgs/appfair/discussions) describing the app, who maintains
it, and how it meets the [inclusion criteria](https://appfair.org/docs/inclusion-criteria/). The
App Fair is the publisher of record for the catalog, so a first submission starts there. Updates
go straight to a pull request.

## What the app needs

An app is published under the id its build resolves to, read from its own manifest and from its
`Day-appfair.toml` when it carries one. Every such id starts with `org.appfair.app.`; what follows
is the app's own business. `org.appfair.app.<token>` is the convention a new app follows, where
the token is the app's GitHub organization and repository name, and an app that arrived with store
records under another spelling keeps them. Two apps may not publish under one id.

Where the maintainer's own builds go out under the maintainer's identity, the App Fair's identity
lives in a [build flavor](https://daybrite.dev/docs/flavors): a `Day-appfair.toml` beside
`Day.toml`, which leaves `Day.toml` alone. An app written for the App Fair states that identity
in `Day.toml` itself and carries no flavor; the queue then builds the project as it stands, and
the two builds the comparison holds against each other have nothing to differ about.

The flavor is named after this repository, so a submission does not choose the name. A fork
called `gamesfair-apps` would build each app's `gamesfair` flavor with no other change.

```toml
# Day-appfair.toml, in the app's repository — for an app that keeps its own identity in Day.toml
store = "store-appfair"
resources = "resource-appfair"   # optional: the icon the catalog build ships

[app]
id = "org.appfair.app.Faire-Games"
title = "Fair Games"
build = 36          # the store's build counter, past what the stores already have
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
`store-appfair/` directory and its `resource-appfair/` overlay, or plain `store/` and `resource/`
for an app with no flavor. The queue reads them at the submitted commit and does not modify them.

The tag names the version. `v2.0.2` publishes 2.0.2, so a flavor takes the source version and
states no `version` of its own; a flavor that does is accepted only when the tag agrees with it.

The `build` is the store's counter rather than the source's, because the store counts builds per
app and the App Fair's record has a history the source repository does not. Raise it before
cutting the tag: both stores order releases by the version and the build, and reject anything that
repeats or lowers them.

The tag also needs a release carrying the packages the app's own CI built: an `.aab` for Android,
an `.ipa` for iOS. These are the **base app's** packages, so the app's existing release workflow
is enough and the App Fair's own packages never have to be published. The comparison covers
resources, components and DEX while normalizing identities and launcher icons and allowing the
app's own binary to differ, and the submitted package's id, version, permissions and provenance
are checked against the manifest it was built from.

An app built with the shared Day workflow already publishes the base packages.

## The file

```yaml
token: Faire-Games                                 # required
title: Fair Games                                  # required
tag: v2.0.1                                        # required
commit: 549bac3f78ce937146b56a7acecc35cb361175e3   # required
summary: Classic puzzle and arcade games, offline. # optional
id: org.appfair.app.Faire-Games                    # optional, pins the record published to
android-id: org.appfair.app.Faire_Games            # optional, the same pin for Google Play

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
| `tag` | The released tag, `vX.Y.Z`. It names the version published, so the app at that commit has to build `X.Y.Z`. The release assets hang off it. |
| `commit` | What that tag points at, in full and in lower case. Every stage checks this out. |
| `summary` | One line for people reading the catalog. The store listing is the app's own. |
| `id` | Pins the bundle id this app publishes under. Optional: the queue reads the id from the build either way, and any id the stores accept inside `org.appfair.app.` is allowed. Stating it means a build that changes the record it publishes to is caught by the checks. No two apps may publish under one id. |
| `android-id` | The same pin for Google Play, when the package name is not the bundle id with hyphens as underscores. |
| `distribution` | The channels for each target. Each target is built once; each channel under it is a submission. |
| `<channel>` | Settings for one channel, named after it, for a channel this app distributes to. |

The build flavor is set by the catalog and cannot be named in a submission. An app that carries
no `Day-appfair.toml` is built as it stands.

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
| `apple-app-store` | `ios-uikit` | submits the build for App Review | `submit`, `profile-secret` |
| `google-play-store` | `android-mdc` | submits the build to the production track | `submit` |
| `altstore`, `f-droid`, `samsung-galaxy-store` | | declared but not yet implemented; a submission naming one is rejected with that message | |

Publishing means submitting. A merged submission asks Apple to review the build and Google to
review and roll it out to production; neither needs a setting to say so. Releasing on the App
Store stays manual — an approved version waits for the Release button — and Google's rollout
starts when review passes.

`submit: false` under a channel is the exception, for a build that should be uploaded and left
alone: the App Store version then sits in Prepare for Submission with no build attached, and Play
holds the bundle in the internal track as a draft.

Before anything is signed, the queue asks each store what it already holds, and stops there when
a release cannot land: a Play version code used once is used for good, so a repeated `build` is
refused before a package is uploaded, and an App Store version is refused while any language on
the record is missing its What's New or its screenshots. Every language the store record carries
needs both — the listing in the app's repository has to cover them, or the record has to drop
them.

After each lane runs, the queue asks the store what happened and fails the job when the answer
disagrees: App Store Connect has to show the build uploaded, attached to the version, and the
version in a reviewing state; Google Play has to show the version code in the production track as
a completed or in-progress release.

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
2. **The file**: the shape above, the tag pattern, the channels and their targets, and the
   uniqueness of the title and of the ids across the catalog.
3. **The app against the file**: the pinned commit's manifest, read through a sparse checkout that
   excludes the source. `day metadata --json` must report the version the tag names and the
   targets the file asks for, the tag must still point at the pinned commit, and the id it builds
   has to match an `id` pin when the file states one.
4. **The reviewer's comment**: for an update, the range between the commit already published and
   the one this pull request proposes, with the number of commits and files, the build and
   packaging files inside it, and a link to read it on GitHub. A first submission gets a link to
   the source at its commit. The comment is rewritten on every push.
5. **The build** (stage A): `day lint`, then `day pack --no-sign` for each target. This is the only
   stage that runs code from the app, and it holds no credentials, so a pull request cannot
   publish anything or reach a key.
6. **The validation** (stage B), on a runner that never saw the app's source:

   | check | what fails it |
   |---|---|
   | inventory | nothing; it records every file with its digest |
   | identity | a bundle id, version or build number that disagrees with the manifest |
   | permissions | a permission the app never declared |
   | comparison | content differing from the app's own release of the same commit, beyond the app's own binary |
   | provenance | a missing SBOM, a commit other than the pinned one, or a build from a dirty checkout |
   | scan | ClamAV finding something |

A red check is a submission that would fail on merge; the annotation names the file and line to
change.

### When the comparison differs

The App Fair publishes the build it made, checked against the release the app's CI published from
the same commit. Identity is normalized on both sides. Where a flavor states a different display
name, the app's own binary is expected to differ, since day compiles that name into it —
`policy.yaml: expected-differences` names those paths and the run reports them as expected. An app
with no flavor is built from the same manifest as its own release, so nothing is expected to
differ.

Any other difference means one of the two builds is not reproducible from that source: a timestamp
baked into an asset, a dependency resolved differently, a different toolchain version. The run's
summary names each differing file, what differs inside it, and how the two builds' tools compare,
so a difference that comes from the toolchain reads as one.

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

The checks comment on the pull request with the range between the two commits, so the reviewer
reads the source changes going into the release. Release notes on the tag are the other half of
that: a release with no notes leaves the reviewer reading commits.

## When something fails

| where | what to do |
|---|---|
| the file's checks | read the annotation, fix the file, push to the same branch |
| the app's checks | the app and the submission disagree, usually about a version that is not the one the tag names, a tag that has moved off the pinned commit, or an `id` pin the build no longer matches. Fix it in the app, tag again, and update `tag` and `commit` here |
| the build | the app does not build at that commit on that target. Fix it in the app's repository |
| identity or permissions | the package and the manifest disagree; both come from the app, so the fix is there |
| the comparison | reproduce the difference, or have a maintainer waive it with the label |
| the scan | a maintainer will contact you; nothing is published after a scan finds something |
| the upload | open a [publication problem](https://github.com/appfair/appfair-apps/issues/new?template=publication-problem.yml) with the run link |
| the store check before signing | the release cannot land as it stands: Play already has this version code (raise `build` in the manifest and tag again), or the App Store record carries a language the listing does not |
| the confirmation | the lane finished but the store does not show the submission. A binary already uploaded cannot be sent again under the same build number, so a maintainer finishes it by running the publish workflow with `channel: apple-app-store` and `lane: ios submit` |

## Running the checks yourself

```sh
python3 scripts/queue.py add Faire-Games          # or `update Faire-Games`
python3 scripts/queue.py validate apps/Faire-Games.yaml
python3 scripts/queue.py review --app Faire-Games --offline
python3 scripts/queue.py selftest
python3 scripts/queue.py wiring
```

These need Python and PyYAML. `inspect`, `compare` and `audit` run against a package you already
have; the README lists those commands, which are the ones the workflows call.
