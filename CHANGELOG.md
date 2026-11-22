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
