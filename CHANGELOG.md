# Changelog

All notable changes to RetroBridge are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [2.0.0] — 2026-11-22

### Added

- Replayable `setup` wizard: paths, Transmission, scoring, SD destination, and timer.
- Multi-disc model (`game_files`) with automatic `.m3u` playlist generation.
- Incremental export to ES-DE and Knulli, with CHD/RVZ conversion and local staging.
- Incremental torrent scan via `torrent_meta`.
- `status` shows acquired size and in-progress download size.
- `tools verify` without an argument targets errored or corrupt torrents.
- `tools bios` points to RetroBios without duplicating its catalog.
- Export reports files present only on the SD card, without deleting them.
- First stable release: pack import, scored selection, hard links, SD export.

### Fixed

- Multi-disc games whose disc names carry a per-disc subtitle
  (`(Disc 1) (Subsistence)`, `(Disc 2) (Persistence)`) now select every disc
  and generate the matching `.m3u` playlist.
- `find` recognizes a game that is already present even when the query
  includes a disc suffix (`... Disk 2`).
- `genesis` and `megadrive` are treated as one console everywhere
  (`list`, `find`, `pin`, `unpin`, `remove`), and legacy pack paths are no
  longer mistaken for Mega Drive (`.../Sony - PlayStation Portable/Sega
  Genesis Collection.zip`, `Neon Genesis`, `Operation Genesis`).
- Export of disc images: the whole disc set is extracted before conversion
  (`cue` + all `bin` tracks, `gdi` + tracks), so PSX/Saturn/CD games are
  converted instead of failing on a missing track.
- Export errors and file paths containing `[...]` no longer crash the CLI
  (they were parsed as Rich markup) and no longer leave staging files behind.
- Disc conversion has a timeout: a corrupt image can make `chdman` loop
  forever, which used to hang the export indefinitely.
