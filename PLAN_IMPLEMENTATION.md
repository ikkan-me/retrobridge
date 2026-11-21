# PLAN_IMPLEMENTATION.md — exécution de `finalize.md`

> Plan de mise en œuvre, pas le cahier des charges. Le quoi est dans `finalize.md`, ici on découpe le comment.
> **Jour 1 : 2026-11-21 — terminé** (décisions, baseline, harnais de test).
> Lots A à G à venir, un PR par lot.

---

## 0. Décisions verrouillées (jour 1)

| # | Sujet | Décision retenue | Note |
|---|---|---|---|
| D1 | `status` et fraîcheur | **Auto-sync conservé mais encadré.** Actif par défaut ; `--no-refresh` = lecture stricte. Il ne scanne jamais, ne touche pas à la sélection Transmission et n'exécute aucune action destructive. | Implémenté jour 2 (A0). |
| D2 | Timer | 10 min, `scan_ttl = 3600`, `status_ttl = 600` (déclenche l'auto-sync safe). | `systemd --user`, fallback cron. |
| D3 | Langues | `preferred_languages = ["fr", "en"]`, **NTSC 60 Hz d'abord**, PAL seulement en repli. | Compatibilité `pal_language` conservée. |
| D4 | Commandes | `find` public ; `search` et `add` deviennent des alias cachés. | Pas de rupture de script. |
| D5 | Live | Barres/rafraîchissement 1×/s **uniquement en TTY** ; `--no-live` sinon. | `--json` sans barres. |
| D6 | Multi-disques | Hors P0 ; décision de schéma au lot F. | Ne pas bloquer la v2. |
| D7 | Volume | On n'affiche pas la taille réelle pour l'instant. | `space` retiré du périmètre. |
| D8 | Complétion | `--install-completion` proposé en fin de setup, documenté. | Options Typer existantes. |
| D9 | Portables | GB/GBC/GBA/NDS/PSP/Game Gear : **pas de PAL/NTSC**. Région ignorée dans le score, langue prioritaire. | Hotfix avant lot C. |
| D10 | Ranking `find`/`add` | **Score d'abord**. Le bonus de titre ne sert qu'à départager deux scores égaux. | Évite le homonyme partiel. |
| D11 | « Téléchargés » | `Téléchargés = acquis (linked + downloaded)`, avec « dont liés : N ». JSON expose `acquired` et `downloaded_pending`. | Plus d'écart étrange linked > téléchargés. |

---

## 1. Baseline mesurée (2026-11-21, ~21:50)

| Mesure | Valeur | Commentaire |
|---|---:|---|
| `pytest -q` | **49 passed** en 1,16 s | Point de départ sain. |
| `pytest -q` après harnais | **54 passed** en 1,82 s | +5 tests de characterization jour 1. |
| `status --json --no-refresh` | **0,3–2,3 s** | Variable selon la charge du RPC ; ~1,6 s pour `get_session_stats()` à froid. |
| `get_system_status()` sans RPC | **26 ms** | Le budget < 300 ms est atteignable si on maîtrise le live. |
| `status --json` (refresh actif) | 1,97 s, **pas de sync déclenché** | `refresh` absent du JSON : `last_sync` était encore frais (< 10 min). Le mode auto écrira quand il sera périmé — à encadrer en A0. |
| Base | 434 jeux : **65 downloading / 369 linked** | Les 65 cibles sont `wanted=1` mais pas encore téléchargées (bascule NTSC), pas des cibles cassées. |
| `last_scan` / `last_sync` | 20:37 / 20:47 UTC | À convertir en local dans l'UI. |
| Timer | **non installé** | `systemctl --user list-timers` ne montre aucun timer RetroBridge. |
| RPC `files` | 15,0 Mo / 13,2 s | Le scan complet est dominé par le payload. |
| RPC `files + fileStats` | 18,9 Mo / 4–30 s | Utilisé par `scan`. |
| `scan` | `DELETE` + `INSERT` 83 022 lignes | À rendre incrémental (lot D). |

Sauvegarde de référence (post-baseline, pas un backup applicatif) : `/tmp/opencode/retrobridge-baseline-20261121.db`.

**Conclusion de baseline :** le problème n'est pas SQLite (**26 ms**), c'est le **RPC synchrone** dans `status` (~1,6 s pour `session-stats`, handshake 409 inclus). Le budget < 300 ms impose soit un cache de session/stats, soit `--no-live`. L'auto-sync doit rester borné et explicite (A0).

---

## 2. Livrables du jour 1

- [x] `PLAN_IMPLEMENTATION.md` (ce fichier)
- [x] Décisions D1–D8 ci-dessus
- [x] Baseline chiffrée + sauvegarde DB
- [x] `tests/finalize_harness.py` : DB de test, faux Transmission, config temporaire
- [x] `tests/test_finalize_day1.py` : 5 tests (fixture, contrat faux client, status sans écriture, budget < 1 s, vivier lié vs en file)
- [x] `A0` (code) : auto-sync safe + `--no-refresh` — **fait jour 2**
- [x] `A1` (code) : `freshness` + affichage local — **fait jour 2**

---

## 3. Lots et tickets

### Lot A — Fraîcheur & réconciliation ciblée (P0)

| Ticket | Contenu | Fichiers | Tests |
|---|---|---|---|
| **A0** | `status` : auto-sync **safe** (défaut) = `skip_scan=True`, `apply=False`, `reconcile_transmission=False`, `execute_pending=False` ; spinner en TTY, durée affichée, `--no-refresh` pour lecture stricte. | `cli/user.py`, `services/sync.py` | CLI : flags safe, `--no-refresh`, non-suppression |
| **A1** | `services/freshness.py` : `last_scan_at`, `last_sync_at`, `age_seconds`, `stale`, conversion UTC→local ; exposé dans `status` et affiché. | `services/freshness.py`, `services/status.py`, `cli/user.py` | horloge gelée, fuseau, JSON |
| **A2** | ✅ Réconciliation ciblée des `downloading` : `target_exists`, `partial_exists`, `wanted`, `bytes_completed`, `progress_pct`, `download_status` (`queued`, `downloading`, `complete`, `broken`, `orphan_partial`, `present_unwanted`, `unknown`) et `issue`. | `services/status.py` | DB fixture + faux client |
| **A3** | ✅ `--no-live` coupe tout RPC ; `status --json` expose `active_downloads_total`, `download_status_counts`, `target_issues`, `transmission.live`. | `services/status.py`, `cli/user.py` | contrat JSON, budget |

**DoD :** ✅ `get_system_status(live=False)` < 100 ms (mesuré 45 ms) ; `status --no-live` ~0,3–0,4 s (démarrage Python/Typer inclus) ; `status` par défaut ne fait aucun `torrent-set` ; une cible `wanted=1` en file n'est pas signalée, une cible `wanted=0` absente l'est.

### Lot B — Setup : timer, langues, complétion (P0/P2)

| Ticket | Contenu |
|---|---|
| **B1** | ✅ `config.py` : `preferred_languages: list[str]`, compat `pal_language`, helpers `language_code` / `normalize_languages`. |
| **B2** | ✅ `scoring.py` : préférences multi-langues (bonus et pénalités), `score_rom(..., preferred_languages=...)`, `get_lang_label(..., languages)`. |
| **B3** | ✅ `setup` : étape `automation` systemd `--user` (`--no-timer`, `--uninstall-timer`, `--step 4`) ; fallback cron non implémenté. |
| **B4** | ✅ Génération dynamique des units (`ExecStart` = binaire résolu, `WorkingDirectory`, `PATH`, `EnvironmentFile`). |
| **B5** | ✅ `sync` verrouillé : sortie **succès** (`skipped: locked`) pour ne pas alerter systemd. |
| **B6** | ✅ `setup --check` / `status` : état du timer + prochaine exécution. |
| **B7** | ✅ Fin de setup : rappel de `retrobridge --install-completion`. |

**DoD :** ✅ installé et testé sur la machine (`systemctl --user start` → exit 0, 369 liens vérifiés), désinstallation implémentée, aucun root. Timer actif, prochaine exécution affichée.

### Lot C — UX `status` / `list` (P0)

| Ticket | Contenu |
|---|---|
| **C1** | ✅ `cli/formatting.py` : `basename`, `truncate`, `format_size`, `format_size_compact`, `progress_bar`, `progress_text`. |
| **C2** | ✅ `status` : 3 tableaux pleine largeur ; barres emojis 🟩🟨⬜ ; colonne État dédiée ; colonne Taille dans les deux tableaux ; pourcentage aligné à droite ; barre de complétude par système ; basename, `--full-path`, `--no-live`. |
| **C3** | ✅ `list` : une ligne/jeu, séparateurs par console, colonnes `Titre / État / Score / Langue / ROM`, mêmes emojis d'état que `status`, langue sans drapeau, `--wide`, `--sort`. |
| **C4** | ✅ Tests Rich (`Console(record=True, width=120)`) ; `--json` inchangé. |
| **C5** | ✅ Sémantique « Téléchargés » : `acquired = linked + downloaded`, avec « dont liés » ; JSON expose `acquired` et `downloaded_pending`. |

**DoD :** ✅ tient en 120 colonnes, une ligne par jeu en mode normal, `--wide` restaure les valeurs complètes, pas de régression JSON.

### Lot D — `scan` (P0 étapes / P1 incrémental)

| Ticket | Contenu |
|---|---|
| **D1** | Spinner + étapes (`Connexion → Lecture → Indexation → Écriture`) + durée + compteurs. |
| **D2** | `--quiet` / `--json` (événements). |
| **D3** | (P1) Phase légère (`fileCount`, `totalSize`, `name`) + signature par torrent ; ne récupérer `files` que si changement. |
| **D4** | (P1) `UPSERT` + purge des disparus au lieu du `DELETE` global. |

**DoD :** plus de 30 s « muettes » ; benchmark avant/après consigné.

### Lot E — `find` (P0)

| Ticket | Contenu |
|---|---|
| **E1** | ✅ Service `find.py` : TTL + scan optionnel, `find_existing_game` (titre normalisé). |
| **E2** | ✅ Détection « déjà présent », `--update` (réutilise le titre existant). |
| **E3** | ✅ CLI `find <query> -s <system>` : ajout par défaut, `--no-add`, `--yes`, `--dry-run`, `--limit`, `--full-path`, `--json`. Une réponse non numérique au prompt annule. |
| **E4** | ✅ Alias cachés `search` / `add` (masqués de `--help`). |
| **E5** | ✅ Sélection Transmission si `wanted=0`, désélection de l'ancienne ROM en cas d'update. |

**DoD :** ✅ deux exécutions = un seul jeu ; ambiguïté = erreur explicite ; cache périmé = scan avec spinner.

### Lot F — Multi-disques (P1)

| Ticket | Contenu |
|---|---|
| **F1** | Schéma `game_files` (une ligne par disque) + backfill des jeux existants. |
| **F2** | `find`/`add` : détection des disques, sélection de tous, un seul jeu. |
| **F3** | `sync` : liaison de tous les disques, état agrégé, `.m3u`. |
| **F4** | `export` : un export par disque (`export_files`) + `.m3u` cible. |
| **F5** | ✅ `status` : taille et progression agrégées par jeu. |

**DoD :** ✅ un jeu multi-disques s'ajoute en une commande ; tous les disques sont liés et exportés ; les `.m3u` contiennent tous les disques.

### Lot G — Confort (P2)

FTS/index de recherche, `watch`, notifications, `--quiet` généralisé, i18n.

---

## 4. Ordre, dépendances, durée indicative

| Ordre | Lot | Dépend de | Durée |
|---|---|---|---|
| 1 | **A0 → A1 → A2 → A3** | décisions | 1,5 j |
| 2 | **B1 → B7** | A1 (affichage âge) | 1,5 j |
| 3 | **C1 → C4** | A2 | 1,5 j |
| 4 | **E1 → E5** | A1 | 2 j |
| 5 | **D1 → D4** | A1 | 1,5 j |
| 6 | **F** | E, décision schéma | 2–3 j |
| 7 | **G** | tout | optionnel |

**Chemin critique :** A → B → E. C peut se faire en parallèle de B.

---

## 5. Garde-fous

- Un PR par lot ; pas de mélange refactor/feature.
- Tests sans réseau : faux client Transmission, DB temporaire, `systemctl` mocké.
- Budget : `get_system_status(live=False)` < 100 ms ; `status --no-live` < 400 ms processus ; `find` < 1 s cache chaud.
- Compatibilité : alias `search`/`add`, `pal_language` accepté, `--json` non cassé.
- Rollback : `--no-refresh`, `--no-live`, `refresh.mode=off`, `setup --uninstall-timer`.

## 6. Risques

| Risque | Parade |
|---|---|
| Refactor encore en cours (`1a9f17b`, `e0e5abe`, `466162d`) | rebase fréquent, PR courts, A0 en premier |
| `status` RPC lent (2 s constatés) | `--no-live` par défaut ? cache session-id, timeout court, stats live optionnelles |
| 65 « downloading » normaux confondus avec des cibles cassées | critère `wanted=0` + fichier absent = cassé ; `wanted=1` = file d'attente |
| systemd indisponible / session non persistante | fallback cron, message `enable-linger` |
| Rich hors TTY | `console.is_terminal`, tests golden |
| Migration multi-disques | lot F séparé, pas avant la v2 |

---

## 7. Jour 2 — terminé (2026-11-21)

- [x] **A0** : auto-sync safe.
  - `run_sync()` accepte `execute_pending` et `reconcile_transmission`.
  - `status` appelle `skip_scan=True`, `apply=False`, `execute_pending=False`, `reconcile_transmission=False`.
  - Spinner TTY, durée en ms, message explicite, `--no-refresh` pour lecture stricte.
- [x] **A1** : `services/freshness.py` + affichage `Snapshot : scan il y a X · sync il y a Y` + `freshness` dans `--json`.
- [x] Tests : **64 passed** (49 initiaux + 5 jour 1 + 5 A0 + 5 fraîcheur).
- [x] Vérification réelle : `status --no-refresh` affiche la fraîcheur ; run chaud à 0,31 s.
- [x] Garde-fou testé : un sync safe ne supprime ni ROM ni `pending_action`.

**Reste ouvert :** `status` par défaut dépend encore du RPC pour les stats live (0,3–2,3 s selon charge). `--no-live` livre le budget interne ; il reste l'optimisation du démarrage CLI (~0,25 s) si on veut un processus < 300 ms.

---

## 8. Jour 3 — terminé (2026-11-21)

- [x] **A2** : réconciliation ciblée.
  - Champs ajoutés : `target_exists`, `partial_exists`, `wanted`, `in_cache`, `bytes_completed`, `file_length`, `progress_pct`, `download_status`, `issue`, `file_name`.
  - Statuts : `queued` (demandé, rien reçu), `downloading` (`.part` ou octets reçus), `complete`, `broken` (`wanted=0` + absent), `orphan_partial` (`.part` non sélectionné), `present_unwanted`, `unknown`.
  - `active_downloads_total`, `download_status_counts`, `target_issues` exposés.
- [x] **A3** : `--no-live` + contrat JSON.
  - `get_system_status(..., live=False)` n'instancie jamais `TransmissionClient`.
  - `transmission.live = false`, `reachable = false` ; l'UI affiche « live désactivé » et non « Injoignable ».
  - `--json` inclut tous les champs de réconciliation.
- [x] Tests : **70 passed**.
- [x] Vérification réelle : 65 `downloading` → **37 downloading + 28 queued, 0 cible cassée**. Les 3 PS2 de l'incident seraient désormais classés `broken`.
- [x] Budget : `get_system_status(live=False)` = **45 ms** ; `status --no-live` = **0,30–0,37 s** (démarrage Python/Typer inclus).

**Note :** le seuil < 300 ms au niveau processus est limite à cause des ~0,25 s de démarrage Python/imports. Le budget interne est tenu ; une optimisation (imports paresseux dans la CLI) pourra être faite plus tard si nécessaire.

---

## 9. Jour 4 — terminé (2026-11-21)

- [x] **B1** : `preferred_languages` (défaut `["fr","en"]`) + conversion des anciens `pal_language`.
- [x] **B2** : scoring multi-langues ; les tests historiques FR passent sans modification.
- [x] **B3/B4** : `setup --step 4` installe des units générées dynamiquement (`ExecStart` = binaire courant, ici `.venv/bin/retrobridge sync`).
- [x] **B5** : `sync` déjà verrouillé → `{"skipped": true, "reason": "locked"}`, exit 0.
- [x] **B6** : `status` affiche `Sync auto : timer actif — prochaine : …` ; `setup --check` ajoute une ligne Timer.
- [x] **B7** : le setup rappelle `retrobridge --install-completion`.
- [x] Tests : **77 passed**.
- [x] Vérification réelle : timer installé, `systemctl --user start retrobridge-sync.service` → exit 0, 369 liens vérifiés, prochaine exécution planifiée.

**État du timer :** actif, `OnCalendar=*:0/10`, `RandomizedDelaySec=30s`, `Persistent=true`.

---

## 10. Hotfix scoring — terminé (2026-11-21, avant lot C)

Déclencheur : 7 téléchargements « Japan » sur 65. Diagnostic : 3 avaient une version US
mal notée, 4 étaient des cas réels (titre/plateforme), et **1 217 fichiers `(USA, ...)`
étaient notés 200**.

- [x] **Parseur de région centralisé** (`domain/regions.py`) :
  - `(USA, Europe)`, `(USA, Australia)`, `(USA, Canada)` → US ;
  - `(AU-US)`, `(JP-US)`, `(EU-US)`, `(US-EU)` → US ;
  - `(World)` → WORLD, `(Japan)` → JP, `(Europe, Australia)` → PAL ;
  - `(En,Fr,De)` / `(en-fr)` restent des listes de langues, jamais des régions.
- [x] `score_rom()` utilise ce parseur.
- [x] **Portables** (`GB`, `GBC`, `GBA`, `NDS`, `PSP`, `GAMEGEAR` + Lynx/NGP/Wonderswan/Pokemon Mini) :
  - plus de bonus/pénalité PAL/NTSC ;
  - base 1000 + 100 si la langue préférée est présente ;
  - pénalité si version monolingue non désirée (`(Ja)`, `(Es)`...) ;
- [x] **Ranking `find`/`add`** : tri par `(score, match_bonus, taille)`. Le score est
  prioritaire ; le bonus de titre ne départage que les égalités.
- [x] Tests : **83 passed** (dont parseur, portables, ranking).
- [x] Vérification réelle :
  - `Tales of Phantasia (GBA)` : Europe FR 1100 > Japan 1000 = USA 1000 ;
  - `Pokemon Pinball (GBC)` : `(AU-US)` reconnu US ;

**Reste connu (hors hotfix) :**
- `Marvel vs. Capcom 2` : le fichier US s'appelle `Marvel vs. Capcom 2 (USA)` sans le sous-titre → non matché ;
- `Daytona USA 2001` : le titre US est `Daytona USA` ;
- doublons de titres accentués (`Pokemon` / `Pokémon`) ;
- les 3 portables encore en « Japan » seront remis sur la meilleure langue via `find --update`.

---

## 11. Jour 5 — terminé (2026-11-21)

- [x] **C1** : helpers d'affichage (basename, troncature, tailles, barre).
- [x] **C2** : `status` avec progression, basename et `--full-path`.
- [x] **C3** : `list` groupé par console, une ligne par jeu, colonnes `Titre / État / Score / Langue / ROM`, `--wide`, `--sort`.
- [x] **C4** : tests Rich + vérification `--json`.
- [x] **C5** : sémantique des compteurs.
  - Panneau : `Téléchargés: 369 (dont liés: 369)`.
  - Tableau par système : `Téléchargés` (acquis) + `dont liés`.
  - `--json` : `acquired` et `downloaded_pending` ajoutés, `downloaded` reste l'état exclusif.
- [x] Tests : **90 passed**.
- [x] Vérification réelle :
  - `status` : tableaux **pleine largeur**, barre de complétude par système ;
  - barres emojis : `🟩` plein, `🟨` partiel, `⬜` vide ; `2%` affiche un carré partiel, pas une barre pleine ;
  - `État` dans une colonne dédiée avec des emojis de largeur stable (`📥`, `⏳`, `✅`, `❌`, `🟡`) ;
  - colonne `Taille` séparée (`14Mo / 494Mo`) ;
  - `Progression` alignée à droite : barre puis `2%` ;
  - `list --system GBA` : séparateur GBA, une ligne/jeu, colonnes `Titre / État / Score / Langue / ROM`, état emoji identique à `status`, langue sans drapeau ; `--wide` restaure tout.

**Décision UI :** « Téléchargés » est désormais cumulatif (liés + en attente de lien), avec le détail « dont liés ». Les états restent exclusifs en base.

---

## 12. Jour 6 — terminé (2026-11-21)

- [x] **E1** : `services/find.py` (`find_existing_game`, `refresh_cache_if_needed`).
- [x] **E2** : détection accents/titre normalisé, `--update`.
- [x] **E3** : `find <query> -s <system>` : ajout interactif par défaut, `q`/`--no-add` annule, `--yes`, `--dry-run`, `--limit`, `--full-path`, `--json`.
- [x] **E4** : `search` et `add` masqués, toujours fonctionnels pour les scripts.
- [x] **E5** : sélection du fichier (`wanted`), démarrage immédiat du torrent, rapport explicite si le RPC échoue (`sync` conseillé), désélection de l'ancien en cas de remplacement.
- [x] Cohérence CLI : `-s/--system` partout (y compris `find`/`search`/`add`) ; `setup --step` n'utilise plus `-s` ; `--db` remplace `--target` dans `tools db`.
- [x] Tests : **98 passed**.
- [x] Vérification réelle :
  - `find "Tales of Phantasia" -s gba` → `Déjà présent` ;
  - `find ... --update` → 3 candidats, Europe FR 1100 en tête ;
  - `--add --yes` exige un match exact et ne crée pas de doublon.

---

## 13. Jour 7 — terminé (2026-11-21)

- [x] **F1** : table `game_files` + backfill automatique (437 jeux existants migrés).
- [x] **F2** : `find`/`add` détectent et sélectionnent tous les disques du même jeu/région.
- [x] **F3** : `sync` lie tous les disques, calcule l'état agrégé et génère les `.m3u`.
- [x] **F4** : `export` écrit un fichier par disque dans `export_files` et génère les `.m3u` cible.
- [x] **F5** : `status` agrège taille/progression par jeu et affiche `(N disques)`.
- [x] Tests : **104 passed** (upgrade et tools multidisc retirés).
- [x] Vérification : migration réelle → 437 `game_files` pour 437 jeux, aucune erreur.

---

## 14. Jour 8 — terminé (2026-11-21)

### Lot D — scan incrémental ✅
- Passe légère (`file-count`, `totalSize`) + table `torrent_meta`.
- Torrents inchangés : seuls `fileStats` sont relus.
- Torrents modifiés : `files` rechargés, cache remplacé pour ce torrent uniquement.
- Étapes affichées via `console.status` + durée + compteurs.
- `--quiet` / `--json` sur `tools scan`.
- Gain mesuré : ~8,2 s → ~4,5 s sur 83 022 fichiers.

### Lot G — confort (partiel) ✅
- Colonnes normalisées `file_name_norm` / `file_name_compact` dans `torrent_cache`.
- `find` pré-filtre désormais en SQL au lieu de charger les 83 022 lignes en Python.
- `--quiet` sur `sync` et `export`.
- Restent optionnels : `watch` TUI, notifications desktop, i18n du CLI.

- [x] Tests : **105 passed**.

---

## 15. Ultime lot — terminé (2026-11-22)

- [x] `status` : taille totale acquise + en cours.
- [x] `tools verify` sans argument : torrents en erreur/corrompus.
- [x] `setup` : profil de destination (`esde`/`knulli`) + point de montage.
- [x] `tools clean` : dry-run par défaut, `--apply` pour supprimer.
- [x] BIOS : commande `tools bios` qui s'appuie sur RetroBios (catalogue interne supprimé).
- [x] `export` : fichiers présents uniquement sur la carte listés en information.
- [x] Lot G partiel : pré-filtre SQL de recherche + `--quiet` sur `sync`/`export`/`scan`.
- [x] Tests : **105 passed**.

---

## 16. Commandes de baseline (reproductibles)

```bash
# tests
.venv/bin/python -m pytest -q

# status sans refresh (lecture seule)
TIMEFORMAT='%R s'; time .venv/bin/retrobridge status --json --no-refresh >/dev/null

# status avec refresh (peut écrire en base si périmé)
TIMEFORMAT='%R s'; time .venv/bin/retrobridge status --json >/dev/null

# sauvegarde de la base
cp ~/.local/share/retrobridge/retrobridge.db /tmp/opencode/retrobridge-baseline-$(date +%Y%m%d).db
```
