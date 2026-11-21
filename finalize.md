# finalize.md — Dernière ligne droite

> Document de travail : décisions à valider, rien n'est encore implémenté.
> Objectif de cette phase : passer d'un outil « qui marche » à un outil **fiable au quotidien**.
> Dernière mise à jour : 2026-11-21.
>
> **État d'avancement :** lots A (fraîcheur/`status`) et B (setup, timer, langues) implémentés et testés. Suivi détaillé dans `PLAN_IMPLEMENTATION.md`.

---

## 0. Constat de départ

Trois couches de données coexistent aujourd'hui et sont mélangées à l'écran :

| Couche | Contenu | Écrite par | Fraîcheur |
|---|---|---|---|
| `torrent_cache` | miroir brut de Transmission (15 torrents, **83 022 fichiers**) | `tools scan` | TTL `scan_ttl` (1 h) |
| `games` | 434 jeux, ROM choisie, état `downloading`/`linked`, score | matching + `sync` | au sync |
| Transmission live | débits, `wanted`, `bytesCompleted` | RPC | temps réel |

Conséquences observées :

- `status` affiche « 4 en téléchargement » alors que Transmission a 15 torrents actifs (dont 6 en téléchargement réel) : ce sont deux métriques différentes.
- Des cibles supprimées restent affichées « en téléchargement » alors que `wanted=0` et que le fichier n'existe plus.
- `torrent_cache.is_wanted` peut diverger du RPC (441 en cache vs 401 en live après dédoublonnage).
- Les timestamps `meta` sont stockés en UTC (`last_sync = 18:15` UTC = `19:15` local en hiver) : toute future UI doit convertir explicitement, sinon on croira le cache plus vieux/plus frais qu'il ne l'est.
- `retrobridge-sync.timer` existe dans `systemd/` mais n'est pas installé : rien ne rafraîchit automatiquement.

**Ce document acte une règle simple : un affichage ne doit jamais déclencher un rafraîchissement lourd.**

---

## 1. Principe directeur : snapshot + live, jamais de refresh complet implicite

Décisions proposées (à valider) :

- [x] `list` est **strictement lecture seule**. `find` a pour comportement principal l'**ajout après sélection** ; `--no-add` (ou `--json` sans `--yes`) le rend lecture seule. `status` est en lecture seule tant que `last_sync` est frais ; au-delà de `status_ttl`, il exécute un **sync safe** (`--no-refresh` force la lecture stricte).
- [x] Le sync safe de `status` ne scanne jamais, n'exécute aucune action destructive (`pending_actions`) et n'écrit pas dans Transmission (`apply=False`, `reconcile_transmission=False`).
- [ ] On sépare visuellement **« Transmission (live) »** et **« Collection (snapshot du JJ/MM HH:MM) »**.
- [ ] On affiche l'âge de `last_scan` / `last_sync` (converti en heure locale) et un badge `[périmé]` au-delà du TTL.
- [ ] Le rafraîchissement lourd (scan) est déclenché par : le timer, `sync`, ou un `--refresh` explicite. `status` ne déclenche jamais de scan.
- [ ] `status` peut faire une **réconciliation ciblée** (quelques `stat()` + RPC minimal) pour les seuls jeux `downloading`, pas un scan complet.
- [ ] Aucun pourcentage inventé : si l'info n'est pas disponible, on affiche un spinner ou une étape, pas une barre fausse.
- [x] **Consoles portables** (GB/GBC/GBA/NDS/PSP/Game Gear) : PAL/NTSC n'existe pas. La région est ignorée dans le score ; la langue et la révision départagent.
- [x] **Classement `find`/`add`** : le score est le critère principal ; le bonus de titre ne sert qu'à départager des scores égaux.
- [x] **Compteurs** : « Téléchargés » = acquis (liés + en attente de lien), avec le détail « dont liés » ; les états restent exclusifs en base.

Justification chiffrée (mesurée sur la machine cible) :

| Opération | Volume | Durée |
|---|---:|---:|
| `session-get` (ce que fait `status`) | ~0 Mo | **48 ms** |
| `torrent-get` `files` | 15,0 Mo | **13,2 s** |
| `torrent-get` `files + fileStats` (scan) | 18,9 Mo | 4 s à chaud, jusqu'à 30 s |
| `DELETE` + `INSERT` de 83 022 lignes | base 23 Mo | coût disque + verrou SQLite |

