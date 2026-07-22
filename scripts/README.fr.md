# 🖥️ CLI, shell et tests Hivemind

> CLI scriptable, shell interactif et scripts de test opérationnels pour
> Hivemind `1.3.0`.

🇬🇧 [English version](README.md)

---

## Prérequis

```bash
uv sync --dev
```

Variables d'environnement :

```bash
export MCP_URL=http://localhost:8080    # URL du serveur (via WAF)
export MCP_TOKEN=votre_token_secret     # Token d'authentification
```

---

## Articulation avec `/admin` et `/live`

| Surface          | Expose                                              | Notes                                                                                                                                                |
| ---------------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mcp_cli.py`     | **45 opérations MCP directes**                      | Commandes Click + shell interactif. Ce README est la référence.                                                                                       |
| Web `/admin`     | Workflows sélectionnés via le proxy `POST /api/tool` | Console web authentifiée (cookie HttpOnly). Sections : Dashboard, Spaces, Space Detail, Consolidation, Audit, Access, et Outils opérateur (Backups, Maintenance). |
| Web `/live`      | Visualisation read-only des spaces / notes / bank   | Utilise des endpoints REST dédiés (`/api/spaces`, `/api/live/<id>`, `/api/bank/<id>`), PAS le protocole MCP.                                          |

La fixture figée du serveur MCP est la surface d'outils canonique. La CLI expose
45 opérations directes, dont l'invitation de space et la requête long read-only, mais ne réplique pas
chaque outil MCP additif ni chaque alias de tier. **La parité est
directionnelle, pas bijective** : la console `/admin` sélectionne un
sous-ensemble de la surface pour les workflows opérateur (elle n'expose pas
`backup_download`, `space_export`, ni `admin_bulk_update_tokens`, qui restent
réservés à la CLI/MCP), et la CLI de son côté ne réplique pas chaque outil MCP
additif. `/live` est une UI read-only de confort ; ses capacités sont un
sous-ensemble de `live read`, `bank read`, `bank list` et `space info`.

---

## CLI scriptable (Click)

Chaque opération listée ci-dessous correspond à une commande Click. Aide
complète : `uv run python scripts/mcp_cli.py --help` ou `... <group> --help`.

### System (3 outils)

```bash
uv run python scripts/mcp_cli.py health                              # Santé du service (probes S3 + LLM)
uv run python scripts/mcp_cli.py whoami                              # Identité du token courant
uv run python scripts/mcp_cli.py about                               # Version, capacités du service
```

### Space (10 outils MCP)

```bash
uv run python scripts/mcp_cli.py space list                          # Liste les spaces accessibles
uv run python scripts/mcp_cli.py space create my-proj -d "Desc" --rules-file RULES/live-mem.standard.memory.bank.md  # manage
uv run python scripts/mcp_cli.py space invite my-proj sha256:<64-hex-minuscules>  # manage + accès ; ajout seul
uv run python scripts/mcp_cli.py space info my-proj                  # Détails (counts, owner, dates, queue summary)
uv run python scripts/mcp_cli.py space rules my-proj                 # Rules Memory Bank de ce space
uv run python scripts/mcp_cli.py space summary my-proj               # Synthèse complète (rules + bank + notes counts)
uv run python scripts/mcp_cli.py space update my-proj -d "Nouv desc" # Modifie description / owner
uv run python scripts/mcp_cli.py space update-rules my-proj -f rules.md  # Remplace les rules (manage)
uv run python scripts/mcp_cli.py space export my-proj                # Export tar.gz
uv run python scripts/mcp_cli.py space delete my-proj --confirm      # Irréversible (manage)
```

Avant `space delete`, mettre en quiescence tous les writers et jobs de fond du
space. La CLI rend `status: partial` avec les compteurs exacts, clés échouées,
état du marker et action de recovery ; elle ne présente ni ne retente ce
résultat comme un succès. Le `unsafe_recovery` Hivemind avancé reste une
procédure via client MCP, pas un flag CLI.

### Live notes (3 outils)

```bash
uv run python scripts/mcp_cli.py live note my-proj observation "Trouvé X"   # Append une note (agent = token)
uv run python scripts/mcp_cli.py live read my-proj                          # Liste les notes récentes non consolidées
uv run python scripts/mcp_cli.py live search my-proj "mot-clé"              # Recherche full-text dans les notes
```

### Bank (11 outils)

```bash
uv run python scripts/mcp_cli.py bank list my-proj                          # Liste les fichiers bank
uv run python scripts/mcp_cli.py bank read my-proj activeContext.md         # Lit un fichier bank
uv run python scripts/mcp_cli.py bank read-all my-proj                      # Lit toute la bank (démarrage agent)
uv run python scripts/mcp_cli.py bank consolidate my-proj                   # 🧠 Enfile une consolidation de ses propres notes (fire-and-forget)
uv run python scripts/mcp_cli.py bank consolidate my-proj --all-agents      # Scope global explicite (manage/admin)
uv run python scripts/mcp_cli.py bank consolidation-status <job_id>         # Check de statut manuel (NE PAS poller automatiquement)
uv run python scripts/mcp_cli.py bank consolidation-queues                  # Résumé des lanes sur tous les spaces accessibles
uv run python scripts/mcp_cli.py bank stale-spaces                          # 🚨 Spaces ≥5 notes / plus ancienne ≥5 jours
uv run python scripts/mcp_cli.py bank stale-spaces --min-notes 10 --min-age-days 7 --consolidate  # Déclenche un bulk limité au caller
uv run python scripts/mcp_cli.py bank stale-spaces --consolidate --all-agents  # Bulk global explicite (manage/admin)
uv run python scripts/mcp_cli.py bank compact my-proj                       # Dry-run des fichiers surdimensionnés
uv run python scripts/mcp_cli.py bank compact my-proj --apply               # Compaction via LLM (manage)
uv run python scripts/mcp_cli.py bank repair my-proj                        # Dry-run (Unicode / préfixes parasites)
uv run python scripts/mcp_cli.py bank repair my-proj --apply                # Applique les fixes (manage)
uv run python scripts/mcp_cli.py bank write my-proj activeContext.md -f ./ctx.md   # Bypass LLM (manage)
uv run python scripts/mcp_cli.py bank delete my-proj progress.md --confirm  # Supprime fichier + doublons Unicode (manage)
```

### Tier long (5 outils + maintenance du binding local)

Flux de routine sur la stack par défaut : la première `graph push` auto-bind
le space au runtime long embarqué — aucune étape `graph connect`.

```bash
uv run python scripts/mcp_cli.py graph push my-proj                         # Push bank → graphe (la 1re push auto-bind ; delete + re-ingest)
uv run python scripts/mcp_cli.py graph status my-proj                       # État connexion + stats graphe
uv run python scripts/mcp_cli.py graph query my-proj "déploiement" --limit 10  # Requête sémantique read-only
# Override avancé / diagnostic uniquement (moteur externe, ontologie non par défaut) :
uv run python scripts/mcp_cli.py graph connect my-proj <url> <token> <memory_id> [-o ontologie]
uv run python scripts/mcp_cli.py graph disconnect my-proj
# Valide/provisionne le Graph Memory embarqué, puis remplace l'override legacy.
# Le graphe distant reste intact ; aucun document n'est ingéré.
uv run python scripts/mcp_cli.py graph use-local my-proj
```

### Backup (5 outils)

```bash
uv run python scripts/mcp_cli.py backup create my-proj -d "avant migration"
uv run python scripts/mcp_cli.py backup create --all                        # Backup TOUS les spaces accessibles (admin)
uv run python scripts/mcp_cli.py backup list --space-id my-proj             # Liste les backups filtrés par space
uv run python scripts/mcp_cli.py backup download <backup_id>                # Télécharge l'archive
uv run python scripts/mcp_cli.py backup restore <backup_id> --confirm       # Restaure (space ne doit pas exister)
uv run python scripts/mcp_cli.py backup delete <backup_id> --confirm        # Permanent
```

### Délégation de tokens et cycle de vie admin (8 opérations)

```bash
uv run python scripts/mcp_cli.py token create agent-cline -p read,write --email cline@team.io
uv run python scripts/mcp_cli.py token list                                 # Liste les tokens (filtrable)
uv run python scripts/mcp_cli.py token update <hash> --add-spaces my-proj   # Mise à jour delta (add/remove spaces, perms, email)
uv run python scripts/mcp_cli.py token bulk-update --name-contains agent --add-spaces my-proj --confirm   # Mise à jour de masse
uv run python scripts/mcp_cli.py token revoke <hash>                        # Soft-revoke (préserve audit trail)
uv run python scripts/mcp_cli.py token delete <hash>                        # Hard-delete (admin)
uv run python scripts/mcp_cli.py token purge [--all] --confirm              # Purge les tokens revoked (ou --all)
uv run python scripts/mcp_cli.py gc --space-id my-proj --confirm            # Consolider les notes orphelines actuellement éligibles
uv run python scripts/mcp_cli.py gc --space-id my-proj                      # Suppression étape 1 : dry-run ; copier eligible_set_token
uv run python scripts/mcp_cli.py gc --space-id my-proj --confirm --delete-only --expected-eligible-set-token '<token>'  # Suppression étape 2 : destructive
```

`token create` route selon l'identité authentifiée, jamais selon le profil
cible. Un manager persistant non-admin passe par `token_create`, crée seulement
les profils `read`, `read,write` ou `read,write,manage` sans scope initial, puis
invite le hash canonique complet séparément. Un appelant admin/bootstrap passe
par `admin_create_token` pour tout profil cible et peut scoper initialement les
cibles non-admin. `--space-ids` est refusé pour une cible admin : v2 stocke
toujours `[]`, une promotion efface les scopes et un downgrade repart vide sauf
re-scope explicite dans le même update.

Les écritures GC ne s'exécutent que si chaque space candidat se résout en
`DIRECT_LOCAL` ; un état partagé, unsafe, resync-required ou corrompu est refusé
en fail-closed. La suppression n'est jamais un raccourci en une étape : la CLI
exige le token opaque du dry-run précédent et le serveur refuse toute dérive de
l'ensemble des clés éligibles. Avant de réessayer avec un nouveau dry-run,
inspectez le JSON pour `status: "partial"` et les comptes réels
traités/supprimés/échoués.

`admin_audit_recent` n'a volontairement **aucune commande CLI**. C'est un
outil MCP réservé aux admins, utilisé par la vue Audit de `/admin` ; pour un
accès brut, appelez-le via un client MCP. La section Admin de la CLI reste donc
limitée aux 8 commandes ci-dessus.

---

## Shell interactif

```bash
uv run python scripts/mcp_cli.py shell
```

Le shell offre :

- **Autocomplétion** (Tab) sur toutes les commandes et sous-commandes
- **Historique** persistant (`~/.hivemind_shell_history`)
- **Aide contextuelle** : `help`, `help <verbe>` (ex : `help bank`)
- **Affichage Rich** coloré (tables, panels, Markdown)
- **Flag `--json`** sur n'importe quelle commande pour sortie JSON brute

---

## 🧪 Scripts de test

### Preuve Docker du secret embarqué — `verify_embedded_secret_docker.sh`

Preuve Linux/Docker du cycle de vie du credential embarqué. Elle construit une image et un projet
Compose isolés, prouve la réparation d'un volume détenu par root avec les
profils de capabilities livrés, recrée le conteneur Hivemind et ne compare que
des empreintes SHA-256, puis vérifie les entrées refusées. Il n'écrase jamais un
`.env` existant et ne supprime que ses ressources propres.

```bash
bash scripts/verify_embedded_secret_docker.sh
```

### Régression de validation des affirmations consolidées

Le LLM peut encore produire du contenu non étayé. La suite ciblée prouve le
prompt défensif et l'heuristique optionnelle d'affirmations non attribuées,
sans prétendre que ces mécanismes garantissent l'exactitude :

```bash
uv run pytest tests/test_issue17_validation.py
```

---

### Recette globale — `test_recette.py`

Script unifié avec **4 suites sélectionnables** :

```bash
uv run python scripts/test_recette.py --list                       # Liste les suites disponibles
uv run python scripts/test_recette.py --url http://localhost:8080  # TOUTES les suites
uv run python scripts/test_recette.py --suite recette              # Pipeline agent (7 tests)
uv run python scripts/test_recette.py --suite isolation            # Allowlist inter-spaces (18 tests)
uv run python scripts/test_recette.py --suite qualite              # Outils MCP (19 tests)
uv run python scripts/test_recette.py --suite recette,isolation    # Plusieurs suites
uv run python scripts/test_recette.py --suite isolation -v --step  # Pas-à-pas
uv run python scripts/test_recette.py --no-cleanup                 # Conserver les données
```

#### Suites disponibles

| Suite       | Tests | Description                                                                                                              |
| ----------- | ----- | ------------------------------------------------------------------------------------------------------------------------ |
| `recette`   | 7     | Pipeline complet : token → space → notes → consolidation LLM → bank → cleanup                                            |
| `isolation` | 18    | Mono-tenant : refus inter-spaces par allowlist, filtrage backup, read-only et grants de token                            |
| `qualite`   | 19    | Outils MCP : system, admin, space, live, bank, backup, GC                                                                |
| `graph`     | ~8    | Chemin override explicite `graph_connect` : connect, push, status, disconnect (sautée sans `--graph-url`/`--graph-token`)  |

```bash
# Suite graph — exerce le chemin override explicite graph_connect contre un
# moteur long fourni par l'opérateur ; sautée sans --graph-url / --graph-token
# (le chemin nominal embarqué ne demande aucun flag : graph push se lie tout seul)
uv run python scripts/test_recette.py --suite graph \
  --graph-url http://host.docker.internal:8080 \
  --graph-token TOKEN
