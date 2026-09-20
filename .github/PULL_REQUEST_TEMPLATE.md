<!--
Thank you for submitting to the App Fair catalog.

One app per pull request, one file under apps/. The checks below run automatically the moment you
open it, so this list is for the maintainer reading the change.

Specialized templates, if you prefer one:
  ?template=new-app.md   a first submission
  ?template=update.md    a new version of an app already in the catalog
-->

## The app

- **Token:** <!-- Faire-Games -->
- **Title:** <!-- Fair Games -->
- **Tag and commit:** <!-- v2.0.0 at 8b03beeedf19b241954b31678ac8e3fbc816dcc7 -->
- **Channels:** <!-- apple-app-store, google-play-store -->

## What this changes

<!-- A new app, or what is new in this version. A sentence per notable change is plenty; the
     release notes on the store come from the app's own store listing. -->

## Confirmations

- [ ] The tag exists in the app's repository, points at the pinned commit, and is the version
      to publish.
- [ ] Its release carries the packages the app's CI built (`.aab`, `.ipa`), which the App Fair's
      own build is compared against.
- [ ] The app builds under `org.appfair.app.<token>`: its `Day-appfair.toml` states that bundle
      id, and the version and build number climb past what the stores already have.
- [ ] The app meets the [inclusion criteria](https://appfair.org/docs/inclusion-criteria/).
- [ ] The app is free of advertising, tracking, analytics, gambling and cryptocurrency.
- [ ] The store listing in the app's repository (`store/`, or the flavor's `store-<name>/`) is
      current: title, description, keywords, screenshots, release notes.
- [ ] I maintain this app — I have write access to its repository, or its maintainers know this
      submission is coming.

## For a first submission

- [ ] The displayed title is new to the catalog and stands apart from well-known apps on the
      commercial stores.
- [ ] The App Fair has agreed to publish this app (a proposal discussion, an issue, or a
      conversation with a maintainer).