Un refresh complet à chaque `status` (intégré au prompt fish) coûterait donc 15-19 Mo et plusieurs secondes **à chaque commande**. Et si on le branchait sur le client RPC de `status`, il dépasserait le timeout de 3 s volontairement fixé dans `status.py`.

### 1.1 Réconciliation ciblée (proposition)

Pour les N jeux `state='downloading'` (aujourd'hui 1 à 4) :

1. `os.path.exists(matched_file_path)` + `os.stat()` ;
2. Lire `wanted` / `bytesCompleted` à `matched_torrent_id` + `matched_file_index` ;
3. Marquer la ligne « cible absente », « désélectionnée » ou « en cours » ;
4. Ne jamais réécrire la ligne `games` : c'est un état calculé à l'affichage.

Coût attendu : < 100 ms. C'est le seul refresh acceptable dans `status`.

---

## 2. Tes propositions — verdicts et contre-propositions

### 2.1 Setup : demander la langue

**Verdict : oui, mais pas une seule question.**

Il y a trois notions à ne pas confondre :

| Notion | Exemple | Où ça sert |
|---|---|---|
| Langue de l'interface | français / anglais | i18n du CLI (chantier séparé, pas demandé aujourd'hui) |
| Préférence de région | NTSC 60 Hz / PAL 50 Hz | `scoring.region_preference` |
| Langue des ROMs | FR > EN > multi | `scoring.pal_language` actuellement |

Proposition :

- [ ] Étape `scoring` du setup : demander la région, puis une **liste ordonnée de langues** (`fr, en`) avec multi-select, et un fallback.
- [ ] Remplacer `pal_language: str` par `preferred_languages: list[str]` (garder `pal_language` en alias rétro-compatible).
- [ ] Option par système (ex. RPG japonais uniquement) : plus tard, pas dans le setup initial.
- [ ] Afficher les préférences retenues dans `setup --check` et `status`.
- [ ] Proposer à la fin du setup d'installer le timer de sync (voir 2.2).

**Challenge :** demander « la langue » seule est trop grossier. Un utilisateur FR peut vouloir « FR si dispo, sinon NTSC 60 Hz même en anglais », et surtout **ne pas** préférer un PAL 50 Hz juste parce qu'il est en français. La politique doit être explicite : `60 Hz d'abord`, `langue ensuite`, `PAL seulement si le jeu n'existe pas en NTSC`.

### 2.2 « Le scan est une option technique » → l'intégrer au parcours

**Verdict : oui sur le principe, non sur l'automatisme complet.**

Le besoin utilisateur n'est pas « scanner », c'est « avoir des données fraîches ». Et un scan complet ne rafraîchit même pas `games` : il ne fait que le miroir Transmission. Le vrai Rafraîchir, c'est `sync` (scan si TTL expiré + matching + états).

Proposition :

- [ ] `tools scan` reste une commande technique (documentée comme « miroir Transmission »), pas dans le parcours principal.
- [ ] `sync` devient le verbe utilisateur : scan si TTL expiré, puis matching/états.
- [ ] `find`, `add` et `status --refresh` vérifient le TTL avant de lire le cache ; si périmé, scan avec spinner (sauf `--no-refresh`).
- [ ] `status` sans flag **ne scanne jamais** : il affiche la fraîcheur et peut déclencher un **sync safe** si `last_sync` dépasse `status_ttl` (pas de scan, pas d'action destructive, pas d'écriture Transmission), avec spinner et `--no-refresh` pour la lecture stricte.
- [ ] Config : `ttl.scan_ttl` (défaut 3600) + éventuellement `refresh.mode = off | ttl | always` (déconseiller `always`).

#### Timer dans `setup` — proposition détaillée

