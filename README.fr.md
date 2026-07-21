<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/hivemind-mark-dark.svg">
  <img alt="Hivemind" src="assets/brand/hivemind-mark.svg" width="92" height="92">
</picture>

# hivemind

***La couche mémoire ouverte pour la conscience collective des agents.***

Service MCP open-source et vendor-neutral pour une mémoire d'agent à trois
horizons : `short` · `mid` · `long`.

Les agents de n'importe quel runtime MCP-compatible perçoivent le travail des
autres, héritent de ce qu'ils ont appris, et comprennent ensemble des projets
complexes.

[![protocole](https://img.shields.io/badge/protocole-MCP-00A7C7?style=flat-square)](#-concept)
[![version](https://img.shields.io/badge/version-1.2.3-9CA3AF?style=flat-square)](#-licence)
[![CI](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml/badge.svg)](https://github.com/Lesur-ai/hivemind/actions/workflows/ci.yml)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-111827?style=flat-square)](#-licence)
[![python](https://img.shields.io/badge/python-3.11+-F59E0B?style=flat-square)](#-pr%C3%A9requis)

English · [README.md](README.md)

</div>

---

## 📋 Table des matières

- [Concept](#-concept)
- [Project Mesh](#project-mesh)
- [Ce que Hivemind ne revendique PAS (V1)](#-ce-que-hivemind-ne-revendique-pas-v1)
- [Architecture](#-architecture)
- [Prérequis](#-prérequis)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Démarrage rapide](#-démarrage-rapide)
- [Outils MCP](#-outils-mcp)
- [Tier long — ontologie / graphe de connaissances](#-tier-long--ontologie--graphe-de-connaissances)
- [Interface web](#-interface-web)
- [Intégration MCP](#-intégration-mcp)
- [CLI et shell](#-cli-et-shell)
- [Tests](#-tests)
- [Sécurité](#-sécurité)
- [Structure du projet](#-structure-du-projet)
- [Troubleshooting](#-troubleshooting)
- [Contribuer](#-contribuer)
- [Carte de la documentation](#-carte-de-la-documentation)

---

## 🎯 Concept

**Hivemind** est le service MCP (Model Context Protocol) open-source qui donne
aux agents IA une **mémoire partagée vendor-neutral sur trois horizons** —
`short`, `mid`, `long` — auxquels s'ajoute **Project Mesh**, sa feature de
synchronisation au niveau projet : plusieurs agents et équipes peuvent partager
un même space mémoire projet logique, et les administrateurs peuvent activer le
flux de fédération Mesh Sync V1 implémenté depuis la console protégée par
capacités — voir [Project Mesh](#project-mesh) pour sa frontière full-mesh
all-ACK exacte.

Hivemind transforme le contexte d'agent isolé en un space partagé où les
agents **perçoivent** ce que font les autres, **héritent** de ce qu'ils ont
appris, et **comprennent** ensemble des projets complexes.

Les fichiers mémoire Markdown sont une bonne primitive, mais seuls ils
deviennent des îlots : un agent écrit une `.md` bank, un autre agent ou
fournisseur démarre dans un contexte différent, et le porteur du projet
devient la couche d'intégration. Hivemind fait du **space** le propriétaire
de la mémoire, et non d'un assistant, IDE, fournisseur de modèle ou
historique de prompt particulier. Si vous passez d'un agent MCP-compatible à
un autre, le contexte projet accumulé reste dans votre stockage Hivemind et
votre périmètre de gouvernance.

### Les trois horizons de mémoire

| Horizon | Anciennement | Contenu | Moment révélateur |
| --- | --- | --- | --- |
| **`short`** | `live_*` | Notes live append-only : observations, décisions, todos — contexte de travail immédiat, visible dans tout le space. | **Percevoir** — un agent change de cap parce qu'il voit le travail en cours d'un autre. |
| **`mid`** | `bank_*` | La memory bank Markdown consolidée : rules, synthèse, contexte projet. La mémoire de travail structurée dont héritent les autres agents. | **Hériter** — un agent récupère une règle ou une méthode laissée par un autre agent, sans prompt manuel. |
| **`long`** | `graph_*` | Le tier ontologie / graphe de connaissances : rappel associatif dérivé, liens conceptuels construits par le processus collectif. | **Comprendre** — un agent retrouve des liens logiques à travers la connaissance collective. |

Les noms d'outils historiques `live_*` / `bank_*` / `graph_*` correspondent
un-à-un à `short_*` / `mid_*` / `long_*` et **restent appelables** comme
alias de compatibilité. Voir [Alias de compatibilité](#alias-de-compatibilité).

<a id="project-mesh"></a>

### Project Mesh

Au-delà d'un serveur unique, **Project Mesh** est la feature de
synchronisation au niveau projet de Hivemind. En V1, elle est livrée en deux
étages clairement séparés :

- **Disponible aujourd'hui — partage au niveau agent.** Plusieurs équipes,
  contributeurs open-source et flottes d'agents connectent leurs propres
  runtimes MCP-compatibles à un même `space_id` unifié sur un déploiement
  Hivemind — un space possède ses notes `short`, sa bank `mid`, sa projection
  `long` et son état de coordination Project Mesh.
- **Disponible comme fédération d'instances opt-in.** Deux administrateurs
  appairent une cible vierge en trois actions : créer une invitation opaque à
  usage unique (valable **3 600 secondes**), la coller puis l'accepter sur la
  cible, puis la vérifier et l'approuver à la source. L'échange pair signé
  réalise la transition de membership pending, l'import bootstrap borné, l'ACK
  final et l'activation ; une panne après mutation reste en recovery explicite,
  sans rollback silencieux. Ce workflow d'appairage V1 provisionne exactement
  un **mesh à deux nœuds** depuis une source dont le space ne compte qu'un
  membre actif. Il refuse une source qui compte déjà plus d'un membre actif ;
  l'ajout d'un troisième nœud par ce workflow n'est pas supporté en V1. Après
  le bootstrap, les deux pairs opèrent symétriquement. Les
  opérateurs utilisent les routes `/admin`
  `#/mesh` protégées par capacité. Mesh reste une surface HTTP admin/pair : la
  découverte agent plafonnée à 24 outils et la surface MCP complète n'exposent
  **aucun outil MCP `mesh_*`**.

Le cas d'usage est l'accélération du développement logiciel : plusieurs
contributeurs peuvent travailler en parallèle avec leurs propres agents
pendant que la mémoire partagée, la provenance et l'ordonnancement des
mutations restent à l'intérieur de la frontière projet. Voir
[`docs/PROJECT_MESH.md`](docs/PROJECT_MESH.md) pour
le vocabulaire canonique.

<!-- non-claims -->
> Le protocole **Mesh Sync V1** de Project Mesh est conservateur par design :
> **full-mesh all-ACK, pas quorum.** Voir
> [Ce que Hivemind ne revendique PAS (V1)](#-ce-que-hivemind-ne-revendique-pas-v1)
> pour la frontière exacte entre ce qui fonctionne aujourd'hui et ce qui
> relève de travaux ultérieurs.
<!-- /non-claims -->

### Propriété vendor-neutral

La promesse multi-agent de Hivemind est aussi une promesse multi-vendor :

| Sans Hivemind | Avec Hivemind |
| --- | --- |
| Les mémoires Markdown vivent comme des fichiers isolés à côté de chaque agent ou outil. | La mémoire `short`, `mid` et `long` vit dans un seul space Hivemind gouverné. |
| Changer d'agent implique souvent de re-prompter, copier le contexte, ou faire confiance à l'historique du vendor. | N'importe quel agent MCP-compatible peut lire et écrire via des tokens scopés. |
| La connaissance projet dérive dans des sessions vendor-spécifiques. | La mémoire persiste dans le stockage contrôlé par l'opérateur, protégeant continuité, propriété et IP projet. |

### Pourquoi trois horizons ?

Un seul niveau ne suffit jamais :

- `short` seul est **éphémère** — il défile à mesure que le projet avance.
- `long` seul est **trop lourd** pour des notes quotidiennes rapides.
- `mid` est le pont structuré : les agents **écrivent vite** (`short`),
  **consolident** dans une bank durable (`mid`), puis **capitalisent** la
  connaissance dans un graphe adossé à une ontologie (`long`).

Cette architecture de mémoire partagée suit le cadre multi-agent de
[Tran et al., 2025 — *Multi-Agent Collaboration Mechanisms: A Survey of LLMs*](https://arxiv.org/abs/2501.06322),
qui identifie un **environnement partagé** et une **mémoire partagée**
comme composants fondamentaux pour que des agents LLM se coordonnent au
lieu de fonctionner comme des algorithmes isolés.

---

## 🚫 Ce que Hivemind ne revendique PAS (V1)

Hivemind est positionné honnêtement. Les points suivants ne sont **pas**
le comportement actuel. Une phase ultérieure pourra revisiter chacun, mais
d'ici là ils ne sont pas implémentés et ne doivent pas être supposés.
Cette section est le miroir public de
[`docs/POSITIONING.md`](docs/POSITIONING.md), le
garde-fou canonique des non-revendications, et est encadrée par des
sentinelles HTML pour qu'un lint release-doc automatisé la détecte de manière
déterministe.

<!-- non-claims -->
Hivemind V1 ne revendique PAS :

- **le consensus par quorum** — Project Mesh V1 / Mesh Sync V1 est en
  full-mesh all-ACK, pas un runtime à quorum.
- **la topologie en hub** — il n'y a pas de hub central ; tous les peers
  sont équivalents sous Mesh Sync V1.
- **un master / leader permanent à l'exécution** — aucun nœud ne détient
  un leadership permanent, et il n'y a pas de chemin d'élection de leader.
- **la fusion CRDT offline-first** — Hivemind V1 n'est pas un système CRDT
  et ne tente pas de fusion sans conflit offline-first.
- **la fusion de deux spaces déjà peuplés** — la V1 ne fusionne pas deux
  spaces qui portent chacun déjà un état ; il n'y a pas de chemin de
  réconciliation entre deux spaces peuplés.
- **la consolidation collective parallèle** — la consolidation du tier
  `mid` est sérialisée par space ; il n'y a pas de consolidation
  collective parallèle entre agents.
- **le comportement multi-tenant** — l'allowlist `space_ids` par token
  est la **seule** primitive d'isolation. Il n'y a pas d'objet tenant,
  pas de row-level security, et pas d'isolation par bucket par tenant
  dans l'édition open-source. `space_ids` est une allowlist, **pas** une
  multi-tenance ; pour la tenancy, voir les
  [points d'extension downstream](docs/EXTENSION_POINTS.md) (ADR-0003).

De plus :

- **La mémoire `long` n'est jamais autoritaire.** Le tier `long`
  (ontologie / graphe de connaissances) est une **projection dérivée
  uniquement**. Il n'est jamais la source de validité de commit, de
  rollback, d'audit, de tombstones, de watermarks ou de recovery, et
  reste **hors du chemin de commit** (ADR-0010).
- **`backup_restore` sur un space Hivemind partagé est refusé par défaut,
  forçage-en-avant uniquement sur confirmation opérateur explicite.** La
  restauration **par-dessus** un space Hivemind partagé / unsafe / corrompu
  (détection read-only via `hive_status_label`, ADR-0008) est **refusée par
  défaut** ; un état critique corrompu, une `NodeIdentity` locale absente
  (nœud orphelin) ou un backup dont la `bank_version` est strictement
  supérieure au pointeur live sont tous refusés **fail-closed sans aucune
  mutation**. Avec `unsafe_recovery=True` confirmé par l'opérateur, le
  restore exécute la chorégraphie de forçage-en-avant champ-par-champ
  **ADR-0014 (Accepted)** : stage la bank du backup via
  `CommitRuntime`, force `membership_epoch` et `term` strictement vers
  `max(live, backup)+1`, unionne les tombstones live et backup, drop la
  queue en attente, purge `acks/`, prune `watermarks/` vers la
  `MembershipView` post-bump, publie un `BankCommit` forward via
  `assert_commit_allowed()` (point d'autorisation unique ADR-0011) à
  `pointer+1`, émet les events d'audit `UNSAFE_RECOVERY_RESTORED` +
  `RESYNC_REQUIRED` sous `{space}/_hivemind/events/`, et marque le nœud
  `HiveNodeStatus.RESYNC_REQUIRED` jusqu'à re-bootstrap. Voir le
  [guide public de migration et recovery](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.fr.md#restore-dun-space-project-mesh-existant)
  (ADR-0014). Pour
  le cas mono-instance, non partagé (`local_only` / `not_a_space`),
  `backup_restore` reste inchangé — passthrough byte-for-byte.
<!-- /non-claims -->

« Conscience collective » est un langage de positionnement, jamais une
revendication littérale — Hivemind ne revendique ni AGI, ni sentience,
ni conscience.

---

## 🏗️ Architecture

```
     Agent Cline        Agent Claude        Agent X
          │                   │                │
          └────────┬──────────┘                │
                   │                           │
                   ▼  Protocole MCP (Streamable HTTP)  ▼
          ┌────────────────────────────────────────┐
          │   WAF (Caddy + Coraza CRS)             │
          │   Rate Limiting • TLS • OWASP CRS      │
          └────────────┬───────────────────────────┘
                       │
          ┌────────────┴───────────────────┐
          │   Service MCP hivemind         │
          │   short · mid · long           │
          │   État Project Mesh            │
          │   Auth Bearer • consolidation  │
          └──────┬──────────┬──────┬───────┘
                 │          │      │
          ┌──────┴──┐  ┌────┴───┐  │
          │   S3    │  │  LLM   │  │  MCP Streamable HTTP
          │ stockage│  │ (consol│  │  (binding interne moteur long)
          │ durable │  │   mid) │  │
          └─────────┘  └────────┘  │
                       ┌───────────┴────────────┐
                       │  Moteur tier long      │
                       │  ontologie / graphe    │
                       │  (projection dérivée)  │
                       └────────────────────────┘
```

**Stack protocolaire** : S3 + LLM pour l'état autoritaire short/mid et
l'état Project Mesh.
**Produit Hivemind complet** : inclut le moteur obligatoire d'ontologie /
graphe de connaissances `long`, lié au space en interne. C'est une
**projection dérivée**, en dehors du chemin de commit — voir
[Tier long](#-tier-long--ontologie--graphe-de-connaissances).

> Le profil WAF et le runtime `long` embarqué sont livrés dans la stack
> compose par défaut ([docker-compose.yml](docker-compose.yml)) ; le backend
> S3 et le fournisseur LLM sont fournis par l'opérateur via `.env`. Les
> endpoints concrets montrés dans les exemples sont des exemples, pas des
> valeurs par défaut.

---

## 📦 Prérequis

- **Docker** >= 24.0 + **Docker Compose** >= 2.17.0 (`up --wait` est utilisé)
- **Python 3.11+** et [`uv`](https://docs.astral.sh/uv/) (CLI/tests locaux)
- Un **stockage S3-compatible** (Dell ECS, AWS, MinIO)
- Un **LLM** compatible API OpenAI (pour la consolidation `mid`, l'extraction,
  les embeddings et les requêtes sémantiques `long`)
- Aucun backend graphe ni token graphe séparé : Graph Memory + Neo4j + Qdrant
  sont **embarqués dans la stack compose par défaut** (ADR-0019). Le runtime
  embarqué utilise néanmoins l'API LLM configurée pour l'ingestion et les
  requêtes long.

---

## 🚀 Installation

> Vous migrez depuis Live Memory + Graph Memory séparés vers un déploiement
> Hivemind unique ? Voir
> [`docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.fr.md`](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.fr.md)
> (EN : [`docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md`](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)).
> Pour configurer les agents après migration, utilisez le guide canonique en
> anglais [`docs/AGENT_MEMORY_SETUP.md`](docs/AGENT_MEMORY_SETUP.md). Il impose
> notamment un nouveau token Hivemind distinct pour chaque identité agent.
>
> Le `docker compose up -d` par défaut démarre le produit Hivemind
> **complet** : WAF, service MCP Hivemind et runtime `long` embarqué
> (Graph Memory + Neo4j + Qdrant) sur le réseau Docker interne (ADR-0019).
> Un initialiseur ponctuel sans réseau prépare le volume de secrets, puis
> Hivemind crée/persiste et enregistre son credential interne scopé avant la
> readiness ; toute erreur de persistance ou révocation bloque le démarrage.
> Chaque space produit se lie automatiquement au moteur `long` embarqué à sa
> première écriture long (`long_push`) — aucun backend à provisionner à part
> et aucune étape de liaison manuelle.

### 1. Cloner le dépôt

```bash
git clone https://github.com/Lesur-ai/hivemind.git
cd hivemind
```

### 2. Créer un environnement local de développement

```bash
python scripts/configure_dev_env.py
```

Le helper crée `.env` en mode `0600`, avec des credentials bootstrap/MinIO/
Neo4j aléatoires, `sigv4` et Mesh désactivé pour une évaluation locale
mono-nœud délibérée. Il refuse d'écraser un fichier existant et n'affiche
jamais les secrets générés. Avant de tester `mid` ou `long`, configurez
`LLMAAS_API_URL`, `LLMAAS_API_KEY`, `LLMAAS_MODEL`,
`LLMAAS_EMBEDDING_MODEL` et la dimension exacte du modèle dans
`LLMAAS_EMBEDDING_DIMENSIONS`. Le fournisseur doit exposer des endpoints
compatibles `/chat/completions` et `/embeddings` ; les modèles du template sont
des exemples, pas des defaults portables. En production, copiez plutôt
`.env.example`, fournissez votre S3 et vos secrets, et configurez une identité
Mesh complète si vous activez Mesh.

### 3a. Démarrage Docker (recommandé)

```bash
# Construire les images, MinIO local inclus via le profil dev
docker compose --profile dev build

# Démarrer la stack complète par défaut
# (WAF + initialiseur secret + Hivemind + Graph Memory embarqué + Neo4j + Qdrant)
docker compose --profile dev up -d --wait

# Vérifier le statut
docker compose ps

# Health check
curl -s http://localhost:8080/health
```

### 3b. Démarrage local (développement)

Un démarrage direct sur l'hôte n'exécute pas l'initialiseur de volume Compose.
Définissez un `LONG_EMBEDDED_TOKEN` stable et non vide dans `.env`, notamment
sur macOS, ou configurez un chemin local Linux respectant le contrat
`0700`/`0600` décrit dans
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#embedded-credential-lifecycle-and-repair).

```bash
# Créer l'environnement projet et installer les dépendances dev verrouillées
uv sync --locked --dev

# Lancer le serveur
uv run python -m live_mem
```

### 4. Utiliser la CLI fournie

```bash
uv run python scripts/mcp_cli.py --help
```

### 5. Tier `long` — embarqué et lié automatiquement

Le moteur obligatoire `long` d'ontologie / graphe de connaissances tourne
déjà : la stack compose par défaut (étape 3a) le démarre avec ses datastores
Neo4j et Qdrant, sur le réseau interne uniquement (ADR-0019). Chaque space
produit s'y lie automatiquement à sa première écriture long
(`long_push` / `graph_push`), en dérivant un `memory_id` déterministe du
`space_id` — **aucune étape de liaison manuelle**.

`graph_connect` / `long_connect` reste appelable comme **override avancé /
diagnostic uniquement** (ex. pointer temporairement un space vers une Graph
Memory externe historique pendant une migration, ou choisir une ontologie
non par défaut). Ce n'est jamais une étape d'installation requise.

### 6. Vérifier l'installation

```bash
# Health check via la CLI
uv run python scripts/mcp_cli.py health

# Ou test E2E complet (crée un space, écrit des notes, consolide)
uv run python scripts/test_recette.py

# Readiness du tier long : après la première long_push d'un space,
# graph_status / long_status rapporte le runtime embarqué comme connected
# (auto-bind, aucune étape manuelle).
```

### Ports exposés

| Service     | Port   | Description                                   |
| ----------- | ------ | --------------------------------------------- |
| **WAF**     | `8080` | Seul port exposé — Caddy WAF → MCP hivemind   |
| Serveur MCP | `8002` | Réseau Docker interne uniquement              |

---

## ⚙️ Configuration

Éditez `.env`. Toutes les variables sont documentées dans `.env.example`.

### Variables obligatoires

| Variable               | Description                       | Exemple                              |
| ---------------------- | --------------------------------- | ------------------------------------ |
| `S3_ENDPOINT_URL`      | URL du endpoint S3                | `https://s3.example.com`             |
| `S3_ACCESS_KEY_ID`     | Clé d'accès S3                    | `AKIA...`                            |
| `S3_SECRET_ACCESS_KEY` | Clé secrète S3                    | `wJal...`                            |
| `S3_BUCKET_NAME`       | Nom du bucket                     | `hivemind`                           |
| `S3_REGION_NAME`       | Région S3                         | `eu-west-1`                          |
| `LLMAAS_API_URL`       | URL de l'API LLM (avec `/v1`)     | `https://llm.example.com/v1`         |
| `LLMAAS_API_KEY`       | Clé d'API LLM                     | `sk-...`                             |
| `LLMAAS_MODEL`         | Identifiant exact du modèle chat accepté par `/chat/completions` | `modele-chat-fournisseur` |
| `LLMAAS_EMBEDDING_MODEL` | Identifiant exact du modèle embeddings accepté par `/embeddings` | `modele-embedding-fournisseur` |
| `LLMAAS_EMBEDDING_DIMENSIONS` | Longueur exacte des vecteurs retournés | `1024` |
| `ADMIN_BOOTSTRAP_KEY`  | Clé bootstrap admin (≥ 32 caractères aléatoires) | générée par `configure_dev_env.py` |

> Le préfixe de variables d'environnement `LLMAAS_*` est hérité de
> l'intégration LLM-as-a-Service amont et est conservé tel quel dans la
> release publique — les noms de ces tables sont ceux que le service lit
> réellement.
> Les valeurs `qwen3.5:27b`, `bge-m3:567m` et `1024` du template décrivent un
> seul profil fournisseur. Remplacez ensemble les identifiants et la dimension.
> Une dimension erronée casse les écritures/recherches long ; la changer après
> ingestion exige une reconstruction revue de la collection Qdrant et une
> réingestion.

### Runtime long embarqué (obligatoire, ADR-0019)

Le moteur `long` est livré dans la stack compose par défaut et se lie
automatiquement par space à la première écriture long — aucune valeur
opérateur `url` / `token` / `memory_id` à configurer. Ses réglages `.env` :

| Variable | Défaut | Description |
| -------- | ------ | ----------- |
| `NEO4J_PASSWORD` | _(requis)_ | Mot de passe du graph store Neo4j embarqué — la stack refuse de démarrer sans |
| `LONG_EMBEDDED_URL` | `http://graph-memory:8002` | URL réseau-interne du runtime long embarqué |
| `LONG_EMBEDDED_TOKEN` | _(vide = créé au démarrage)_ | Token scopé `read,write` local uniquement ; si vide, Hivemind le persiste atomiquement et l'enregistre avant la readiness, sans fallback volatil ni réactivation après révocation |

L'override par-space `graph_connect` / `long_connect` (url, token,
memory_id, ontology) est une échappatoire avancée / diagnostic uniquement —
ex. une Graph Memory externe historique pendant une migration — jamais une
exigence d'installation.

### Variables optionnelles — LLM (consolidation `mid`)

Le consolidateur `mid` utilise un LLM (API compatible OpenAI) pour
transformer les notes live `short` en bank `mid` structurée.

| Variable                  | Défaut            | Description                     |
| ------------------------- | ----------------- | ------------------------------- |
| `LLMAAS_MODEL`            | `qwen3.5:27b`     | Nom du modèle LLM tel qu'exposé par le fournisseur |
| `LLMAAS_CONTEXT_WINDOW`   | `131072`          | Context window TOTAL du modèle (input + output combinés, en tokens) |
| `LLMAAS_MAX_TOKENS`       | `16384`           | Budget de SORTIE max par requête (en tokens). Le consolidateur l'ajuste dynamiquement : `output = min(MAX_TOKENS, CONTEXT_WINDOW - input)` |
| `LLMAAS_TEMPERATURE`      | `0.3`             | Créativité du LLM (0.0 = déterministe, 1.0 = très créatif) |
| `PROXY_URL`               | _(aucun)_         | Proxy HTTP sortant (ex. `http://10.0.0.1:3128`). **Variable maison** (pas `HTTP_PROXY`) — injectée manuellement dans boto3 (S3) et httpx (LLM). Non supportée pour les connexions du tier `long`. |

### Variables optionnelles — Consolidation et compaction

| Variable                  | Défaut            | Description                     |
| ------------------------- | ----------------- | ------------------------------- |
| `MCP_SERVER_PORT`         | `8002`            | Port d'écoute du serveur MCP    |
| `MCP_SERVER_DEBUG`        | `false`           | Logs détaillés (messages d'erreur complets) |
| `CONSOLIDATION_TIMEOUT`   | `600`             | Timeout par appel LLM (secondes) |
| `CONSOLIDATION_MAX_NOTES` | `200`             | Max de notes par consolidation  |
| `CONSOLIDATION_BATCH_SIZE`| `5`               | Notes par batch LLM (petit = précis, grand = plus rapide) |
| `CONSOLIDATION_COOLDOWN_SECONDS` | `60`      | Cooldown anti-spam par space pour `bank_consolidate` (`0` désactive) |
| `CONSOLIDATION_VALIDATION_ENABLED` | `false` | Vérification optionnelle post-consolidation des claims non sourcés |
| `CONSOLIDATION_VALIDATION_MAX_EXAMPLES` | `20` | Nombre max d'exemples retournés par la validation |
| `COMPACT_THRESHOLD`       | `0.6`             | Déclenchement de l'auto-compaction (0.6 = compacter si bank > 60% du budget) |
| `BANK_FILE_MAX_SIZE`      | `15360`           | Taille max par fichier bank (octets, 15 KB). Au-dessus = candidat à la compaction |
| `RESPONSE_MAX_BYTES`      | `524288`          | Taille max des réponses non-MCP avant troncature |
| `API_TOOL_MAX_BODY_BYTES` | `1048576`         | Taille max du corps accepté par `/api/tool` |
| `ADMIN_AUDIT_RING_SIZE`   | `500`             | Capacité par instance du buffer d'audit console/auth en mémoire ; validée dans `1..500` au démarrage |

---

## ▶️ Démarrage rapide

Voici le parcours local copiable. Il utilise des secrets locaux générés, MinIO
et le credential bootstrap uniquement pour l'évaluation initiale. Avant
`mid`, configurez dans `.env` l'URL, la clé, l'identifiant exact du modèle
chat, celui du modèle embeddings et la dimension retournée ; le fournisseur
doit exposer `/chat/completions` et `/embeddings`. `short` n'a pas besoin de
LLM. Le runtime `long` embarqué démarre avec la stack et s'auto-lie à sa
première écriture, mais l'ingestion et la requête sémantique long utilisent
également ce fournisseur.

```bash
# 1. Cloner le dépôt
git clone https://github.com/Lesur-ai/hivemind.git
cd hivemind

# 2. Créer des credentials locaux aléatoires et installer la CLI verrouillée
python scripts/configure_dev_env.py
uv sync --locked --dev

# 3. Démarrer WAF + Hivemind + MinIO + runtime long embarqué
docker compose --profile dev up --build -d --wait
docker compose ps
docker compose logs hivemind --tail 50

# 4. Pointer la CLI sur le WAF et vérifier le credential bootstrap généré
export MCP_URL=http://localhost:8080
export MCP_TOKEN="$(sed -n 's/^ADMIN_BOOTSTRAP_KEY=//p' .env)"
uv run python scripts/mcp_cli.py health --json
uv run python scripts/mcp_cli.py whoami --json

# 5. Créer un space depuis les rules standard livrées
uv run python scripts/mcp_cli.py space create hivemind-demo \
  --description "Space de démo quickstart" \
  --rules-file RULES/live-mem.standard.memory.bank.md

# 6. short — écrire puis lire une vraie note
uv run python scripts/mcp_cli.py live note hivemind-demo observation "hello short"
uv run python scripts/mcp_cli.py live read hivemind-demo

# 7. mid — nécessite la configuration LLMAAS fournisseur/modèles complète
# Retour immédiat avec status running|queued et un job_id.
uv run python scripts/mcp_cli.py bank consolidate hivemind-demo --json
```

Arrêtez-vous après cet accusé. Ce quickstart opérateur demande explicitement des
contrôles manuels déclenchés par l'opérateur ; un agent de routine doit rendre
la main sans polling. Collez le `job_id` retourné seulement au moment choisi
pour vérifier :

```bash
JOB_ID="coller-le-job-id-retourné"
uv run python scripts/mcp_cli.py bank consolidation-status "$JOB_ID" --json
```

Ne continuez que si cette réponse indique l'état terminal de premier niveau
`"status": "succeeded"`. Si elle indique encore `running` ou `queued`,
arrêtez-vous et ne revérifiez qu'ultérieurement, à un moment choisi —
n'automatisez pas de boucle. Une réponse `failed`, `not_found` ou en erreur fait
échouer le quickstart : diagnostiquez-la avant de lire la bank ou de pousser la
mémoire long.

```bash
# 8. Vérifier le résultat mid terminé
uv run python scripts/mcp_cli.py bank read-all hivemind-demo --json

# 9. long — nécessite aussi le modèle embeddings et sa dimension configurés
uv run python scripts/mcp_cli.py graph push hivemind-demo --json
uv run python scripts/mcp_cli.py graph status hivemind-demo --json
uv run python scripts/mcp_cli.py graph query hivemind-demo "hello" --json

# Ne plus exposer la clé bootstrap aux processus enfants après l'évaluation.
unset MCP_TOKEN
```

Pour tout déploiement persistant, créez un manager dédié et un token
`read,write` distinct par agent, invitez chaque token au space, puis cessez
d'utiliser le credential bootstrap. La procédure exacte figure dans
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#quickstart-dev).

Les noms d'outils historiques `live_*` / `bank_*` / `graph_*` restent
appelables comme alias de compatibilité — voir
[Alias de compatibilité](#alias-de-compatibilité).

Voir [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) et
[`docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md`](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)
pour les détails opérateur complets.

---

## 🔧 Outils MCP

Hivemind expose l'ensemble canonique documenté dans
[tests/fixtures/tool_surface.json](tests/fixtures/tool_surface.json)
(**61 noms enregistrés = 48 enregistrements directs + 13 alias de tier**,
totaux suivis par la fixture de surface) via le protocole MCP
(Streamable HTTP) : outils historiques + alias canoniques de tier
`short_*`/`mid_*`/`long_*` (les deux jeux restent appelables). Les tiers `short`/`mid`/`long`
portent la grammaire publique ; `space_*`, `token_*`, `system_*`, `backup_*` et
`admin_*` sont **transverses** et conservent leurs noms. Il existe 35 outils
directs sans alias ; les 13 alias de tier restent inchangés. Voir le mapping
stable dans
[`docs/TOOL_MAPPING.md`](docs/TOOL_MAPPING.md)
pour le mapping canonique par outil et
[`docs/MCP_TOOLS_SPEC.md`](docs/MCP_TOOLS_SPEC.md)
pour la spécification complète.

### System

| Outil           | Paramètres | Description                                              |
| --------------- | ---------- | -------------------------------------------------------- |
| `system_health` | —          | Statut de santé (S3, LLM, nombre de spaces)              |
| `system_whoami` | —          | Identité du token courant (nom, permissions, spaces)     |
| `system_about`  | —          | Identité du service (version, outils, capacités)         |

### Space

| Outil                | Paramètres                                   | Description                                                  |
| -------------------- | -------------------------------------------- | ------------------------------------------------------------ |
| `space_create`       | `space_id`, `description`, `rules?`, `owner?` | **manage** : crée un space ; `_meta.json` commité en dernier et manager persistant auto-invité |
| `space_update`       | `space_id`, `description?`, `owner?`         | Met à jour la description et/ou l'owner                      |
| `space_update_rules` | `space_id`, `rules`                          | Met à jour les rules du space (manage)                        |
| `space_list`         | —                                            | Liste les spaces accessibles par le token courant            |
| `space_info`         | `space_id`                                   | Infos détaillées (notes, bank, consolidation)                |
| `space_rules`        | `space_id`                                   | Lit les rules courantes du space                             |
| `space_summary`      | `space_id`                                   | Résumé complet : rules + bank + stats (démarrage agent)      |
| `space_export`       | `space_id`                                   | Export tar.gz en base64                                      |
| `space_delete`       | `space_id`, `confirm`, `unsafe_recovery?`    | Supprime le space (⚠️ irréversible, manage ; flag avancé pour recovery Hivemind partagé/unsafe classifiable, jamais un état corrompu) |
| `space_invite_token` | `space_id`, `token_hash`                     | **manage + accès** : hash canonique exact, add-only/idempotent ; distinct de l'enrollment Project Mesh |

### Token

| Outil | Paramètres | Description |
| --- | --- | --- |
| `token_create` | `name`, `permissions`, `expires_in_days?`, `email?` | **manage** : crée un token `read`, `read,write` ou `read,write,manage` avec `space_ids: []` ; secret + hash complet affichés une fois |

`manage` est un rôle de provisioning transitif et de confiance élevée. Tout
manager peut créer de nouveaux spaces globalement, même avec une allowlist vide,
et créer d'autres managers. L'allowlist borne l'accès et les invitations aux
spaces existants, pas `space_create`. Le cycle de vie global des tokens reste
admin uniquement.

La réutilisation d'identité est fail-closed : toute référence persistée vers un
identifiant absent ou partiellement préparé — y compris sur un admin, le manager
créateur ou des tokens révoqués/expirés — bloque `space_create` jusqu'au
nettoyage admin explicite. `space_delete` supprime/reprobe le payload puis
`_meta.json` en dernier, mais l'opérateur doit mettre en quiescence toutes les
mutations et tâches de fond du space : le verrou lifecycle n'est pas une
barrière universelle. Une suppression `partial` exige une recovery et n'est
jamais un succès.

### `short` — notes live (historiquement `live_*`)

| Outil         | Paramètres                                  | Description                                                                                                                |
| ------------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `live_note`   | `space_id`, `category`, `content`, `tags?`  | Écrit une note horodatée (agent = nom du token). Catégories : observation, decision, todo, insight, question, progress, issue |
| `live_read`   | `space_id`, `limit?`, `category?`, `agent?` | Lit les notes live (filtres optionnels)                                                                                    |
| `live_search` | `space_id`, `query`, `limit?`               | Recherche full-text dans les notes                                                                                         |

### `mid` — memory bank (historiquement `bank_*`)

| Outil                       | Paramètres                        | Description                                                                                                       |
| --------------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `bank_read`                 | `space_id`, `filename`            | Lit un fichier bank (supporte les sous-dossiers)                                                                  |
| `bank_read_all`             | `space_id`                        | Lit toute la bank en une requête (démarrage agent)                                                                |
| `bank_list`                 | `space_id`                        | Liste les fichiers bank avec chemins relatifs (sans contenu)                                                      |
| `bank_consolidate`          | `space_id`, `agent?`              | Enfile une consolidation LLM async. `agent` omis/null = caller ; `agent=""` explicite = tous les agents (manage/admin). Appeler une seule fois ; ne pas surveiller/poller sauf demande explicite |
| `bank_consolidation_status` | `job_id`                          | Check de statut manuel uniquement pour un job retourné par `bank_consolidate`                                     |
| `bank_consolidation_queues` | `space_ids?`                      | Résumé read-only des files de consolidation par space                                                             |
| `bank_stale_spaces`         | `min_notes?=5`, `min_age_days?=5`, `space_ids?` | Liste les spaces avec ≥N notes non consolidées dont la plus ancienne a ≥D jours (supervision)        |
| `bank_compact`              | `space_id`, `dry_run?`            | Compacte les fichiers bank surdimensionnés via LLM. `dry_run=True` par défaut (**manage**)                        |
| `bank_repair`               | `space_id`, `dry_run?`            | Répare les noms de fichiers corrompus (Unicode, préfixes parasites). `dry_run=True` par défaut (**manage**)       |
| `bank_write`                | `space_id`, `filename`, `content` | Écrit/remplace un fichier bank directement — contourne la consolidation LLM (**manage**)                         |
| `bank_delete`               | `space_id`, `filename`, `confirm?=False` | Supprime un fichier bank et ses doublons Unicode (**manage**, irréversible) ; `confirm=True` est requis |

### `long` — ontologie / graphe de connaissances (historiquement `graph_*`)

| Outil              | Paramètres                                           | Description                                                                                                  |
| ------------------ | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `graph_connect`    | `space_id`, `url`, `token`, `memory_id`, `ontology?` | Attache le moteur du tier `long` à un space. Teste la connexion, crée la mémoire de connaissances si besoin. Alias cible `long_connect`. |
| `graph_push`       | `space_id`, `include_volatile?`                      | Ingère le contenu canonique `mid` dans le graphe `long`. Delete + re-ingest, nettoyage orphelins. **Pas un canal bidirectionnel de routine** — voir plus bas. |
| `graph_status`     | `space_id`, `include_graph?`                         | Statut de connexion + stats du graphe (documents, entités, relations, top entités) ; détail du graphe optionnel |
| `graph_disconnect` | `space_id`, `use_embedded?`                          | Détache le moteur `long`, ou revient au runtime embarqué avec `use_embedded=true` (**manage**) ; les données restent intactes. Alias `long_disconnect`. |
| `long_ingest`      | `space_id`, `documents`, `mode?`, `include_volatile?` | Planifie l'ingestion de documents canoniques ; `apply` reste différé en V1. Outil direct, sans jumeau `graph_*`. |
| `long_query`       | `space_id`, `query`, `limit?`                        | Requête sémantique read-only sur le moteur long dérivé. Outil direct, sans jumeau `graph_*`.                |

### Backup

| Outil             | Paramètres                       | Description                                       |
| ----------------- | -------------------------------- | ------------------------------------------------- |
| `backup_create`   | `space_id`, `description?`       | Crée un snapshot complet sur S3                   |
| `backup_list`     | `space_id?`                      | Liste les backups disponibles                     |
| `backup_restore`  | `backup_id`, `confirm?=False`, `unsafe_recovery?=False` | **manage** ; `confirm=True` est toujours requis. Normalement le space ne doit pas exister. Sur un space partagé/non sûr, ajouter `unsafe_recovery=True` pour la recovery explicite en avant ; la corruption reste refusée fail-closed. |
| `backup_download` | `backup_id`                      | Télécharge en tar.gz base64                       |
| `backup_delete`   | `backup_id`, `confirm?=False`    | **manage** ; supprime irréversiblement un backup uniquement avec `confirm=True` |

### Admin

| Outil                | Paramètres                                                        | Description                                                                                                    |
| -------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `admin_audit_recent` | `limit?=50`                                                       | Admin uniquement, événements console/auth newest-first du buffer en mémoire par instance (`1..500`) ; métadonnées et clés d'arguments seulement, jamais les valeurs ni les IDs de space |
| `admin_create_token` | `name`, `permissions`, `space_ids?`, `expires_in_days?`, `email?` | Création globale admin/bootstrap ; scopes initiaux seulement pour une cible non-admin, cible admin persistée avec `[]` ; secret + hash complet affichés une fois |
| `admin_list_tokens`  | —                                                                 | Liste les tokens actifs                                                                                        |
| `admin_revoke_token` | `token_hash`                                                      | Révoque un token (le rend inutilisable)                                                                        |
| `admin_delete_token` | `token_hash`                                                      | Supprime physiquement un token du registre (⚠️ irréversible)                                                  |
| `admin_purge_tokens` | `revoked_only?`, `confirm?=False`                                 | Purge en masse : révoqués seuls par défaut ; tous les tokens exigent une confirmation explicite               |
| `admin_update_token` | `token_hash`, `permissions?`, `email?`, `space_ids?` ou `space_ids_add?` / `space_ids_remove?` | Mise à jour unitaire ; remplacement et delta des scopes sont exclusifs ; promotion efface les scopes et downgrade repart vide |
| `admin_bulk_update_tokens` | `names?`, `name_contains?`, `has_space?`, `permissions?`, `email?`, `space_ids_add?`, `space_ids_remove?`, `include_revoked?` | Mise à jour de masse filtrée ; scopes en delta add/remove uniquement                                          |
| `admin_gc_notes`     | `space_id?`, `max_age_days?`, `confirm?`, `delete_only?`, `expected_eligible_set_token?` | Garbage Collector : dry-run, consolidation ou suppression conditionnelle des notes orphelines                  |

Les écritures GC sont routées en fail-closed : chaque space candidat doit se
résoudre en `DIRECT_LOCAL` avant toute notice, consolidation ou suppression.
La suppression est explicitement en deux étapes : lancez d'abord un dry-run et
conservez son `eligible_set_token` opaque, puis renvoyez cette valeur comme
`expected_eligible_set_token` avec `confirm=true, delete_only=true`. Toute
dérive de l'ensemble exact des clés est refusée sans suppression ; une opération
partielle retourne les comptes réels traités/supprimés/échoués au lieu d'annoncer
un succès complet.

### Alias de compatibilité

Les alias canoniques de tier `short_*` / `mid_*` / `long_*` (jeu exact suivi
par [`tests/fixtures/tool_surface.json`](tests/fixtures/tool_surface.json) et
documenté par outil dans
[`docs/TOOL_MAPPING.md`](docs/TOOL_MAPPING.md)) sont
**enregistrés et appelables** — un mince
ré-enregistrement de la fonction *identique*, jamais une copie divergente.
Les noms historiques `live_*` / `bank_*` / `graph_*` sont des **alias de
compatibilité supportés indéfiniment** : aucune date de retrait, aucun
timer. Les appelants utilisant les noms historiques continuent de
fonctionner sans changement.

Les nouvelles intégrations devraient préférer `short_*` / `mid_*` /
`long_*` — c'est la grammaire recommandée pour l'avenir. Tout retrait
futur est **gated par ADR** (ADR-0005) : une dépréciation Stage B d'au
moins une release publique doit précéder tout retrait Stage C, et aucun
retrait n'est jamais déclenché automatiquement par date ou numéro de
release. Pour la politique complète, voir
[`docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations`](docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations).

- Politique (ADR-0005) : [`docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations`](docs/MCP_TOOLS_SPEC.md#compatibility--deprecation-expectations)
- Mapping par outil : [`docs/TOOL_MAPPING.md`](docs/TOOL_MAPPING.md)
- Grammaire et sémantique des tiers (ADR-0002) : [`docs/TOOL_MAPPING.md#invariants`](docs/TOOL_MAPPING.md#invariants)

---

## 🌐 Tier long — ontologie / graphe de connaissances

Le tier `long` est un **moteur d'ontologie / graphe de connaissances**
lié au space en interne. Il extrait des entités et relations typées du
contenu canonique et les stocke dans un graphe de connaissances
navigable pour du **rappel associatif** — le moment « comprendre ».

> ### Frontière d'autorité (non négociable)
>
> Le tier `long` est une **projection sémantique dérivée uniquement**.
> Il n'est **jamais** la source de validité de commit, de rollback,
> d'audit, de tombstones, de watermarks ou de recovery, et se situe
> **en dehors du chemin de commit**. `mid` (la bank consolidée) et les
> fichiers canoniques du dépôt restent l'autorité ; le graphe `long`
> localise et associe, il ne confirme pas.
>
> Cette frontière est dérivée du protocole. Voir le
> [mapping public des tiers et de l'autorité](docs/TOOL_MAPPING.md#invariants)
> (ADR-0002 / ADR-0004).

### `graph_push` est une ingestion, pas un canal de routine

`graph_push` est une **ingestion unidirectionnelle** du contenu
canonique `mid` dans le graphe `long` — pas une synchronisation
bidirectionnelle de routine. Pousser toute la bank à chaque cycle
enseigne au graphe du contenu transitoire qu'une compaction ultérieure
laissera bloqué en état obsolète. Les flux de routine doivent ingérer
des **documents stables et canoniques** avec des clés `source_path`
stables ; les fichiers de focus volatiles (ex. `activeContext.md`,
`progress.md`) ne doivent **jamais** finir dans le graphe `long`.
`graph_push` reste disponible pour un bootstrap one-shot et des
opérations de debug / migration explicites.

### Workflow

```
1. bank_consolidate(space_id)
   └─ Construit la bank mid canonique (appeler une seule fois ; ne pas poller sauf demande)

2. graph_push(space_id)
   ├─ La première push lie automatiquement le space au runtime long embarqué
   │  (memory_id dérivé du space_id, ontologie "general" — sans graph_connect)
   ├─ Pour chaque fichier canonique modifié : delete + re-ingest (recalcul du graphe)
   ├─ Nettoie les documents supprimés (entités orphelines retirées)
   └─ Met à jour les métriques d'ingestion (last_push, push_count)

3. graph_status(space_id)
   └─ Stats : entités, relations, top entités, documents...
```

Override avancé / diagnostic uniquement : `graph_connect(space_id, url,
token, memory_id, ontology?)` (canonique : `long_connect`) re-pointe un
space vers un moteur externe ou choisit une ontologie non par défaut —
jamais une étape requise du workflow de routine.

Chaque push est un **refresh complet** du graphe pour ce fichier : les
fichiers existants sont supprimés puis ré-ingérés pour que le moteur
recalcule les entités guidées par ontologie et les relations typées
avec le contenu à jour.

### Ontologies disponibles

| Ontologie           | Usage                                       |
| ------------------- | ------------------------------------------- |
| `general` (défaut)  | Polyvalente : FAQ, specs, certifications    |
| `legal`             | Documents juridiques, contrats              |
| `cloud`             | Infrastructure cloud, fiches produit        |
| `managed-services`  | Services managés, infogérance               |
| `presales`          | Avant-vente, RFP/RFI, propositions          |

---

## 🖥️ Interface web

> **Note** : le viewer temps réel `/live` décrit en premier est la surface de
> visualisation **héritée** livrée avec le moteur importé — documentée ici
> parce qu'elle accompagne le moteur, son avenir étant une décision
> d'observabilité distincte. La console opérateur `/admin` ci-dessous a été
> refondue au langage de design Hivemind et **constitue** la surface
> produit cible.

Hivemind expose une interface web sur `/live` pour visualiser les
spaces mémoire en temps réel.

### Accès

```
http://localhost:8080/live
```

### Fonctionnalités

| Zone                               | Contenu                                                                                                                       |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Dashboard** (gauche)             | Infos space, consolidation (date + compteurs), stats short/mid, agents colorés, catégories avec %, rules Markdown, statut tier long |
| **Live Timeline** (haut-droite)    | Notes `short` groupées par date (Aujourd'hui/Hier/date), cartes avec agent + catégorie + Markdown                            |
| **Bank Viewer** (bas-droite)       | Onglets de fichiers `mid` consolidés, rendu Markdown                                                                          |

### Auto-refresh intelligent

- Configurable : 3s / 5s / 10s / 30s / manuel
- **Anti-flicker** : ne re-render le DOM que si les données ont changé
- Point d'état avec timestamp du dernier refresh
- Sélection d'un space → chargement immédiat

### API REST (5 endpoints)

| Endpoint                        | Description                                              |
| ------------------------------- | -------------------------------------------------------- |
| `GET /api/spaces`               | Liste des spaces                                         |
| `GET /api/space/{id}`           | Infos complètes (meta + rules + stats + tier long)       |
| `GET /api/live/{id}`            | Notes `short` (filtres : `?agent=`, `?category=`, `?limit=`) |
| `GET /api/bank/{id}`            | Liste des fichiers bank `mid`                            |
| `GET /api/bank/{id}/{filename}` | Contenu d'un fichier bank `mid`                          |

Les endpoints `/api/*` nécessitent un Bearer Token. La page `/live` et
les fichiers `/static/*` sont publics.

### Console d'administration (`/admin`)

La console d'administration à routes hash est disponible sur `/admin` et
utilise le proxy authentifié `/api/tool` pour ses workflows opérateur :

```
http://localhost:8080/admin
```

| Section | Fonctionnalités |
| --- | --- |
| **Dashboard** | Statut de santé (S3 / LLM / version / uptime), barre d'identité, nombre de spaces et de tokens, signaux de file/lane de consolidation |
| **Spaces** | Index des spaces avec compteurs short/mid/long et labels d'état ; point d'entrée vers Space Detail (création visible seulement pour manage/admin) |
| **Space Detail** | Vue par space unifiée via sélecteurs de tier mémoire : notes `short`, fichiers bank `mid`, connaissances dérivées `long`, rules, résumé d'accès, et actions sûres par space (création/suppression de backups, suppression du space) |
| **Consolidation** | Lanes/jobs de consolidation (queued / running / succeeded / failed) et le filtre de planification stale-banks |
| **Audit** | Événements console/auth récents via `admin_audit_recent` (admin uniquement, cette instance, en mémoire depuis le redémarrage) |
| **Access** | Gestion des tokens et de l'accès aux spaces : création (manager-safe vs admin), invitation par hash exact, update / révocation / suppression / purge avec confirmations typées |
| **Outils opérateur** | Backups (création / restauration / suppression) et Maintenance (compact, repair, GC, purge) derrière des confirmations explicites |

- **Auth** : nécessite un token valide (comme `/live`), session via cookie HttpOnly
- **Compatible CSP** : zéro handler inline, tout via `data-action` + délégation d'événements
- **Le tier long est dérivé, jamais autoritaire** : jamais une source de commit,
  rollback, audit, appartenance ni recovery — le panneau long de Space Detail
  rend son état réel (ou un échec honnête), jamais un « désactivé » neutre.
- **Mono-tenant** : la liste `space_id` d'un token est une allowlist, pas une
  frontière de tenant (Hivemind OSS est mono-tenant).
- **Périmètre d'audit honnête** : la vue Audit est best-effort, non persistante
  et non exhaustive ; elle conserve uniquement les clés d'arguments (jamais
  les valeurs ni les IDs de space), et les appels d'outils MCP `/mcp` ne sont
  pas listés

---

## 🔌 Intégration MCP

> 📖 **Guides complets** : voir les guides d'intégration par client pour la
> configuration pas à pas (config serveur, custom instructions, workflow,
> multi-agents, troubleshooting) :
> [`CLAUDE_CODE_INTEGRATION.fr.md`](CLAUDE_CODE_INTEGRATION.fr.md),
> [`CODEX_INTEGRATION.fr.md`](CODEX_INTEGRATION.fr.md).

### Avec Claude Desktop

L'interface de connecteurs distants de Claude Desktop attend actuellement un
OAuth supporté et n'accepte pas le header bearer statique de Hivemind.
Anthropic ne documente pas non plus d'expansion sûre d'une variable
d'environnement pour ce header dans `claude_desktop_config.json`. En attendant
l'évolution d'un de ces contrats, utilisez Claude Code pour cet accès et ne
copiez jamais un token dans un dépôt. Voir la
[frontière Desktop précise](CLAUDE_CODE_INTEGRATION.fr.md#-avec-claude-desktop).

### Via Python (client MCP)

```python
import os

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async def example():
    headers = {"Authorization": f"Bearer {os.environ['HIVEMIND_TOKEN']}"}
    async with streamablehttp_client("http://localhost:8080/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            # Charger tout le contexte mid
            result = await session.call_tool("mid_read_all", {
                "space_id": "mon-projet"
            })

            # Écrire une note short
            await session.call_tool("short_note", {
                "space_id": "mon-projet",
                "category": "observation",
                "content": "Build passing en CI"
            })
```

---

## 💻 CLI et shell

### Installation de la CLI

```bash
uv sync --locked --dev
export MCP_URL=http://localhost:8080
export MCP_TOKEN=votre_token
```

### Commandes CLI (Click)

```bash
uv run python scripts/mcp_cli.py health
uv run python scripts/mcp_cli.py whoami                       # Identité du token courant
uv run python scripts/mcp_cli.py about
uv run python scripts/mcp_cli.py space list
uv run python scripts/mcp_cli.py space create mon-projet \
  --description "Mon projet" \
  --rules-file RULES/live-mem.standard.memory.bank.md
uv run python scripts/mcp_cli.py token create agent-cline -p read,write   # appelant manage/admin ; sans scope initial
uv run python scripts/mcp_cli.py space invite mon-projet sha256:<64-hex-minuscules>
uv run python scripts/mcp_cli.py live note mon-projet observation "Build OK"   # short
uv run python scripts/mcp_cli.py bank consolidate mon-projet --json            # ACK mid async ; arrêt ici
```

Pour ce parcours opérateur explicite, copiez le `job_id` retourné puis effectuez
un contrôle ultérieur choisi (jamais une boucle de polling automatique) :

```bash
uv run python scripts/mcp_cli.py bank consolidation-status <job_id> --json
```

N'exécutez la suite que lorsque ce contrôle indique l'état terminal
`"status": "succeeded"` ; `running`, `queued`, `failed`, `not_found` ou une
erreur impose l'arrêt avant toute lecture de bank ou push long.

```bash
uv run python scripts/mcp_cli.py bank read-all mon-projet                      # mid
uv run python scripts/mcp_cli.py graph push mon-projet    # long — la 1re push auto-bind au runtime embarqué
uv run python scripts/mcp_cli.py graph status mon-projet  # long — état connexion + stats graphe
uv run python scripts/mcp_cli.py graph query mon-projet "Build" --json
# Override avancé / diagnostic uniquement (moteur externe, ontologie non par défaut) :
uv run python scripts/mcp_cli.py graph connect mon-projet URL TOKEN MEM-ID -o general
uv run python scripts/mcp_cli.py graph disconnect mon-projet
# Maintenance : valide/provisionne le runtime embarqué puis remplace l'override
# sans supprimer les données du graphe distant ni ingérer de documents.
uv run python scripts/mcp_cli.py graph use-local mon-projet
```

### Shell interactif

```bash
uv run python scripts/mcp_cli.py shell
```

Autocomplétion, historique, affichage Rich. Voir
[scripts/README.md](scripts/README.md) pour la référence complète.

---

## 🧪 Tests

Script de tests unifié avec suites sélectionnables via `--suite` :

```bash
docker compose --profile dev up -d --wait   # Prérequis

# Toutes les suites
uv run python scripts/test_recette.py --url http://localhost:8080

# Une seule suite
uv run python scripts/test_recette.py --suite recette     # Pipeline agent
uv run python scripts/test_recette.py --suite isolation    # Isolation space-scope
uv run python scripts/test_recette.py --suite qualite      # Régression des outils MCP

# Suite tier long — exerce le chemin override explicite graph_connect
# contre un moteur long fourni par l'opérateur ; sautée sans
# --graph-url / --graph-token. Le chemin nominal embarqué (auto-bind) ne
# demande aucun flag (cf. quickstart : long_push se lie tout seul).
uv run python scripts/test_recette.py --suite graph \
  --graph-url http://host.docker.internal:8080 \
  --graph-token votre_token

# Lister les suites disponibles
uv run python scripts/test_recette.py --list
```

| Suite       | Description                                                                              |
| ----------- | ---------------------------------------------------------------------------------------- |
| `recette`   | Pipeline complet : token → notes `short` → consolidation LLM → bank `mid`                |
| `isolation` | Refus provisioning writer, create/auto-grant/invite manager, isolation cross-space, filtrage backup |
| `qualite`   | Régression des outils MCP : system, admin, space, short, mid, backup, GC                 |
| `graph`     | Chemin override explicite `graph_connect` du tier `long` : connect, push, status, disconnect (sautée sans `--graph-url`/`--graph-token`) |

La suite protocole Hivemind importée tourne aussi sous `pytest` :

```bash
uv run pytest tests/test_hivemind_state.py tests/test_hivemind_peer.py
uv run pytest tests
```

---

## 🔒 Sécurité

### Authentification

- **Bearer Token** obligatoire sur toutes les requêtes MCP
- **Clé bootstrap** pour créer le premier token admin
- **Tokens SHA-256** stockés sur S3 (jamais en clair)
- **Hiérarchie de permissions à 4 niveaux** : admin ⊃ manage ⊃ write ⊃ read
- **Portée par space** : l'allowlist `space_ids` par token est la **seule**
  primitive d'isolation (mono-tenant — voir
  [Ce que Hivemind ne revendique PAS (V1)](#-ce-que-hivemind-ne-revendique-pas-v1))
- **Frontière writer** : `write` ne mute que les spaces allowlistés ; aucune
  création de space/token ni extension d'accès
- **Frontière manager** : `manage` peut créer de nouveaux spaces arbitraires et
  déléguer des managers non-admin transitivement ; les invitations à un space
  existant restent bornées par l'allowlist de l'appelant (ADR-0022)

### WAF (Caddy + Coraza)

- **OWASP CRS** : injection SQL/XSS, path traversal, SSRF
- **Rate Limiting** : 600 événements HTTP MCP/min/IP (Streamable HTTP)
- **Edge Mesh** : activé par défaut, avec 120 requêtes pair/min et plafond brut 256 Kio avant
  Coraza ; l'authentification Ed25519 reste applicative
- **TLS automatique** : Let's Encrypt en production (`SITE_ADDRESS=domaine.com`)
- **Conteneur non-root** : utilisateur `mcp`

> Plusieurs acceptations de sécurité héritées supposaient un opérateur unique
> de confiance. Elles sont re-formulées comme responsabilités explicites du
> déployeur dans le contrat public de modèle de menace,
> [`docs/SECURITY.md`](docs/SECURITY.md), dans le périmètre OSS
> [mono-tenant](docs/EXTENSION_POINTS.md) (ADR-0003).

---

## 📂 Structure du projet

```
hivemind/
├── src/live_mem/              # Code source (outils MCP + interface web)
│   ├── server.py              # Serveur FastMCP + middlewares
│   ├── config.py              # Configuration pydantic-settings
│   ├── auth/                  # Authentification (check_access = isolation allowlist)
│   ├── static/                # /live (viewer hérité) + /admin (console opérateur)
│   ├── core/                  # Services métier
│   │   ├── storage.py         #   Stockage S3 durable
│   │   ├── space.py           #   CRUD des spaces mémoire
│   │   ├── live.py            #   Notes short (append-only)
│   │   ├── consolidator.py    #   Pipeline LLM de consolidation mid
│   │   ├── graph_bridge.py    #   Ingestion tier long (projection dérivée)
│   │   ├── tokens.py          #   Gestion des tokens SHA-256
│   │   ├── backup.py          #   Snapshots S3
│   │   └── ...
│   └── tools/                 # Outils MCP (8 modules)
│       ├── system.py          #   system_* (transverse)
│       ├── space.py           #   space_*  (transverse)
│       ├── access.py          #   token_create + space_invite_token (manage)
│       ├── live.py            #   live_*  → short_*
│       ├── bank.py            #   bank_*  → mid_*
│       ├── graph.py           #   graph_* → long_*
│       ├── backup.py          #   backup_* (transverse)
│       └── admin.py           #   admin_*  (transverse)
├── scripts/                   # CLI + Shell + Tests
├── waf/                       # Caddy + Coraza WAF
├── docs/                      # Déploiement, sécurité, migration, docs protocole/outils
├── assets/brand/              # Assets de marque (licence + provenance des hashs)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── uv.lock
```

> **Note** : le package source et plusieurs noms de modules portent
> encore l'identité importée `live_mem` / `graph_bridge`. La grammaire
> publique est une couche additive ; les renommages de source sont des
> travaux ultérieurs, pas des changements de comportement.

---

## 🔍 Troubleshooting

### Le service ne démarre pas

```bash
docker compose logs hivemind --tail 50
docker compose logs waf --tail 20
```

### 401 Unauthorized

- Vérifiez votre token : `Authorization: Bearer VOTRE_TOKEN`
- La clé bootstrap n'est pas un token — créez d'abord un token via
  `admin_create_token`

### La consolidation échoue

- Vérifiez les credentials LLM dans `.env`
- Le timeout par défaut est de 600s — augmentez `CONSOLIDATION_TIMEOUT`
  si besoin
- `bank_consolidate` retourne un accusé de job async (`running` ou
  `queued`) avec `next_action="return_to_user_without_polling"` ;
  appelez-le une seule fois et ne surveillez/pollez pas sauf demande
  explicite
- `bank_consolidation_status(job_id)` reste disponible pour des checks
  de statut manuels uniquement

---

## 🤝 Contribuer

Le développement se passe **via GitHub** — issues, pull requests et revues
de code. Les contrats d'architecture publics vivent dans
[`docs/ARCHITECTURE_CONTRACTS.md`](docs/ARCHITECTURE_CONTRACTS.md),
[`docs/MCP_TOOLS_SPEC.md`](docs/MCP_TOOLS_SPEC.md) et
[`docs/PROJECT_MESH.md`](docs/PROJECT_MESH.md). Lisez
[`CONTRIBUTING.md`](CONTRIBUTING.md) avant d'ouvrir une issue ou une pull request.

---

## 🗺️ Carte de la documentation

L'anglais constitue le contrat canonique ; les guides français préservent les
comportements critiques sans imposer une parité ligne à ligne.

| Besoin | Point de départ |
| --- | --- |
| Évaluer ou installer Hivemind | Ce README, puis [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| Connecter un agent | [`docs/AGENT_MEMORY_SETUP.md`](docs/AGENT_MEMORY_SETUP.md), puis le guide [Codex](CODEX_INTEGRATION.fr.md) ou [Claude Code](CLAUDE_CODE_INTEGRATION.fr.md) |
| Comprendre les outils et permissions | [`docs/MCP_TOOLS_SPEC.md`](docs/MCP_TOOLS_SPEC.md), [`docs/TOOL_EXPOSURE.md`](docs/TOOL_EXPOSURE.md) et [`scripts/README.fr.md`](scripts/README.fr.md) |
| Comprendre l'architecture et ses frontières | [`docs/ARCHITECTURE_CONTRACTS.md`](docs/ARCHITECTURE_CONTRACTS.md), [`docs/POSITIONING.md`](docs/POSITIONING.md) et [`docs/PROJECT_MESH.md`](docs/PROJECT_MESH.md) |
| Sécuriser, sauvegarder, restaurer ou migrer | [`docs/SECURITY.md`](docs/SECURITY.md), [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) et [`docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.fr.md`](docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.fr.md) |
| Dépanner ou demander de l'aide | [`FAQ.fr.md`](FAQ.fr.md), [`SUPPORT.md`](SUPPORT.md) et [`SECURITY.md`](SECURITY.md) pour signaler une vulnérabilité confidentiellement |
| Contribuer | [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) et [`CHANGELOG.md`](CHANGELOG.md) |

---

## 📄 Licence

Apache License 2.0

---

## 👤 Origine

Hivemind s'appuie sur des moteurs originalement développés par
**Christophe Lesur**. Le projet est publié sous licence Apache-2.0.

> L'identité de release publique est enregistrée dans [`VERSION`](VERSION) et
> le [`CHANGELOG.md`](CHANGELOG.md) public : le projet est publié sous le nom
> `hivemind` sur
> [github.com/Lesur-ai/hivemind](https://github.com/Lesur-ai/hivemind),
> versionné par le fichier `VERSION` du dépôt (SemVer).

---

*Hivemind — la couche mémoire ouverte pour la conscience collective des agents. `short · mid · long`.*
