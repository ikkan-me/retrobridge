# Changelog

Toutes les évolutions notables de RetroBridge sont documentées ici.
Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/)
et le projet utilise le [versionnage sémantique](https://semver.org/lang/fr/).

## [2.0.0] — 2026-11-22

### Ajouté

- Assistant `setup` rejouable : chemins, Transmission, scoring, destination SD et timer.
- Modèle multi-disques (`game_files`) avec génération automatique des playlists `.m3u`.
- Export incrémental vers ES-DE et Knulli, avec conversion CHD/RVZ et staging local.
- Scan incrémental des torrents via `torrent_meta`.
- `status` affiche la taille acquise et la taille en cours de téléchargement.
- `tools verify` sans argument cible les torrents en erreur ou corrompus.
- `tools bios` renvoie vers RetroBios sans dupliquer son catalogue.
- L'export signale les fichiers présents uniquement sur la carte SD, sans les supprimer.

### Modifié

- `tools clean` est en dry-run par défaut ; `--apply` supprime réellement.
- Recherche pré-filtrée en SQL et options `--quiet` sur `scan`, `sync` et `export`.
- `find` ajoute par défaut ; `--no-add` limite à la recherche.

### Retiré

- Conversion automatique PAL → NTSC et commande `tools multidisc`.
- Catalogue BIOS interne (`tools sources`) au profit de RetroBios.

## [1.0.0]

- Première version stable : import de packs, sélection scorée, hard links, export SD.
