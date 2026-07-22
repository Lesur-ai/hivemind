# ❓ FAQ — Hivemind

🇬🇧 [English version](FAQ.md)

---

> **Notation des versions :** Hivemind possède sa propre ligne SemVer publique
> (valeur courante dans [`VERSION`](VERSION)). Les anciens numéros cités
> ci-dessous désignent la release Live Memory héritée où un comportement est
> apparu ; ils documentent la provenance, pas des releases Hivemind ultérieures.
> Les versions de schéma comme `version: 2` ne sont pas des versions produit.

## Concepts généraux

### Qu'est-ce que les tiers short, mid et long dans Hivemind ?

Hivemind expose **un seul produit** à trois horizons de mémoire :

|                 | **`short`**                          | **`mid`**                                  | **`long`**                                 |
| --------------- | ------------------------------------ | ------------------------------------------ | ------------------------------------------ |
| **Rôle**        | Notes live immédiates                | Mémoire structurée consolidée              | Ontologie / graphe de connaissances dérivé |
| **Données**     | Observations / décisions append-only | Bank Markdown (consolidée par LLM)         | Entités et relations typées                |
| **Stockage**    | S3 (fichiers)                        | S3 (fichiers)                              | Runtime Graph Memory embarqué (Neo4j + Qdrant), livré dans la stack compose par défaut (ADR-0019) |
| **Autorité**    | Oui (chemin de commit)               | Oui (chemin de commit)                     | **Non** — projection dérivée uniquement (ADR-0010) |
| **Analogie**    | Tableau blanc                        | Carnet de projet                           | Index de bibliothèque                      |

Les trois tiers sont complémentaires. Les agents **écrivent vite** (`short`),
**consolident** dans une bank durable (`mid`), et **capitalisent** la
connaissance dans un graphe adossé à une ontologie (`long`).

> Les noms d'outils historiques `live_*` / `bank_*` / `graph_*` restent
> appelables comme **alias de compatibilité** qui mappent un-à-un vers
> `short_*` / `mid_*` / `long_*`. Les deux jeux restent enregistrés
> indéfiniment conformément à la
> [politique de compatibilité](docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations)
> (ADR-0005) ; la grammaire
> canonique short/mid/long est la grammaire recommandée pour l'avenir.

### Qu'est-ce qu'un « space » ?

Un espace mémoire isolé = un projet. Il contient :
- **Rules** : template Markdown définissant la structure de la bank
- **Notes live** : observations, décisions, todos... émises par les agents (append-only)
- **Bank** : fichiers Markdown consolidés par le LLM selon les rules

### Que sont les « rules » ?

Les rules définissent la structure de la Memory Bank. Elles sont écrites en
Markdown à la création du space, puis peuvent être remplacées par un appelant
disposant de la permission `manage` via `space_update_rules`. Ce remplacement
modifie les instructions des consolidations futures sans réécrire
silencieusement les fichiers mid existants. Révisez et versionnez ces
changements comme toute autre politique projet. Le LLM utilise les rules
courantes pour créer et maintenir les fichiers de la bank.

Exemple de rules (Memory Bank standard) :
```markdown
### projectbrief.md
Objectifs, périmètre, critères de succès.

### activeContext.md
Focus courant, changements récents, prochaines étapes.

### progress.md
Ce qui marche, ce qui reste, problèmes connus.
```

---

## Agents et tokens

### Quelle est la relation entre un token et un agent ?

Depuis Live Memory hérité **v0.8.1**, chaque token **est** un agent. Le `client_name` du token est automatiquement utilisé comme identité de l'agent — il n'y a pas de paramètre `agent=` dans `short_note`.

|                        | **Token = Agent**                                 |
| ---------------------- | ------------------------------------------------- |
| **Rôle**               | Authentification **et** identité                  |
| **Exemple**            | Token `cline-dev` → agent `cline-dev`             |
| **Partageable ?**      | Non — 1 token = 1 agent = 1 identité              |
| **Où le fournir ?**    | Header `Authorization: Bearer` (auto-détecté)     |