```

> ⚠️ Lorsque Hivemind tourne dans Docker, utilisez `host.docker.internal` au lieu de `localhost` dans `--graph-url` pour un moteur qui tourne sur l'hôte.

### Test compaction bank — `test_bank_compact.py`

Test unitaire direct du moteur de compaction. Exécution : `uv run python scripts/test_bank_compact.py`.

---

## Options communes

| Option          | Description                                                                  |
| --------------- | ---------------------------------------------------------------------------- |
| `--url`         | URL du serveur Hivemind (défaut : `$MCP_URL` ou `http://localhost:8080`)     |
| `--token`       | Bootstrap key admin (défaut : `$ADMIN_BOOTSTRAP_KEY` ou `.env`)              |
| `--json` / `-j` | Sortie JSON brute sur n'importe quelle commande (bypasse Rich)               |
| `--suite`       | Suites à exécuter, séparées par virgules (défaut : toutes)                   |
| `--graph-url`   | URL Graph Memory (pour `--suite graph`)                                      |
| `--graph-token` | Token Graph Memory (pour `--suite graph`)                                    |
| `--step`        | Mode pas-à-pas (pause entre chaque étape)                                    |
| `--no-cleanup`  | Conserver les données après le test                                          |
| `-v`            | Affichage détaillé                                                           |
| `--list`        | Liste les suites disponibles et quitte                                       |

---

## Architecture

```
scripts/
├── mcp_cli.py                # Point d'entrée CLI Click + Shell interactif
├── test_recette.py           # 🧪 Recette globale (4 suites, ~44 tests)
├── test_bank_compact.py      # 🧪 Tests unitaires compaction bank
├── configure_dev_env.py      # Générateur local sûr de .env (refuse l'écrasement)
├── README.md                 # Documentation (Anglais)
├── README.fr.md              # Documentation (Français) ← Vous êtes ici
└── cli/
    ├── __init__.py           # Config (BASE_URL, TOKEN)
    ├── client.py             # MCPClient Streamable HTTP (SDK MCP)
    ├── commands.py           # Commandes Click (1 par outil MCP)
    ├── display.py            # Affichage Rich (tables, panels)
    └── shell.py              # Shell interactif (prompt_toolkit)
```

---

*CLI Hivemind — 1.3.0*
