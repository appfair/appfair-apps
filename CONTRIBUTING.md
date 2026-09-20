# Submitting an app

A submission is one file: `apps/<token>.yaml`. Adding it publishes an app; changing its `tag`
publishes a new version of one. This page is the reference for that file and for the rules the
checks apply to it.

## Before a first submission

Open a [discussion](https://github.com/orgs/appfair/discussions) describing the app, who maintains
it, and how it meets the [inclusion criteria](https://appfair.org/docs/inclusion-criteria/). The
App Fair is the publisher of record for everything in this catalog, so a first submission begins
as a conversation and becomes a pull request. An update goes straight to a pull request.

## What the app needs

The App Fair publishes every app under its own identity: `org.appfair.app.<token>`, where the
token is the app's GitHub organization and repository name. The maintainer's own builds keep the
maintainer's identity, and the App Fair build takes the catalog's. In a Day project that
separation is a [build flavor](https://daybrite.dev/docs/flavors) — a `Day-appfair.toml` beside
`Day.toml`:

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

Every value in the signing tables is an environment reference the queue's runners fill from the
App Fair's secrets, so the app repository stays free of anything secret.

What the store shows — name, description, keywords, screenshots, release notes, icon — is the
app's `store-appfair/` listing and `resource-appfair/` overlay. The queue reads them at the
submitted tag and leaves them alone.

Two numbers decide whether a submission can be accepted at all. Both stores order releases by the
version and the build number, and both refuse anything that repeats or lowers them, so the
flavor's `version` and `build` have to be raised before the tag is cut.

The tag also has to carry a release with the packages the app's own CI built: an `.aab` for
Android, an `.ipa` for iOS. The App Fair publishes the build it makes here, and that release is
what it is compared against, so a tag with no packages attached leaves a submission with nothing
to be checked against. An app built with the shared Day workflow already publishes them.

## The file

```yaml
token: Faire-Games                                 # required
title: Fair Games                                  # required
repo: https://github.com/Faire-Games/Faire-Games   # required
tag: v1.9.0                                        # required
flavor: appfair                                    # optional
summary: Classic puzzle and arcade games, offline. # optional
targets: [ios-uikit, android-mdc]                  # required
maintainers: [marcprux]                            # required

stores:                                            # required: at least one true
  apple: true
  play: true

apple:                                             # optional
  profile-secret: IOS_PROFILE_FAIRE_GAMES_B64
  submit: false

play:                                              # optional
  submit: false
```

| key | what it is |
|---|---|
| `token` | The app's GitHub organization and repository name, the name of this file, and the last segment of its bundle id. It holds for the life of the app. |
| `title` | The name on the home screen and in the store, up to 30 characters. Unique in this catalog, and distinct from well-known apps elsewhere. |
| `repo` | The app's public GitHub repository. The queue's runners carry this repository's token alone, so the source has to be readable without one of its own. |
| `tag` | The released tag to build, `vX.Y.Z`. What is published has to be what the maintainer released, and it has to still be there later. |
| `flavor` | The Day flavor carrying the App Fair identity. Leave it out when the app's `Day.toml` already builds under `org.appfair.app.<token>`. |
| `summary` | One line for people reading the catalog. The store listing is the app's own. |
| `targets` | What is built and submitted: `ios-uikit`, `android-mdc`, or both. Each needs its store turned on. |
| `maintainers` | The GitHub accounts that may change this file, with the `@` left off. The first is the maintainer of record. |
| `stores.apple` / `stores.play` | Which stores this app is published to. |
| `apple.profile-secret` | The NAME of the repository secret holding this app's App Store provisioning profile, when it has one of its own. The value belongs in the secret. |
| `apple.submit` / `play.submit` | Ask for App Review, or promote to production, on top of uploading. They default to `false`, which suits a queue where publishing a binary and asking a store to review it are two decisions. |

The file is strict: an unknown key is an error, because a key nobody reads is a rule nobody
applied. `policy.yaml` holds the patterns and lists the checks use, and editing it is how the
catalog changes what it accepts.

An editor that understands `# yaml-language-server: $schema=` completes and checks the file from
`schema/app.schema.json`; the line at the top of every submission points at it.

## What the checks do

Opening a pull request runs the stages a merge will run, stopping before the signing:

1. **The rules against themselves** (`scripts/queue.py selftest`), so a change to the checks is
   itself checked.
2. **The file** — the shape above, the namespace, the tag pattern, the store and target pairing,
   and the title's uniqueness across the catalog.
3. **The app against the file** — the tag's manifest is read on its own, through a sparse checkout
   that leaves the source behind. `day metadata --json` has to report `org.appfair.app.<token>`, a
   version matching the tag, and the targets the file asks for.
4. **The build** (stage A) — `day lint`, then `day pack --no-sign` for each target. This is the
   only stage that runs anything the app supplied, and it holds no credentials, so opening a pull
   request cannot publish anything or reach a key.
5. **The validation** (stage B) — on a fresh runner that never saw the app's source, the packages
   are opened as archives and held against everything else that is known:

   | check | what would fail it |
   |---|---|
   | inventory | (records every file with its digest; nothing fails) |
   | identity | the package's bundle id, version or build number disagreeing with the manifest |
   | permissions | the package asking for something the app never declared |
   | comparison | files differing from the maintainer's release of the same tag |
   | provenance | a missing SBOM, a commit that is not the tag's, or a build from a dirty checkout |
   | scan | ClamAV finding something |

A red check is a submission that would fail on merge, and the annotation says which line of which
file to change.

### When the comparison differs

The App Fair ships the build it made. The maintainer's release of the same tag is what that build
is checked against, so a difference means one of the two is not reproducible from the source at
that tag — a timestamp baked into an asset, a dependency resolved differently, a toolchain
version. The run lists the differing files.

Two ways forward: fix the cause and tag again, or, when a maintainer of this repository has
understood the difference and accepts it, add the `allow-mismatch` label and re-run. The label is
recorded in the run, so a waived comparison stays visible afterwards.

## What merging does

Merging is the approval. The publish workflow repeats stages A and B, then signs and uploads:

- **Stage C** takes the package stage A built, puts the App Fair's signature on it with
  `day sign apply` — which re-signs the archive without rebuilding anything — and uploads it
  through the fastlane lanes `day store stage` writes from the app's listing. The key material is
  named on the command line from this repository's secrets, so the app's own `[signing]` tables
  are never read.
- **The record** goes into `state/published.json`: what was published, from which tag, by which
  run.

What happens next belongs to the stores: Apple's review, Google's rollout. A rejection reaches the
app's maintainer, who fixes it in their repository, tags again, and opens a new pull request here
with the new tag.

To publish an app again with its file unchanged — after a store-side failure or a transient upload
error — a maintainer of this repository runs the `publish` workflow manually with the app token.

## Updating an app

Change `tag`. That is the whole update:

```diff
-tag: v1.9.0
+tag: v1.9.1
```

Everything else follows from the app's repository at the new tag. An app that changes its title
changes this file too, because the catalog's uniqueness rule is about the displayed title.

## When something fails

| where | what to do |
|---|---|
| the file's checks | read the annotation, fix the file, push to the same branch |
| the app's checks | the app and the submission disagree, usually about the flavor's id or version. Fix it in the app, tag again, update `tag` here |
| the build | the app does not build at that tag on that target. It is the app's build, so it is fixed in the app's repository |
| identity or permissions | the package and the manifest disagree. Both come from the app, so the fix is there |
| the comparison | see above: reproduce, or have a maintainer waive it with the label |
| the scan | a maintainer takes it up with you directly; a submission is never published on a scan that found something |
| the upload | open a [publication problem](https://github.com/appfair/appfair-apps/issues/new?template=publication-problem.yml) with the run link |

## Running the checks yourself

```sh
python3 scripts/queue.py validate apps/Faire-Games.yaml
python3 scripts/queue.py selftest
python3 scripts/queue.py wiring
```

These need Python and PyYAML. The stages that read a package — `inspect`, `compare`, `audit` — run
against a package you already have; the README shows the commands, and they are the same ones the
workflows call.
