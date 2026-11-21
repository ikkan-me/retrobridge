# RetroBridge v2 🕹️🌉

[![CI](https://github.com/ikkan-me/retrobridge/actions/workflows/ci.yml/badge.svg)](https://github.com/ikkan-me/retrobridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

RetroBridge automates the gap between raw ROM downloads and ready-to-play handhelds. It seamlessly links Transmission torrent packs to an organized local library via hard links, and syncs curated setups directly to your ES-DE, Knulli, or Batocera targets.

> ⚠️ **Avertissement légal / Legal notice** — RetroBridge est un outil d'organisation et d'automatisation locale. Il ne fournit, n'héberge et ne distribue aucun jeu, ROM, BIOS ou autre fichier protégé par le droit d'auteur. Les packs torrents, ROMs et BIOS utilisés proviennent exclusivement de sources tierces choisies par l'utilisateur ; celui-ci doit disposer des droits nécessaires (jeux possédés, licences des BIOS, législation locale). L'auteur décline toute responsabilité quant à l'usage de l'outil. RetroBridge n'est affilié ni à Nintendo, Sony, Sega, Microsoft, ni à aucun éditeur ou ayant droit.
>
> *RetroBridge does not host, provide or distribute any copyrighted game, ROM or BIOS. You are responsible for using it only with content you are legally allowed to use.*

---

## 🎯 Principes d'architecture

- **Source unique de vérité** : Une base SQLite (`~/.local/share/retrobridge/retrobridge.db`) pilote l'ensemble des états (jeux, ROM sélectionnée, cache torrent, exports). Un jeu référence toujours un fichier réel d'un pack indexé : `tools audit` et `sync` refusent les références orphelines.
- **Zéro démon en arrière-plan** : Pas de service resident. Un système de TTL sur les métadonnées lance les rafraîchissements nécessaires à la demande, protégé par un verrou fichier non-bloquant (`fcntl.flock`).
- **Modèle « 3 Répertoires »** :
  1. **Download-Dir** : Répertoire brut où Transmission télécharge et seed (jamais altéré manuellement).
  2. **Library-Dir** : Arborescence locale propre organisée par système, utilisant des **hard links** vers le Download-Dir (zéro espace disque dupliqué, même partition).
  3. **Targets** : Vos cartes SD portables (Anbernic, Miyoo, Steam Deck, etc.) identifiées par un fichier UUID (`.retrobridge-target`).
- **Préservation des cartes SD** : Toutes les décompressions et conversions lourdes (`chdman`, `dolphin-tool`) s'effectuent sur votre SSD local dans un répertoire temporaire de staging (`/tmp/retrobridge/staging`), avant copie du fichier final sur la carte SD.
- **Scoring 60Hz / NTSC prioritaire** : Priorité absolue aux versions 60Hz (USA / World / Japan), sélection automatique des versions les plus récentes (`v2.00 > v1.01`, `Rev 2 > Rev 1`), et repli automatique sur le PAL 50Hz si le jeu n'est sorti qu'en Europe.

---

## 🚀 Installation rapide

```bash
git clone https://github.com/ikkan-me/retrobridge.git
cd retrobridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Configuration (`~/.config/retrobridge/config.toml`)

Le fichier est généré ou modifiable à votre convenance :

```toml
[paths]
download_dir = "/mnt/hdd-data/RetroGaming/Download"
library_dir  = "/mnt/hdd-data/RetroGaming/ROMs"
data_dir     = "~/.local/share/retrobridge"

[transmission]
host = "127.0.0.1"
port = 9091
path = "/transmission/rpc"
user = ""
password_env = "RETROBRIDGE_RPC_PASSWORD"
timeout = 30.0  # secondes (augmenter si Transmission est lent ou saturé)

[ttl]
scan_ttl   = 3600  # secondes avant de rescanner les torrents
status_ttl = 600   # secondes avant que `status` relance un sync automatique

[scoring]
region_preference = "NTSC"   # NTSC (60Hz en priorité) ou PAL
pal_language      = "french" # Langue préférée en cas de repli PAL

[targets.rg35xx]
profile    = "esde"
mount_path = "/run/media/votre-utilisateur/ROMS"
uuid       = "rg35xx-sd-card-uuid"
```

---

## 🎮 Commandes utilisateur

### 1. `retrobridge status`
Affiche en un écran l'état global de la collection, le ratio de partage et la santé du stockage :
```bash
retrobridge status
retrobridge status --json
retrobridge status --no-refresh   # ne pas lancer de sync automatique
```

Si la dernière synchronisation date de **plus de 10 minutes**, `status` lance automatiquement un `sync` avant l'affichage (indépendamment du timer systemd). Le délai est réglable via `[ttl] status_ttl` (600 s par défaut).

### 2. `retrobridge find <recherche> -s <système>`
Recherche dans le cache des torrents, vérifie la fraîcheur du cache et permet d'ajouter le jeu :
```bash
retrobridge find "Gran Turismo 4" -s ps2            # recherche + prompt d'ajout (q = annuler)
retrobridge find "Gran Turismo 4" -s ps2 --no-add   # recherche seule
retrobridge find "Gran Turismo 4" -s ps2 --yes      # meilleur candidat exact
retrobridge find "Ico" -s ps2 --update
```
- Ajout interactif après sélection : répondre `q` (ou autre chose qu'un numéro) annule.
- `--no-add` : recherche seule.
- `--yes` : meilleur candidat, uniquement si le titre est un match exact.
- `--update` : remplace la ROM d'un jeu déjà présent.
- `--dry-run`, `--limit`, `--full-path`, `--json`, `--no-refresh`.
- En `--json`, `find` est **read-only** sauf avec `--yes` (pas de prompt).
- L'ajout lance immédiatement le téléchargement dans Transmission. Si le RPC échoue, un message demande de lancer `retrobridge sync`.
- Les jeux multi-disques sélectionnent automatiquement tous les disques disponibles (même jeu/région).

`search` et `add` restent disponibles comme alias cachés pour les scripts.

### 3. `retrobridge sync [--dry-run]`
Réconciliation locale complète :
- Vérifie l'état de téléchargement dans Transmission.
- Crée les **hard links** dans `Library-Dir` pour les ROMs terminées.
- Regroupe automatiquement les jeux multi-disques `(Disc 1)`, `(Disc 2)` et génère les playlists `.m3u`.
- Exécute les suppressions locales en attente.
```bash
retrobridge sync --dry-run
retrobridge sync
```

### 4. `retrobridge export <target> [--tier 1|2|3] [--system X] [--raw] [--dry-run]`
Export incrémental vers une carte SD :
- Contrôle préalable de l'espace disponible sur la carte.
- Détection différentielle : ne convertit et ne copie que les nouveaux jeux ou ceux modifiés.
- Conversion automatique selon le profil (`.chd` pour PS1/PS2/Saturn, `.rvz` pour GameCube/Wii, extraction des `.zip` pour NES/SNES/GBA).
```bash
# Export partiel des classiques 2D (Tier 1)
retrobridge export rg35xx --tier 1

# Export complet de la collection
retrobridge export rg35xx

# Simulation d'export sans écriture
retrobridge export rg35xx --dry-run
```

### 5. Verrouillage & Suppression
- **`retrobridge pin <titre>` / `unpin <titre>`** : Verrouille la ROM sélectionnée pour l'exclure des remplacements automatiques.
- **`retrobridge remove <titre>`** : Suppression asynchrone sécurisée (désélectionne dans Transmission, supprime le hard link dans Library-Dir, supprime le fichier brut dans Download-Dir si non partagé, et planifie le nettoyage sur les cartes SD).

### 6. `retrobridge pack add <fichier.torrent>`
Ajoute un pack de ROMs en pause dans Transmission **et désélectionne tous ses fichiers** :
```bash
retrobridge pack add ~/torrents/mon-pack.torrent
```
Aucun octet n'est téléchargé tant qu'un jeu n'est pas explicitement ajouté avec `add`. `--keep-all` conserve la sélection complète si tu veux le pack entier.

### 7. `retrobridge setup [--check] [--step N]`
Assistant de configuration rejouable :

| Étape | Contenu |
| :---: | :--- |
| 1 | Chemins Download-Dir / Library-Dir, profil de destination et point de montage SD |
| 2 | Connexion Transmission (hôte, port) |
| 3 | Préférences de scoring (NTSC / PAL) |
| 4 | Automatisation optionnelle (timer systemd utilisateur) |

`retrobridge setup --check` affiche le diagnostic complet (Python, Transmission, `chdman`, `dolphin-tool`, espace disque, partition commune).

---

## 🛠️ Commandes utilitaires (`retrobridge tools`)

**Packs et Transmission**
- **`retrobridge tools scan`** : Force la mise en cache immédiate des fichiers des torrents Transmission.
- **`retrobridge tools apply [--dry-run]`** : Aligne les priorités Transmission sur la sélection réelle : seuls les fichiers des jeux ajoutés restent demandés, tout le reste des packs est désélectionné (exécuté aussi par `sync`).
- **`retrobridge tools audit`** : Vérifie qu'aucun jeu n'est orphelin (fichier absent des packs indexés, fichier disparu du disque, doublons). `sync` signale aussi automatiquement tout orphelin.
- **`retrobridge tools pause <id>` / `resume <id>`** : Pilotage direct d'un torrent (pause, reprise).
- **`retrobridge tools verify [id]`** : Vérifie un torrent ; sans ID, vérifie tous les torrents signalés en erreur ou corrompus.
- **`retrobridge tools relocate <id> <nouveau_chemin>`** : Déplace les données d'un torrent et réaligne son emplacement dans Transmission.
- **`retrobridge tools clean`** : Liste les fichiers orphelins du Download-Dir (dry-run par défaut) ; `--apply` les supprime.

**Export**
- **`retrobridge tools prepare-export <target> [--system X] [--tier N] [--dry-run]`** : Convertit les fichiers au format d'export dans le répertoire de staging, sans écrire sur la carte.
- **`retrobridge tools profiles`** : Liste les profils frontends disponibles (`esde`, `knulli`).

**BIOS et base de données**
- **`retrobridge tools bios`** : Propose l'installation des BIOS via **RetroBios** (outil externe), sans dupliquer son catalogue.
- **`retrobridge tools db init`** : Initialise le schéma de la base SQLite v2.
- **`retrobridge tools db migrate [--dry-run]`** : Aligne les données sur les codes systèmes canoniques (fusion des alias historiques `GC`, `GENESIS`, `PS1`, `SMS`, `GG`, `TG-16`).
- **`retrobridge tools db backup [--dest <chemin>]`** : Crée une copie horodatée de la base.
- **`retrobridge tools db import-legacy <roms_manager.db>`** : Importe une ancienne base v1 (les jeux sans fichier réel sont ignorés).

---

## 📊 Tiers d'exportation

Pour adapter facilement votre collection à la taille de votre carte micro-SD :

| Tier | Nom | Systèmes inclus |
| :---: | :--- | :--- |
| **1** | **Classiques 2D** | NES, SNES, Mega Drive, Master System, Game Gear, PC Engine, GB, GBC, GBA, Arcade |
| **2** | **Première 3D** | Tier 1 + PS1, Saturn, Nintendo 64 |
| **3** | **PS2 & Suivantes** | Tier 2 + Dreamcast, PS2, GameCube, Wii, PSP, Nintendo DS |

---

## ⏱️ Synchronisation périodique optionnelle (systemd utilisateur)

Aucun démon n'est requis : `sync` reste lançable à la main. Pour l'automatiser toutes les 10 minutes :

```bash
mkdir -p ~/.config/systemd/user
cp systemd/retrobridge-sync.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now retrobridge-sync.timer
systemctl --user list-timers retrobridge-sync.timer
```

Le mot de passe RPC peut être fourni dans `~/.config/retrobridge/rpc.env` :

```bash
echo 'RETROBRIDGE_RPC_PASSWORD=mon_mot_de_passe' > ~/.config/retrobridge/rpc.env
chmod 600 ~/.config/retrobridge/rpc.env
```

---

## 🧪 Tests & Qualité

La suite de tests unitaires et de caractérisation s'exécute via `pytest` :
```bash
pytest -v tests
```
Toutes les suites (caractérisation v1, lock, profils, sync hard links, suppression asynchrone, multi-disques m3u, export incrémental, scoring NTSC 60Hz) sont couvertes.

---

## 👏 Remerciements & Crédits

- **[RetroBios](https://github.com/Abdess/retrobios)** par [Abdess](https://github.com/Abdess) : un grand merci pour son travail exceptionnel sur la centralisation, la vérification des sommes de contrôle SHA256 et la distribution des fichiers BIOS pour les plateformes d'émulation rétrogaming.

---

## 📄 Licence

Distribué sous licence **MIT** — voir [LICENSE](LICENSE). Le logiciel est fourni « en l'état », sans garantie d'aucune sorte. Cette licence ne couvre ni les ROMs, ni les BIOS, ni les packs torrents, qui restent soumis à leurs propres droits et licences.