- [ ] Nouvelle étape `automation` (ou question en fin de setup) : « Synchroniser automatiquement toutes les 10 min ? [O/n] ».
- [ ] Détection de `systemctl --user` ; sinon proposer cron (`*/10 * * * * retrobridge sync`) ou `--no-timer`.
- [ ] **Ne pas réutiliser le chemin codé en dur** du service actuel (`<projet>/.venv/bin/retrobridge`) : générer `ExecStart` depuis le binaire réellement installé (`shutil.which("retrobridge")`, ou `sys.executable -m retrobridge`), plus `WorkingDirectory` et `Environment=PATH=...` pour que `chdman`/`dolphin-tool` soient trouvables.
- [ ] Conserver `EnvironmentFile=-%h/.config/retrobridge/rpc.env` (mot de passe RPC optionnel).
- [ ] Installer dans `~/.config/systemd/user/`, puis `daemon-reload` + `enable --now retrobridge-sync.timer` (aucun root).
- [ ] Vérifier ensuite avec `systemctl --user list-timers` et afficher « prochaine synchro : HH:MM ».
- [ ] `setup --check` affiche une ligne « Timer : actif (prochaine exécution 19:40) » ou « inactif ».
- [ ] `status` affiche aussi l'état du timer (actif/inactif, dernière et prochaine exécution).
- [ ] `setup --uninstall-timer` (ou `retrobridge tools timer disable`) : `disable --now`, suppression des units, `daemon-reload`.
- [ ] Si `sync` est déjà verrouillé par une autre instance : sortir en **succès** avec un message, pas en échec systemd (sinon le timer génère des alertes inutiles). Ajouter `RandomizedDelaySec=30s` et `TimeoutStartSec` raisonnable.
- [ ] Si la session utilisateur n'est pas permanente : proposer `loginctl enable-linger` (à expliquer, ne pas forcer).
- [ ] Journal : `journalctl --user -u retrobridge-sync` documenté dans le README.

**Challenge :** un scan automatique dans `status` est le piège classique : la commande la plus fréquente devient la plus lente et la plus écrivante. Le TTL + timer donnent 95 % du bénéfice sans bloquer le prompt.

### 2.3 Scan long : barre de progression

**Verdict : oui, mais pas de fausse barre.**

Le scan actuel est **un seul appel RPC** (`torrent-get` avec `files` + `fileStats`), puis un `DELETE`/`INSERT` massif. Il n'y a pas de flux d'octets à afficher : une barre de pourcentage serait mensongère.

Options :

| Option | Progrès affiché | Coût | Recommandation |
|---|---|---|---|
| A. Étapes + spinner + chrono | « Connexion → Lecture Transmission (15 torrents) → Indexation (83 022 fichiers) → Écriture base » | nul | **à faire maintenant** |
| B. Un appel par torrent | « Torrent 3/15 » réel | 15 allers-retours, à benchmarker | plus tard si A ne suffit pas |
| C. Scan incrémental | « 2 torrents modifiés / 15 » | refonte `scan` | **le vrai gain** |

Proposition :

- [ ] Afficher les étapes et le nombre de torrents/fichiers, avec durée écoulée.
- [ ] Remplacer `DELETE FROM torrent_cache` + réinsertion complète par `UPSERT` + suppression des lignes disparues (scan incrémental), et ne réécrire que si nécessaire.
- [ ] `--quiet` et `--json` (événements) pour les scripts.
- [ ] Message final : `Scan terminé : 15 torrents, 83 022 fichiers en 4,2 s`.

**Challenge :** « progressbar » sur un RPC monolithique = soit faux, soit une refonte. Commencer par des étapes honnêtes, puis viser l'incrémental (C) qui accélère réellement.

### 2.4 `status` : barres de progression + nom de ROM simplifié

**Verdict : oui, avec une colonne dédiée plutôt qu'une surimpression.**

- [x] Colonne `Progression` : barre emojis (`🟩` plein, `🟨` partiel, `⬜` vide) + `% (octets / total)` sur la 2ᵉ ligne ; colonne `État` séparée avec emojis.
- [x] `status` utilise 3 tableaux pleine largeur ; la complétude par système a aussi sa barre.
- [ ] Colonne `ROM` : **basename uniquement** (`Ico (USA).zip`), avec `--full-path`/`-v` pour le chemin complet.
- [ ] Le titre du jeu reste lisible : ne pas superposer la barre **sur** le titre (troncature, daltonisme, copier-coller). Deux lignes dans la cellule si nécessaire : titre puis barre.
- [ ] Rafraîchissement live 1×/s **uniquement en TTY** (`console.is_terminal`), sinon valeur du snapshot avec son âge.
- [ ] En mode `--json`, pas de barre : `{percent, bytes_completed, bytes_total, wanted, exists}`.

