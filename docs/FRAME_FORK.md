# Frame fork: behavior, installation and maintenance

This fork is based on upstream SamsungTV Smart v0.14.5. It keeps the integration
`domain` and device unique IDs unchanged so existing Home Assistant entries,
entities and automations can survive a repository-source migration. The upstream
license and author credits are retained.

## State and control changes

For Frame devices reporting `FrameTVSupport=true`, the media-player entity adds
`tv_mode`:

| REST power | Art state | `tv_mode` |
| --- | --- | --- |
| `standby` or `off` | Any, including cached Art state | `off` |
| `on` | `on` | `art` |
| `on` | `off` | `tv` |
| Unreachable, known power/Frame capabilities and remote WS disconnected | Any | `off` |
| `on` | Temporarily unavailable, with a last valid Art state | Last valid `art` or `tv` |
| Insufficient evidence | Any | `unknown` |

The media-player entity retains upstream semantics: Art Mode is `off` for media
playback, while ordinary TV viewing is `on`. Use `tv_mode` for a three-mode policy.
`art_mode_status` preserves `unavailable` instead of reporting a false `off`.

The fork also:

- Parses both `get_artmode_status` and legacy `artmode_status` responses.
- Rejects invalid Art values, queries a healthy Art connection every 15 seconds,
  and notifies HA when the received mode changes.
- Discovers device power capabilities when HA starts while the TV is sleeping.
- Treats unreachable REST plus a disconnected remote WebSocket as deep standby
  only after power or Frame capabilities have been learned. REST failure with an
  active remote WebSocket remains unknown; a fresh HA start without discovered
  capabilities cannot infer deep standby from the integration alone.
- Retains the last valid Art mode during Art WebSocket reconnects only while REST
  confirms `PowerState=on`. Without a prior valid mode, the mode remains unknown.
  `art_mode_status` still exposes the raw unavailable connection state. The cached
  mode is an estimate until Art communication recovers, with no expiry timer.
- Gives standby, deep standby and unknown physical power precedence over cached
  Art state when requesting wake.
- Allows an Art wake request before the sleeping device establishes its Art channel.

An operator can add a guarded smart-plug fallback for the first discovery after
HA restart: with mains on and numeric power, use a device-calibrated standby
threshold and the explicit watch-mode helper. This is an automation estimate,
not an independent Art API confirmation. Keep a suitable IR wake path available
for deep standby and verify wake before sending further mode commands.

These changes do not force every power-on into Art Mode. Default-Art versus
explicit watching remains an operator automation policy. Network power-key
acknowledgement may still fail to wake some firmware; do not infer success from
service completion. Confirm the resulting mode. A guarded IR wake may be useful
when an operator already has a suitable remote configured.

## Migrate from upstream or a locally patched installation

1. Back up `custom_components/samsungtv_smart` and the relevant HA configuration.
2. Add `https://github.com/Tefx/ha-samsungtv-smart` under HACS custom repositories,
   category **Integration**. Confirm a release with `samsungtv_smart.zip` is available.
3. Use HACS to remove the downloaded **upstream repository** before installing this
   fork. This temporarily removes component files. Do not restart HA in that gap.
   Do not delete the SamsungTV Smart integration entry under HA Devices & services.
4. Download this fork's published release through HACS. The runtime domain remains
   `samsungtv_smart`; no second Samsung integration entry is required.
5. Validate HA configuration, restart HA, and confirm the existing entity IDs,
   `tv_mode`, service calls and automations still work.
6. Verify HACS lists this fork as installed and upstream as not installed. Check
   that the installed release and component manifest version agree.

HACS updates and redownloads now obtain the fixed files from this fork. This does
not protect against manually installing upstream over the same domain or
manually editing downloaded component files. Future fixes must be committed and
released here, rather than applied only to a live HA filesystem.

To roll back, reverse the HACS source migration and reinstall the desired release,
or restore the saved component directory and HACS source consistently. Retain HA
integration entries and credentials. Automations depending on `tv_mode` require
this fork or an upstream version providing an equivalent interface.

## Updating the fork

`frame-stable` is the maintained default branch. `origin` should point to this
fork, and `upstream` to `https://github.com/ollo69/ha-samsungtv-smart.git`.

Upstream changes are reviewed and merged explicitly; this fork does not
silently follow upstream master. Fetch upstream tags into a separate namespace
so future upstream release names cannot replace fork release tags:

```sh
git fetch upstream 'refs/tags/*:refs/tags/upstream/*'
git switch frame-stable
# Select and review a stable upstream release before merging it:
git merge refs/tags/upstream/vX.Y.Z
python -m pytest --noconftest -o addopts= tests/test_frame_state.py
```

Keep a minimal diff, resolve upstream changes without dropping Frame behavior,
and repeat live standby/Art/TV, HA-started-in-standby and rapid command checks.
The focused tests execute actual source method bodies with transport stubs. They
are not full HA-runtime or physical-TV tests. Existing naive-UTC deprecation
warnings are inherited from the library's datetime convention.

The canonical product version is `custom_components/samsungtv_smart/manifest.json`.
Use SemVer: fixes increment PATCH, optional public interfaces increment MINOR,
and incompatible interfaces increment MAJOR. The initial fork release is 0.15.0
because it adds `tv_mode`. Its packaging job exposed an annotated-tag checkout
issue before producing a HACS asset; 0.15.1 fixes that packaging issue and is the
first installable release. The original tag remains unchanged. Fork versions do
not have to match upstream versions; record the upstream base in release notes.

After updating the manifest, tests and documentation, commit and push the branch.
Wait for **Frame regression** to pass, then create an immutable annotated tag and
publish a GitHub release:

```sh
VERSION=$(python -c 'import json; print(json.load(open("custom_components/samsungtv_smart/manifest.json"))["version"])')
git tag -a "v$VERSION" -m "Release v$VERSION: Frame fixes"
git push origin "v$VERSION"
gh release create "v$VERSION" --verify-tag --title "v$VERSION" --notes-file release-notes.md
```

The **Release** workflow reruns the Frame regressions, verifies the annotated tag
and manifest version, builds `samsungtv_smart.zip`, and uploads the HACS asset.
Do not move a published tag or replace its asset with different bytes. Check the
asset and workflow before advertising the release or updating HA. HACS tracks
published releases from this fork; it does not merge upstream changes for you.