**Pourquoi ce changement ?** L'ancien modèle (Token ≠ Agent) permettait de passer un nom d'agent libre, ce qui causait des notes orphelines (agent non reconnu à la consolidation), de l'usurpation d'identité, et de l'éparpillement.

### Un agent peut-il lire les notes d'un autre agent ?

Oui ! `short_read(space_id="mon-projet")` retourne les notes de TOUS les agents. C'est le principe de la collaboration : chaque agent voit le travail des autres. Vous pouvez aussi filtrer par agent : `short_read(space_id="mon-projet", agent="claude-review")`.

---

## Permissions et sécurité

### Quels sont les niveaux de permissions ?

Depuis Live Memory hérité **v1.5.0**, il y a 4 niveaux **hiérarchiques et cumulatifs** :

| Niveau     | Inclut                | Accès                                                                                                                                            |
| ---------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| **read**   | —                     | Lecture : `bank_read`, `short_read`, `space_info`, `backup_list`, etc.                                                                            |
| **write**  | read                  | Mutations dans les spaces autorisés seulement : `short_note`, sa consolidation, `graph_push`, etc.                                               |
| **manage** | write + read          | Provisioning + maintenance : `space_create`, `token_create`, `space_invite_token`, `bank_write`, `space_delete`, restore/delete                  |
| **admin**  | manage + write + read | Administration : `admin_create_token`, `admin_gc_notes`, etc.                                                                                    |

Un token `write` ne peut ni créer un space/token, ni élargir un accès, ni
modifier directement la bank, ni supprimer un space. Il ne mute que les spaces
déjà allowlistés. `manage` est un rôle de provisioning transitif et de confiance
élevée : chaque manager peut créer de nouveaux spaces globalement et d'autres
managers non-admin.

### Pourquoi les permissions sont-elles cumulatives ?

Chaque niveau **inclut automatiquement** tous les niveaux inférieurs. Inutile de préciser `read,write` si vous accordez `manage` — `manage` contient déjà `write` et `read`.

```
read < write < manage < admin
```

En pratique, lors de la création ou de la mise à jour d'un token, indiquez toujours la **liste complète** des permissions (ex. : `"read,write,manage"`), car le champ `permissions` est une **liste explicite** stockée sur S3, pas un niveau unique. Le serveur vérifie la présence du niveau requis dans cette liste.

### Quel type de token créer pour mon cas d'usage ?