Données disponibles : `bytesCompleted` et `length` par fichier via Transmission ; le débit et l'ETA par **fichier** ne le sont pas (seulement par torrent). Pour un ETA par jeu, mesurer le delta de `bytesCompleted` entre deux rafraîchissements — à ne faire que si le live est activé.

**Challenge :** `matched_file_name` contient parfois un chemin complet (lignes historiques) et parfois un basename : normaliser à l'écriture (`Path(...).name`) et à l'affichage.

### 2.5 `list` : une ligne par jeu, séparateurs par console

**Verdict : oui, mais il faut arbitrer « une ligne » vs « information complète ».**

- [x] Une ligne = titre, état (emoji identique à `status`), score, langue (sans drapeau), ROM (basename tronqué avec `…`). Système en en-tête de groupe.
- [ ] Séparateur horizontal par console : `console.rule(system)` entre groupes, ou `table.add_section()` si l'on reste dans une seule table.
- [ ] Largeur adaptative : `overflow="ellipsis"`, `no_wrap` sur les colonnes courtes, ratios ; tenir en 100-120 colonnes.
- [ ] `--wide` (ou `--full`) pour afficher titre/ROM complets, `--json` inchangé.
- [ ] Regrouper les multi-disques en une ligne (`Shenmue  [3 disques]`) avec le détail en `--wide`.
- [ ] Trier par système puis titre (déjà le cas) ; ajouter `--sort title|state|score`.

**Challenge :** un nom de ROM No-Intro/Redump dépasse souvent 100 caractères. « Tenir sur une ligne » implique de tronquer : il faut donc un mode `--wide` pour ne pas perdre l'info, et ne jamais tronquer le titre du jeu (seulement le fichier).

### 2.6 Fusionner `search` et `add`

**Verdict : oui, mais en gardant le contrôle et l'idempotence.**

Proposition : une commande `find` (alias `search`) :

```bash
retrobridge find "Gran Turismo 4" -s ps2            # recherche + ajout (q = annuler)
retrobridge find "Gran Turismo 4" -s ps2 --no-add   # recherche seule
retrobridge find "Gran Turismo 4" -s ps2 --yes      # meilleur candidat exact
retrobridge find "Gran Turismo 4" -s ps2 --yes --dry-run
```

- [x] Vérifier le TTL du cache ; si périmé, scan avec spinner (sauf `--no-refresh`).
- [ ] Afficher le top N : score, région/langue, taille, **état Transmission** (`wanted`, `0 %`, complet).
- [x] Détecter « déjà dans la collection » (titre normalisé + système) et afficher : `déjà présent (linked) — ROM : Ico (USA).zip`. `--update` pour changer explicitement.
- [x] `find` sans `--yes` : confirmation interactive ; une réponse non numérique (`q`, `n`, `non`...) annule l'ajout ; `--no-add` force la recherche seule.
- [x] `--yes` : prend le meilleur candidat **uniquement si le match est non ambigu** (score de titre exact) ; sinon erreur explicite.
- [x] Si le fichier est `wanted=0` : `add` le sélectionne et démarre le torrent.
- [x] Idempotence : relancer la même commande ne crée pas de doublon, ne change rien, code retour 0.
- [x] Garder `add`/`search` comme alias cachés pour les scripts existants.
- [x] En `--json`, `find` est read-only sauf `--yes` (pas de prompt interactif).

**Challenge :** fusionner ne veut pas dire « ajouter automatiquement ce qui ressemble ». Une recherche floue + `--yes` est le meilleur moyen d'ajouter une démo, un hack ou le mauvais disque. Le flux doit rester : recherche → candidat → confirmation (ou `--yes` seulement si match exact).

**Dette à traiter au passage :** `search_candidates()` charge les 83 022 lignes en Python à chaque appel. Pour 434 jeux ça passe, mais `find` fréquent mérite soit un pré-filtre SQL, soit une table FTS5 (`file_name_normalized`), soit un index par système.

### 2.7 Complétion shell (`--install-completion` / `--show-completion`)

Ces deux options sont générées automatiquement par Typer/Click : ce n'est pas du code métier.

- `--install-completion` : détecte le shell courant (bash, zsh, fish, PowerShell) et installe la complétion, pour que `retrobridge <Tab>` propose commandes et options.
- `--show-completion` : affiche le script de complétion sur la sortie standard, sans rien installer. Utile pour l'inspecter, l'intégrer à un paquet ou l'installer manuellement.

Proposition dans `setup` :

- [ ] En fin de setup, proposer « Installer la complétion pour fish/zsh/bash ? » (ou afficher la commande exacte).
- [ ] Documenter le mécanisme dans le README (une ligne suffit) : `retrobridge --install-completion`, puis rouvrir le shell.
- [ ] Rappeler qu'il faut relancer l'installation après une mise à jour des commandes.
- [ ] Limite assumée : la complétion porte sur les commandes et options, pas sur les valeurs dynamiques (titres de jeux, systèmes).

---

## 3. Fonctionnalités suggérées (issues de l'usage récent)

### 3.1 Multi-disques dans `add` et `find`

- [ ] `add` doit sélectionner **tous les disques** du jeu, pas seulement le fichier du meilleur score.
- [ ] Afficher `3 disques` dans `find`, `list`, `status`.
- [ ] Le `.m3u` est déjà géré à l'export ; l'`add` doit créer les lignes `games` par disque ou une entité multi-disque cohérente (à trancher, cf. §4).

### 3.2 Progression des longs travaux

- [ ] Barres de progression sur `sync`, `export`, `remove` (pas seulement `scan`).
- [ ] `--quiet` / `--json` partout ; notification desktop optionnelle en fin de traitement.

### 3.3 Fraîcheur & fuseaux

- [ ] Stocker `last_scan`/`last_sync` en UTC (déjà le cas) et **formater en local** à l'affichage (`il y a 12 min`).
- [ ] `status --json` doit exposer `last_scan_at`, `last_sync_at`, `age_seconds`, `stale: true|false`.
- [ ] Test unitaire sur le passage UTC → local.

### 3.4 Divers