| Cas d'usage | Permissions recommandées | `space_ids` |
| --- | --- | --- |
| Agent IA en mode travail (Cline, Claude) | `read,write` | Spaces du projet |
| Provisioner / agent IA + maintenance | `read,write,manage` | Spaces existants concernés ; création globale possible |
| Opérateur humain (maintenance multi-projets) | `read,write,manage` | Spaces existants concernés ; délégation manager possible |
| Administrateur | `read,write,manage,admin` | Vide (l'admin voit tout) |
| Lecteur / dashboard de monitoring | `read` | Spaces à monitorer |

### Comment restreindre un token à des spaces spécifiques ?

Chaque token a un champ `space_ids` listant les spaces autorisés :

```bash
# Restreindre KSE à 3 spaces
uv run python scripts/mcp_cli.py token update sha256:363... -p "read,write" -s "live-mem,graph-mem,mcp-office"
```

**Sémantique de `space_ids` (Live Memory hérité v1.5.0+)** :
- `space_ids = ["a", "b"]` → accès uniquement à ces spaces
- `space_ids = []` pour un **non-admin** → **aucun accès** (changement dans Live Memory hérité v1.5.0, valait « tous » avant)
- `space_ids = []` pour un **admin** → accès à **tout** ; v2 impose cette forme
  stockée vide pour qu'un downgrade ne réactive pas une allowlist dormante

À la **création d'un token non-admin** via `admin_create_token`, vous pouvez utiliser :
- `space_ids=""` (par défaut) → token « muet » (aucun accès aux spaces existants). La réponse contient un champ `warning_no_access` pour le signaler explicitement.
- `space_ids="a,b,c"` → liste explicite.
- `space_ids="*"` ou `space_ids="all"` → **snapshot** de tous les spaces existants à la création (pas les futurs spaces — volontaire pour rester aligné sur la sémantique stricte héritée de v1.5.0).

Pour une cible admin, ces entrées sont ignorées et `space_ids: []` est stocké.
Une promotion admin efface les scopes ; un downgrade repart vide sauf si cette
même mise à jour assigne explicitement un nouveau scope non-admin. Le bulk suit
la même règle.

Par compatibilité, les outils admin acceptent aussi, sur une cible non-admin, un identifiant canonique
explicite dont le space n'existe pas encore. Ne l'utilisez pas comme
réservation : ce pré-grant non-admin bloque ensuite `space_create` pour le même
identifiant (y compris une préparation partielle compatible) jusqu'à son retrait
par un admin. Préférez créer d'abord, attribuer ensuite.

Un manager emploie le flux plus étroit :

```text
token_create(name="agent", permissions="read,write")
space_invite_token(space_id="project-a", token_hash="sha256:<64 hex minuscules>")
```

`token_create` n'accepte pas `space_ids` et démarre toujours vide. Chaque
`space_invite_token` ajoute un seul space existant accessible au manager.

### Le hash retourné par `admin_list_tokens` contient `sha256:` — dois-je le passer tel quel ?

**Les deux formes sont acceptées** par `admin_revoke_token`, `admin_delete_token` et `admin_update_token` :
```bash
admin_update_token(token_hash="sha256:f172084ef03...", space_ids="x")  # OK
admin_update_token(token_hash="f172084ef03...", space_ids="x")          # OK aussi
```

Le minimum reste de 16 caractères hex (8 octets de hash) pour éviter les collisions accidentelles.

Cette compatibilité concerne uniquement les outils admin. Pour
`space_invite_token`, le hash canonique exact est obligatoire : `sha256:` suivi
des 64 caractères hexadécimaux minuscules. Hash nu, majuscules et préfixes sont
refusés afin que l'onboarding manager ne devienne pas un oracle du registre.

### Que se passe-t-il quand un token crée un nouveau space ?

Un token `write` est refusé. Un token `manage` persistant peut créer un space
quel que soit son allowlist courant ; le space commité est automatiquement ajouté
à ses `space_ids`. Admin/bootstrap ont déjà l'accès global. `_meta.json` est
écrit en dernier ; un résultat partial/recovery-required doit être rejoué avec
exactement les mêmes entrées **uniquement** si `recovery.retry_safe` vaut
`true`, jamais traduit en succès ni rollbacké automatiquement. Sinon suivre
`recovery.action` : notamment, supprimer un space conserve les allowlists
historiques, donc la réutilisation de son ID reste bloquée jusqu'au retrait
admin explicite de toutes les références obsolètes, y compris sur les tokens
admin/révoqués/expirés.

### Que faire avant de supprimer ou réutiliser l'ID d'un space ?

Quiescer d'abord tous les writers et jobs du space : notes, consolidation,
opérations graph, restore/GC et activité Hivemind. La suppression revalide
chaque objet payload puis retire `_meta.json` en dernier, mais son verrou de
lifecycle ne fence pas tous les writers. Un résultat `partial` n'est pas un
succès : suivre ses compteurs, clés échouées, état du marker et
`recovery.action`, sans retry automatique. Même après une suppression propre,
`space_create` refuse la réutilisation tant qu'un admin n'a pas retiré cet ID de
tous les tokens qui le portent, y compris admin/révoqués/expirés. En v2, une
entrée admin normale porte `[]` ; la compter reste une défense en profondeur
pour un objet legacy ou pré-migration.

### Comment ajouter la permission `manage` à un token ?

```bash
uv run python scripts/mcp_cli.py token update sha256:xxx -p "read,write,manage"
```

⚠️ La mise à jour de permissions **remplace** la liste complète — incluez toujours `read,write` en plus de `manage`.

C'est une élévation de confiance explicite. Aucun writer n'est promu
**automatiquement** : n'upgrader que les tokens autorisés à allouer des spaces
arbitraires et à déléguer d'autres managers. Sinon conserver le writer et
provisionner via un manager séparé.

### Que s'est-il passé lors de la migration Live Memory héritée v1.5.0 ?

Avant Live Memory hérité v1.5.0, `space_ids=[]` signifiait « accès à tout ». Depuis cette release, cela signifie « aucun accès » (pour les tokens non-admin).

**Migration de schéma one-shot** : avant d'accepter des requêtes, le lifespan
ASGI met à niveau un token store historique en version 1. Les tokens non-admin
avec `space_ids=[]` reçoivent un snapshot des spaces alors existants, puis le
store est persisté en version 2 ; tout échec bloque le démarrage. Une allowlist
vide v2 n'est plus jamais élargie aux redémarrages suivants. Les nouveaux tokens
issus de `token_create` restent donc sans scope jusqu'à invitation. Cette
migration ne promeut jamais un writer en manager.

Le validateur Graph Memory embarqué n'effectue pas cette migration. Il n'accepte
qu'un registre dont la version est l'entier `2` et refuse les versions absentes,
legacy, futures ou malformées. Il valide toute la liste de tokens avant de
chercher le bearer : la corruption d'une autre entrée refuse donc aussi
l'authentification, comme l'autorité Hivemind fail-closed. Le long engine ne peut
contourner ni le gate de démarrage ni la validation structurelle.

### Puis-je donner des droits admin à un token ?

Oui, avec prudence :
```bash
uv run python scripts/mcp_cli.py token update sha256:xxx -p "read,write,manage,admin"
```

Un token admin peut gérer les autres tokens, consolider les notes de tous les agents et lancer le GC. Il voit tous les spaces via la permission `admin` ; v2 stocke son `space_ids` à `[]`.

Un manager ne peut ni créer ni promouvoir un admin avec `token_create` ; le
cycle global admin/bootstrap passe par `admin_create_token` ou
`admin_update_token`.

---

## Consolidation

### Comment fonctionne la consolidation ?

1. Le LLM lit les **rules**, la **bank actuelle**, la **synthèse précédente** et les **notes live**
2. Il produit des fichiers bank mis à jour (Markdown pur)
3. Les notes consolidées sont **supprimées** de `live/`
4. Une synthèse résiduelle est sauvegardée

### Que se passe-t-il si 2 agents consolident en même temps ?

Un `asyncio.Lock` par space empêche les consolidations simultanées :
- La première requête est acceptée comme un job async avec `{"status": "running"}` et un `job_id`
- La seconde reçoit `{"status": "queued"}` avec un `job_id` et une position dans la file
- Appelez `mid_consolidate` une seule fois en fin de session et rendez la main à l'utilisateur ; ne surveillez pas et ne pollez pas tant qu'un check de statut explicite n'est pas demandé

C'est voulu : les deux agents écrivent dans les mêmes fichiers bank. La consolidation séquentielle permet à chaque agent de voir le travail du précédent.

### Puis-je consolider les notes de TOUS les agents d'un coup ?

Oui. Un caller manage/admin doit demander explicitement ce scope avec
`mid_consolidate(space_id="mon-projet", agent="")`. Omettre `agent` (ou passer
`null`) consolide toujours uniquement les propres notes du caller, quel que soit
son niveau de permission.

⚠️ **Permissions** : consolider les notes d'un autre agent ou de tous les agents nécessite un token **manage** (ou admin). Un token write ne peut consolider que ses propres notes (`agent` omis/null ou `agent="mon-nom"`).

### Que deviennent les notes après consolidation ?

Elles sont **supprimées** de `live/`. Leur contenu est intégré dans les fichiers bank. C'est irréversible (d'où l'intérêt des backups).

### Le consolidateur peut-il inventer du contenu (halluciner) ?

Oui. La consolidation assistée par LLM peut encore omettre, déformer ou
inventer du contenu. Hivemind fournit les métadonnées des notes et un prompt
défensif demandant de préserver le vocabulaire métier, sourcer les nombres,
éviter les structures inventées et séparer agents/tâches. Une passe heuristique
optionnelle peut signaler des affirmations apparemment non attribuées avec
`CONSOLIDATION_VALIDATION_ENABLED=true`. Elle est désactivée par défaut, ne
prouve pas l'exactitude et ne remplace pas la revue humaine des changements
importants. Conservez les dossiers sources hors de Hivemind quand le domaine
exige un registre autoritaire.

Exécutez les tests de régression livrés avec :

```bash
uv run pytest tests/test_issue17_validation.py
```

**Si vous observez du contenu non étayé**, signalez-le sur le
[tracker d'issues Hivemind](https://github.com/Lesur-ai/hivemind/issues) avec
les notes et la bank produite.

### Comment identifier les banks qui ont besoin d'être consolidées sur plusieurs spaces ?

Utilisez **`bank_stale_spaces`** (introduit dans Live Memory hérité v2.4.0) — un outil de supervision read-only qui
scanne la liste S3 de chaque space accessible et signale ceux dont les notes live
se sont accumulées :

```bash
# Seuils par défaut : ≥5 notes non consolidées ET la plus ancienne ≥5 jours
uv run python scripts/mcp_cli.py bank stale-spaces

# Seuils personnalisés + déclenchement de la consolidation sur chaque space stale
uv run python scripts/mcp_cli.py bank stale-spaces --min-notes 10 --min-age-days 7 --consolidate
```

La même vue est disponible dans la console web admin sous
**Consolidation → filtre stale** (`/admin#/consolidation`), avec des inputs de
filtre live et des boutons `Consolidate` par ligne / en bulk.

Un space est marqué `stale` ssi `short_notes_count >= min_notes` **ET**
`oldest_note_age_days >= min_age_days` (les deux inclusifs). Le listing est léger
(clés S3 uniquement, aucun contenu fetché). L'âge de la plus ancienne note est
dérivé du préfixe timestamp du nom de fichier (`YYYYMMDDTHHMMSS_…`), pas du
`LastModified` S3 — donc le résultat est déterministe et indépendant du clock
drift entre agents.

### Qu'est-ce que la compaction de bank (`bank_compact`) ?

Quand les fichiers bank deviennent trop volumineux (> `BANK_FILE_MAX_SIZE`, 15 KB par défaut), ils peuvent causer des échecs de consolidation (dépassement du context window LLM) ou des performances dégradées.

`bank_compact` résume les fichiers surdimensionnés via un appel LLM dédié, en préservant les décisions clés et les jalons tout en supprimant les détails obsolètes.

```bash
# Scan seul (dry-run, par défaut)
uv run python scripts/mcp_cli.py bank compact mon-espace

# Appliquer la compaction
uv run python scripts/mcp_cli.py bank compact mon-espace --apply
```

L'**auto-compaction** est également déclenchée automatiquement avant la consolidation si la bank dépasse `COMPACT_THRESHOLD` (60% par défaut) du budget de sortie du LLM.

### Puis-je utiliser un proxy HTTP pour les connexions sortantes ?

Oui. Supporté depuis Live Memory hérité **v1.8.1**, définissez `PROXY_URL` dans `.env` :

```env
PROXY_URL=http://10.0.0.1:3128
```

Cela route chaque requête à destination d'Internet à travers le proxy : le
trafic S3 (boto3) et LLM (httpx) du cœur — appels de consolidation et sondes
`/health` / `system_health` — plus l'egress du Graph Memory embarqué : appels
LLM d'extraction et d'embeddings (avec leurs sondes provider-health), S3 des
documents, et lectures S3 du token-store partagé. C'est une **variable
maison** (pas `HTTP_PROXY`) pour éviter d'affecter d'autres bibliothèques
Python : le pont MCP interne Hivemind→graph-memory, Neo4j, Qdrant et les
healthchecks locaux des conteneurs restent toujours directs, et la stack MinIO
du profil dev, qui ne définit pas `PROXY_URL`, reste directe elle aussi. Un
échec proxy échoue fermé — les requêtes ne sont jamais rejouées en direct
silencieusement.

---

## Garbage Collector

### Pourquoi un Garbage Collector ?

Si un agent écrit des notes mais ne consolide jamais (crash, suppression, oubli), les notes s'accumulent indéfiniment dans `live/`. Le GC identifie et traite ces notes orphelines.

### Comment fonctionne le GC ?

3 modes via `admin_gc_notes` :

| Mode              | Paramètres                       | Action                                                                 |
| ----------------- | -------------------------------- | ---------------------------------------------------------------------- |
| **Dry-run**       | `confirm=False` (défaut)         | Scanne et rapporte                                                     |
| **Consolidation** | `confirm=True`                   | Consolide les notes dans la bank via LLM + ajoute un avertissement « ⚠️ GC » |
| **Suppression**   | `confirm=True, delete_only=True, expected_eligible_set_token=<token du dry-run>` | Supprime l'ensemble exact revu sans consolider (perte de données) |

Par défaut, le GC effectue un dry-run en lecture seule. Son
`eligible_set_token` opaque identifie l'ensemble exact des clés éligibles sans
les exposer. La suppression destructive est une seconde requête explicite qui
renvoie ce token comme `expected_eligible_set_token` ; tout ajout, retrait ou
substitution de clé à cardinalité égale retourne `status: "conflict"` et ne
supprime rien. Dans la console d'administration, l'opérateur doit également
saisir exactement le challenge `delete <N> notes`.

Avant toute mutation du GC, le service prouve que tous les spaces candidats sont
`DIRECT_LOCAL`. Sous les verrous de consolidation, il revalide avant chaque
notice GC, avant de confier la sélection exacte au consolidateur et avant chaque
lot de suppression par space. Ces contrôles route-first ne constituent pas un
compare-and-swap intra-appel pendant le travail LLM ou stockage. Un space partagé
sain est staged-not-implemented ; les états unsafe, resync-required et corrompus
sont refusés en fail-closed. La consolidation ne consomme que les clés exactes
des anciennes notes sélectionnées, jamais les notes fraîches du même agent. Une
consolidation ou suppression partielle retourne `status: "partial"` avec les
comptes réels demandés/traités/supprimés/échoués et n'est jamais retentée
automatiquement. Les clients doivent relancer et revoir un nouveau dry-run avant
une nouvelle suppression destructive ; la console d'administration impose ce
workflow en invalidant sa preuve en cache. Le token serveur est une preuve
déterministe de l'ensemble exact, pas un nonce à usage unique.

### Le GC laisse-t-il une trace dans la bank ?

Lorsqu'une consolidation réussit, oui : le GC écrit une note spéciale avant
l'appel du consolidateur :
```
⚠️ GARBAGE COLLECTOR — Consolidation forcée
Le GC a détecté X notes orphelines de l'agent 'nom-agent' (> 7 jours).
Ces notes n'ont jamais été consolidées par l'agent.
```

Le LLM voit cette note comme premier input sélectionné et l'intègre dans la
bank. Une exécution partielle ou en échec peut au contraire laisser la notice
non traitée puis la nettoyer ; consultez les champs par agent
`notice_processed` et `notice_cleaned`.

---

## Docker et déploiement

### Comment tester localement ?

```bash
# 1. Configurer l'environnement
python scripts/configure_dev_env.py
uv sync --dev
# Pour mid/long, définir URL/clé, modèle chat, modèle embeddings et sa dimension
# exacte comme documenté dans .env.example.

# 2. Démarrer la stack complète (WAF + Hivemind + runtime long + Neo4j + Qdrant + MinIO dev)
docker compose --profile dev up --build -d --wait

# 3. Tester
uv run python scripts/test_recette.py
uv run pytest tests/test_issue17_validation.py
```

### Comment fonctionne le WAF ?

Caddy + Coraza (OWASP CRS) protège contre les injections, XSS, etc. Les routes MCP (Streamable HTTP) sont authentifiées par token côté serveur. Les autres routes passent par le WAF.

### Comment déployer en production ?

1. Définir `SITE_ADDRESS=mon-domaine.com` dans `.env`
2. Exposer les ports 80+443 dans docker-compose.yml
3. Caddy obtient automatiquement un certificat Let's Encrypt
4. Voir [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) pour le runbook opérateur complet

---

## S3 et stockage

### Pourquoi S3 et pas une base de données ?

- Simplicité : pas de schéma, pas de migration, pas de serveur DB
- Portabilité : tout est fichiers Markdown/JSON
- Scalabilité : S3 gère des milliards d'objets
- Coût : le stockage S3 est très abordable

### Pourquoi deux clients S3 (SigV2 + SigV4) ?

Contrainte de certains stockages S3-compatibles (notamment Dell ECS) :
- SigV2 pour les opérations de données (PUT, GET, DELETE)
- SigV4 pour les opérations de métadonnées (HEAD, LIST)

Ce câblage dual est le défaut `S3_SIGNATURE_MODE=dual`. Si vous utilisez
AWS S3 ou MinIO, définissez `S3_SIGNATURE_MODE=sigv4` dans `.env` : un seul
client SigV4 sert alors toutes les opérations — Hivemind et le runtime
Graph Memory embarqué reflètent le même réglage.

### Puis-je utiliser AWS S3 ou MinIO ?

Oui ! Configurez `S3_ENDPOINT_URL` et les credentials, et définissez
`S3_SIGNATURE_MODE=sigv4` (voir [.env.example](.env.example)). Le mode dual
SigV2/V4 (`dual`, le défaut) n'est nécessaire que pour Dell ECS. Aucune
modification de code n'est requise.

---

## CLI et shell

### Comment configurer la CLI ?

3 façons de passer l'URL et le token :

```bash
# 1. Variables d'environnement
export MCP_URL=http://localhost:8080
export MCP_TOKEN=lm_xxx
uv run python scripts/mcp_cli.py health

# 2. Paramètres CLI
uv run python scripts/mcp_cli.py --url http://mon-serveur:8080 --token lm_xxx health

# 3. Automatique (lit .env)
uv run python scripts/mcp_cli.py health   # URL par défaut 8080, token depuis .env
```

### Comment obtenir l'aide sur une commande ?

```bash
# CLI Click (aide native --help)
uv run python scripts/mcp_cli.py space --help
uv run python scripts/mcp_cli.py bank consolidate --help

# Shell interactif
hivemind> help           # aide globale
hivemind> help space     # sous-commandes space
hivemind> space          # idem
hivemind> help bank      # sous-commandes bank
```

### Puis-je utiliser la CLI en mode JSON pour scripter ?

Oui ! Ajoutez `--json` à n'importe quelle commande :

```bash
uv run python scripts/mcp_cli.py space list --json | jq '.spaces[].space_id'
```

---

## Troubleshooting — problèmes fréquents

### Je reçois un 403 sur tous les spaces

**Cause la plus fréquente** : votre token a `space_ids=[]` (aucun accès). Avec la sémantique actuelle héritée de Live Memory v1.5.0, un token non-admin sans `space_ids` ne peut accéder à rien.

**Diagnostic** :
```bash
uv run python scripts/mcp_cli.py token list --json | jq '.tokens[] | select(.name=="mon-token") | .space_ids'
```

**Solution** : demander à un manager ayant accès d'inviter le hash complet
exact, ou à un admin de mettre le token à jour :
```bash
uv run python scripts/mcp_cli.py space invite space-a sha256:<64-hex-minuscules>
uv run python scripts/mcp_cli.py token update sha256:xxx -s "space-a,space-b"
```

### Mon token `manage` ne peut rien faire

Un token `manage` sans `space_ids` ne peut accéder aux spaces existants ni y
inviter. Il conserve toutefois l'autorité globale de créer de nouveaux spaces
et d'autres managers ; tout nouveau space créé est auto-ajouté à son allowlist.

**Solution** : le faire inviter par un manager autorisé avec le hash exact, ou
le faire mettre à jour par un admin :
```bash
uv run python scripts/mcp_cli.py token update sha256:xxx -s "space-a,space-b"
```

### La consolidation échoue avec « LLM returned invalid JSON »

Cause probable : la bank est trop volumineuse. Le LLM a un context window limité et peut échouer sur les réponses JSON longues.

**Solutions** :
1. Compacter la bank : `bank_compact mon-espace --apply`
2. Vérifier les tailles : `bank_list mon-espace` — si un fichier dépasse 15 KB, c'est un candidat à la compaction
3. Relancer la consolidation après compaction

### `mid_consolidate` retourne « queued »

Un autre agent (ou vous-même dans un autre terminal) consolide le même space. Votre requête a été acceptée et s'exécutera après les jobs précédents sur ce space.

**Solution** : rendez la main à l'utilisateur sans poller. Conservez le `job_id` retourné uniquement si un check de statut explicite est nécessaire plus tard. `bank_consolidation_status(job_id)` est manuel uniquement ; ne le pollez pas automatiquement.

### Je ne retrouve plus mes notes après consolidation

C'est normal ! Les notes sont **supprimées** de `live/` après consolidation. Leur contenu est intégré dans les fichiers bank. Utilisez `mid_read_all` pour retrouver le contenu consolidé.

Si vous pensez que des notes ont été perdues, vérifiez la synthèse résiduelle : `space_summary mon-espace`.

---

## Limites et performances

### Combien de notes peut-on écrire ?

Hivemind ne publie aucune promesse de capacité illimitée. Une note est limitée
à 100 000 caractères, `short_read` retourne au plus 500 notes par appel et une
consolidation traite jusqu'à 200 notes par job par défaut
(`CONSOLIDATION_MAX_NOTES`). Le volume total retenu dépend du backend S3, du
lifecycle des objets, des limites de requête et du capacity planning opérateur.

### Quelle est la latence ?

Cette release ne publie ni SLA de latence ni benchmark portable. Les résultats
dépendent de la distance et de la charge S3, du réseau, des modèles chat et
embeddings, de la queue fournisseur, de la taille de la bank et des ressources
hôte. Benchmarkez le déploiement exact avec des données représentatives et
consignez environnement, nombre d'opérations, warm-up et percentiles avant de
fixer une cible opérationnelle.

### Combien d'agents simultanés ?

Hivemind ne publie aucune garantie d'agents illimités ni de « zéro conflit ».
Les notes append-only utilisent des clés d'objet uniques, mais la concurrence
pratique est bornée par le WAF, les workers Hivemind, S3, le réseau et le
fournisseur. La consolidation est sérialisée FIFO par space (un job mute la
bank d'un space à la fois) et les écritures Project Mesh exigent l'ACK de chaque
membre actif. Testez en charge la topologie visée. `mid_consolidate` reste un
handoff async « call-once » ; ne surveillez pas et ne pollez pas sauf demande
explicite.