- [ ] `retrobridge watch` (TUI live) : plus tard, seulement si `status` reste rapide.
- [ ] `--profile`/`--config` homogènes sur toutes les commandes (certaines ne l'ont pas).
- [ ] Codes retour documentés (0 = OK, 1 = erreur, 2 = périmé/à faire) pour les scripts et le prompt.
- [ ] `retrobridge find --json` stable (contrat de sortie) pour intégration shell.

---

## 4. Questions à trancher (challenge)

1. **Langue** : langue du CLI ou langue des ROMs ? Une seule langue ou liste ordonnée ? Faut-il un réglage par système ?
2. **Fraîcheur acceptable** : 10 min (timer) ou 1 h (TTL actuel) ? Acceptes-tu un scan de 4-30 s dans `find` quand le cache est périmé, ou préfères-tu un refus avec message « lance sync » ?
3. **`status` live** : rafraîchissement 1×/s uniquement en TTY, ou valeur figée au lancement ? (Le live coûte un RPC par seconde.)
4. **`find --add`** : confirmation systématique, ou `--yes` auto si match exact ? Que faire si deux candidats ont le même score ?
5. **Nom des commandes** : `find` + alias `search`, ou garder `search` et cacher `add` ? `sync` reste-t-il le verbe utilisateur ?
6. **Multi-disques** : une ligne `games` par disque (actuel) ou une entité « jeu » avec N fichiers ? Impact sur `status`, `export`, `remove`.
7. **Timer** : systemd user uniquement, ou aussi cron/launchd/Windows ? Installé par défaut au setup ?
8. **Scan** : garder le nom `scan` (technique) ou le renommer `refresh` avec `--full/--fast` ?

---

## 5. Roadmap proposée

### P0 — avant de considérer la v2 « finie »
- [ ] Fraîcheur affichée dans `status` (dates locales + âge + badge périmé).
- [ ] Séparation visuelle live vs snapshot.
- [ ] Timer `retrobridge-sync.timer` installé par `setup`.
- [ ] `status` : basename + barre de progression (colonne dédiée) + réconciliation ciblée.
- [ ] `list` : une ligne par jeu + séparateurs par console + `--wide`.
- [ ] `scan` : étapes/spinner + durée.
- [ ] `find` : TTL + `--add` + détection « déjà présent ».

### P1 — fiabilité
- [ ] Multi-disques dans `add`.
- [ ] Scan incrémental.

### P2 — confort
- [ ] FTS/index de recherche.
- [ ] `watch` TUI.
- [ ] Notifications, `--quiet`, codes retour.
- [ ] Complétion shell proposée en fin de setup + documentée dans le README.
- [ ] i18n du CLI (si vraiment souhaité).

---

## 6. Critères d'acceptation

- [ ] `get_system_status(live=False)` répond en < 100 ms sur 83 022 fichiers ; `status --no-live` reste < 400 ms processus inclus (démarrage Python/Typer).
- [ ] `status` n'écrit **jamais** en base ni dans Transmission (test : mtime DB inchangé, aucun `torrent-set`).
- [ ] `status` affiche l'âge du snapshot et `stale=true` au-delà du TTL.
- [ ] `list` tient en 120 colonnes, une ligne par jeu, et `--json` reste inchangé.
- [ ] `find --add` relancé deux fois ne crée pas de doublon (test d'idempotence).
- [ ] `scan` affiche des étapes réelles et la durée, sans pourcentage inventé.
- [ ] Le timer et le verrou empêchent deux sync simultanés.
- [ ] Un test couvre le passage UTC → heure locale.

---

## 7. Ce qu'on ne fait pas (pour éviter les régressions)

- Pas de scan complet dans `status`, `list` ou le prompt.
- Pas d'ajout silencieux depuis une recherche floue.
- Pas de pourcentage de progression simulé.
- Pas de refresh qui écrit en base depuis une commande de consultation.
- Pas de fusion search/add qui casse `--json` ou les scripts existants.

---

## A la fin

La v2 est « finie » quand :

- [ ] Un nouvel utilisateur fait `setup` (chemins, Transmission, région + langues, timer) et obtient un `status` compréhensible sans lire la doc.
- [ ] `status` ne ment jamais sur la fraîcheur : il dit ce qui est live, ce qui est snapshot, et depuis quand.
- [ ] `find` permet de chercher et d'ajouter sans double action ni doublon.
- [ ] `list` et `status` sont lisibles sur un terminal standard, et exploitables en `--json`.
- [ ] `sync` est le seul verbe qui écrit, et il est protégé par un verrou.


## Réflexions ultérieurs encore jamais prise en compte

### Déjà couvert

- [x] Supprimer la logique de conversion / upgrade PAL → NTSC.
- [x] Corriger l'écart « Liés » / « Téléchargés » : `Téléchargés = acquis (liés + en attente)`.
- [x] Unifier `--system/-s` sur toutes les commandes.

### Ultime lot proposé — « finitions d'exploitation »

Objectif : fermer les derniers écarts d'usage sans refonte majeure.

| # | Sujet | Proposition |
|---|---|---|
| 1 | ✅ **Taille dans `status`** | Ajouter au panneau résumé la taille totale des jeux **en cours** et **acquis** (somme des `game_files`), en plus du nombre. |
| 2 | ✅ **`tools verify` sans argument** | Vérifier automatiquement les torrents signalés en erreur par Transmission (fichier corrompu / `Verify Local Data`), avec `--json` et un récapitulatif. |
| 3 | ✅ **Setup de la destination** | Ajouter une étape « destination » : profil `esde` / `knulli` / autre, chemin de montage et création de la target, au lieu de laisser cela à l'édition manuelle du TOML. |
| 4 | ✅ **`audit` ↔ `clean`** | Clarifier : `audit` = diagnostic strictement lecture seule ; `clean` = action qui consomme les résultats d'audit, en `--dry-run` par défaut. |
| 5 | ✅ **BIOS / RetroBios** | ✅ `tools bios` propose RetroBios s'il est installé, sinon affiche les instructions. L'ancien catalogue `tools sources` a été supprimé. |
| 6 | ✅ **Export SD — extra** | Lister les fichiers présents **uniquement sur la carte** (absents de la source) comme information de réconciliation, jamais comme erreur et sans suppression automatique. |
| 7 | **Finitions lot G** (partiel : SQL + `--quiet`) | `watch` TUI, notification desktop de fin de sync/export, i18n du CLI, fallback cron du timer et `loginctl enable-linger`. |

### Ordre suggéré

1. `status` : taille totale + cohérence live/snapshot.
2. Export : fichiers « destination-only » en information.
3. `tools verify` sans argument.
4. Setup destination + `audit`/`clean`.
5. Wrapper RetroBios.
6. Finitions G (watch, notifications, i18n).
