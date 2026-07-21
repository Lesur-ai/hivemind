# 🔌 Guide d'intégration Hivemind avec Claude Code

> **Révision documentaire** : 2026-07-19

Ce guide connecte **Claude Code** aux tiers unifiés short, mid et long de
Hivemind via un seul endpoint MCP. Le contrat réutilisable multi-client est
documenté dans [Configure agents for unified Hivemind
memory](docs/AGENT_MEMORY_SETUP.md).

---

## 📋 Table des matières

- [Prérequis](#-prérequis)
- [Étape 1 — Démarrer Hivemind](#-étape-1--démarrer-hivemind)
- [Étape 2 — Créer un token pour Claude Code](#-étape-2--créer-un-token-pour-claude-code)
- [Étape 3 — Connecter Claude Code à Hivemind](#-étape-3--connecter-claude-code-à-hivemind)
- [Étape 4 — Créer un espace mémoire](#-étape-4--créer-un-espace-mémoire)
- [Étape 5 — Donner ses instructions à Claude Code](#-étape-5--donner-ses-instructions-à-claude-code)
- [Workflow recommandé](#-workflow-recommandé)
- [Multi-agent : Claude Code + Cline + autres clients supportés](#-multi-agent--claude-code--cline--autres-clients-supportés)
- [Troubleshooting](#-troubleshooting)
- [Avec Claude Desktop](#-avec-claude-desktop)
- [Récapitulatif](#-récapitulatif)

---

## 📦 Prérequis

| Composant            | Version            | Vérification                        |
| -------------------- | ------------------ | ----------------------------------- |
| **Docker**           | ≥ 24.0             | `docker --version`                  |
| **Docker Compose**   | ≥ 2.17.0           | `docker compose version`            |
| **Claude Code**      | ≥ 2.1              | `claude --version`                  |
| **Hivemind**      | Déployé et démarré | `curl http://localhost:8080/health` |

> 💡 Si Claude Code n'est pas installé : `npm install -g @anthropic-ai/claude-code` (macOS/Linux/Windows) ou utilisez l'installateur dédié — voir la documentation officielle Anthropic. Claude Code fournit la commande `claude` dans le terminal et propose des extensions IDE (VS Code, JetBrains) qui partagent la même configuration.

---

## 🚀 Étape 1 — Démarrer Hivemind

Si Hivemind n'est pas encore démarré :

```bash
cd /chemin/vers/hivemind
python scripts/configure_dev_env.py
uv sync --locked --dev
# Avant mid/long, configurer URL/clé fournisseur, modèle chat, modèle embeddings
# et dimension exacte décrits dans docs/DEPLOYMENT.md.
docker compose --profile dev up --build -d --wait
```

Le helper crée un fichier local mode `0600`, avec des secrets aléatoires,
MinIO local et Mesh désactivé. Il refuse d'écraser un `.env` existant. Pour un
déploiement réseau ou de production, configurez le template de production et
suivez plutôt le [guide de déploiement](docs/DEPLOYMENT.md).

**Vérification** :

```bash
# Avec S3 local sans LLM : HTTP 200 et « degraded ». Avec le LLM : « healthy ».
curl -fsS http://localhost:8080/health \
  | jq -e '.status == "healthy" or .status == "degraded"'
```

---

## 🔑 Étape 2 — Créer un token pour Claude Code

Claude Code a besoin d'un **nouveau Bearer Token dédié à cette identité
agent**, avec les permissions `read,write`. Ne réutilisez jamais un token
legacy Live Memory ou Graph Memory, un token administrateur, ou un token
partagé par plusieurs agents.

### Première installation vierge — initialiser le token opérateur

Sur la stack créée à l'étape 1, aucun token stocké n'existe encore. Utilisez
une seule fois le secret bootstrap généré pour créer le premier token
administrateur, puis remplacez-le dans le shell avant de créer le token Claude
Code :

```bash
cd /chemin/vers/hivemind
export MCP_URL=http://localhost:8080
export MCP_TOKEN="$(sed -n 's/^ADMIN_BOOTSTRAP_KEY=//p' .env)"
uv run python scripts/mcp_cli.py token create local-ops-admin \
  -p read,write,manage,admin --json

# Copier le token lm_... one-shot de la réponse JSON, puis remplacer cette valeur.
export MCP_TOKEN='lm_REMPLACER_PAR_LE_TOKEN_ADMIN_RETOURNE'
uv run python scripts/mcp_cli.py whoami --json
```

La première commande est routée vers `admin_create_token`, car l'appelant est
l'identité bootstrap. Conservez le secret et son hash canonique complet ; le
secret ne sera plus affiché. En production, récupérez le secret bootstrap dans
le gestionnaire de secrets plutôt que depuis un fichier local.

### Créer le token agent dédié via la CLI

```bash
cd /chemin/vers/hivemind
export MCP_TOKEN=<token_manage_ou_admin_de_confiance>

# Créer un token « read,write » pour Claude Code
uv run python scripts/mcp_cli.py token create claude-code-agent -p read,write
```

La CLI affichera quelque chose comme :

```
Token created successfully!
  Name   : claude-code-agent
  Token  : lm_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2
  Perms  : read, write

⚠️  This token will NEVER be displayed again. Copy it now!
```

> **⚠️ IMPORTANT** : copiez ce token immédiatement ! Il ne sera plus jamais affiché (seul le hash SHA-256 est stocké).
> Conservez également le `token_hash` exact retourné. Le nouveau token n'a
> aucun accès tant qu'un manager n'invite pas ce hash canonique complet.

### Pourquoi le secret bootstrap reste séparé

La clé bootstrap crée le premier admin via `admin_create_token` ; elle ne peut
pas appeler le `token_create` borné des managers. Ne configurez pas Claude Code
avec cette clé pour le travail courant. La séquence d'installation vierge
ci-dessus crée d'abord un admin ; l'appel CLI suivant utilise cet admin pour
créer le token `read,write` dédié.

---

## ⚙️ Étape 3 — Connecter Claude Code à Hivemind

Configurez une seule entrée MCP Hivemind. Le même endpoint fournit short, mid
et long ; n'ajoutez pas un serveur MCP Graph Memory séparé. Les portées, le
type HTTP, la substitution des variables et les unités de timeout ci-dessous
suivent la [documentation MCP officielle de Claude
Code](https://code.claude.com/docs/en/mcp) à jour.

Claude Code stocke sa configuration MCP dans un fichier JSON. Trois portées sont disponibles :

| Portée    | Emplacement                                    | Portée effective                   |
| --------- | ---------------------------------------------- | ---------------------------------- |
| `local`   | `~/.claude.json` (clé `projects.<cwd>`)        | Répertoire courant uniquement      |
| `user`    | `~/.claude.json` (clé `mcpServers` au top)     | Tous les projets de l'utilisateur  |
| `project` | `.mcp.json` à la racine du projet              | Commitée au repo (équipes)         |

La configuration recommandée est un `.mcp.json` de portée projet ne contenant
qu'une référence de variable d'environnement. Claude Code demande une
approbation avant de charger un serveur de portée projet : relisez le fichier
avant de l'approuver. Utilisez plutôt `local` ou `user` si l'endpoint lui-même
doit rester privé.

### 3.1 — Configuration projet alimentée par l'environnement (recommandée)

Créez `.mcp.json` à la racine du projet :

```json
{
  "mcpServers": {
    "hivemind": {
      "type": "http",
      "url": "https://votre-serveur/mcp",
      "headers": {
        "Authorization": "Bearer ${HIVEMIND_TOKEN}"
      }
    }
  }
}
```

Claude Code substitue `${HIVEMIND_TOKEN}` dans les headers HTTP. Définissez-la
dans l'environnement qui lance Claude Code sans l'inscrire dans l'historique :

```bash
printf 'Token Hivemind : '
IFS= read -r -s HIVEMIND_TOKEN
printf '\n'
export HIVEMIND_TOKEN
claude
```

Utilisez `http://localhost:8080/mcp` pour une instance locale. N'ajoutez pas de
valeur par défaut à `HIVEMIND_TOKEN` : un secret absent doit échouer visiblement.

### 3.2 — Portée privée local ou user

Les entrées `local` et `user` vivent dans `~/.claude.json`. La commande
`claude mcp add` accepte `--header`, mais persiste ce header dans ce fichier
privé. N'utilisez ce mode que si votre politique de secrets autorise un
credential en clair dans un fichier utilisateur ; la méthode projet ci-dessus
garde le credential hors de la configuration et du dépôt.

### 3.3 — Vérifier la connexion

Après configuration :

```bash
claude mcp list
```

Vous devriez voir `hivemind` avec un statut connecté. Lancez ensuite Claude
Code dans le projet et demandez :

> *« Appelle `system_whoami` sur hivemind et montre-moi le nom de l'agent, ses
> permissions et les spaces attribués. »*

Vérifiez l'identité dédiée, les permissions `read,write` et le space attendu.
Appelez ensuite `space_rules`, `mid_read_all` et `short_read` pour ce space.
L'endpoint public `/health` ne vérifie que la joignabilité, pas le token. Un
token invalide doit laisser le serveur en échec dans `/mcp` et produire HTTP
401 sur `/mcp`.

### 3.4 — Whitelister les outils (éviter les prompts de permission)

Claude Code demande confirmation à chaque appel d'outil MCP non autorisé. Pour éviter ces interruptions, ajoutez les outils Hivemind à l'allow-list du projet (ou de l'utilisateur).

Créez ou éditez `.claude/settings.local.json` à la racine du projet :

```json
{
  "permissions": {
    "allow": [
      "mcp__hivemind__space_list",
      "mcp__hivemind__space_info",
      "mcp__hivemind__space_rules",
      "mcp__hivemind__mid_read_all",
      "mcp__hivemind__mid_read",
      "mcp__hivemind__short_read",
      "mcp__hivemind__short_note",
      "mcp__hivemind__short_search",
      "mcp__hivemind__mid_consolidate",
      "mcp__hivemind__long_query",
      "mcp__hivemind__long_status",
      "mcp__hivemind__bank_consolidation_status",
      "mcp__hivemind__system_health",
      "mcp__hivemind__system_whoami"
    ]
  }
}
```

> 💡 **Convention de nommage** : Claude Code expose chaque outil MCP sous la forme `mcp__<nom-serveur>__<nom-outil>`. Si vous avez nommé votre serveur `hivemind-prod` à l'étape 3.1, ajustez le préfixe en conséquence.

Alternative interactive : tapez `/permissions` dans une session Claude Code pour ouvrir l'éditeur de permissions.

Pour une configuration globale (tous projets), utilisez `~/.claude/settings.json` à la place.

### 3.5 — Serveur HTTPS distant

Pour un déploiement en production, l'URL et le bloc JSON sont identiques — seul le schéma change (`https://` au lieu de `http://`). Aucune option supplémentaire requise côté Claude Code.

---

## 📁 Étape 4 — Créer un espace mémoire

Avant que Claude Code puisse écrire des notes, un provisioner de confiance doit
créer un **espace mémoire** avec ses **rules** et inviter le token writer. Les
sessions ordinaires `read,write` ne découvrent pas `space_create` ; une session
de provisionnement `manage` ou `admin` distincte découvre et peut invoquer le
flux complet `space_create` → `token_create` → `space_invite_token`. La CLI ou
la console Admin authentifiée permettent également ce provisionnement.

### Via la CLI

```bash
uv run python scripts/mcp_cli.py space create mon-projet \
  --rules-file ./RULES/live-mem.standard.memory.bank.md \
  -d "Mon projet de développement"

uv run python scripts/mcp_cli.py space invite mon-projet \
  sha256:<64-hex-minuscules-exacts-de-l-etape-2>
```

Plusieurs templates de rules sont fournis dans le dossier `RULES/` du repo :

| Template                                  | Cas d'usage                                           |
| ----------------------------------------- | ----------------------------------------------------- |
| `RULES/live-mem.standard.memory.bank.md`  | Memory Bank projet standard à six fichiers            |
| `RULES/product.management.memory.bank.md` | Équipe produit (vision, portfolio, personas, features) |
| `RULES/medical.memory.bank.md`            | Organisation non clinique de notes de santé ; vérification humaine requise |
| `RULES/presales.memory.bank.md`           | Avant-vente, qualification de prospect, RFP           |
| `RULES/book.memory.bank.md`               | Écriture de livre / projet éditorial                  |

Vous pouvez aussi utiliser le workflow de spaces de la console Admin
authentifiée, puis inviter le hash exact du token Claude Code. Gardez les
sessions Claude Code courantes sur l'identifiant `read,write` invité.

### Exemple de rules standards

```markdown
# Memory Bank Rules

## Fichiers à maintenir

### projectbrief.md
Vision, objectifs, périmètre du projet.

### activeContext.md
Focus courant, travail en cours, décisions récentes, prochaines étapes.

### progress.md
Ce qui marche, ce qui reste à faire, problèmes connus.

### techContext.md
Technologies utilisées, configuration, contraintes techniques.

### systemPatterns.md
Architecture, patterns, décisions techniques, composants.

### productContext.md
Pourquoi ce projet existe, problèmes résolus, expérience utilisateur.
```

---

## 📝 Étape 5 — Donner ses instructions à Claude Code

Hivemind inclut déjà le tier `long` d'ontologie/graphe de connaissances derrière
le même endpoint et le même `space_id`. Pour la hiérarchie des sources, le gate
de démarrage fail-closed, la politique de lookup long et la réécriture des
workflows, utilisez le [guide agent canonique](docs/AGENT_MEMORY_SETUP.md).

Claude Code lit automatiquement les fichiers `CLAUDE.md` au démarrage. Deux emplacements possibles :

| Emplacement                | Portée                                              | Recommandé pour                       |
| -------------------------- | --------------------------------------------------- | ------------------------------------- |
| `<racine-projet>/CLAUDE.md` | Le projet courant (committé avec le repo)          | Workflow spécifique au projet         |
| `~/.claude/CLAUDE.md`      | Tous les projets de l'utilisateur courant (privé)   | Préférences globales, identité, style |

Pour Hivemind, le `CLAUDE.md` au niveau projet est l'emplacement idéal car `{SPACE}` est spécifique au projet.

### Template recommandé (à coller dans `CLAUDE.md`)

Ce template utilise le placeholder `{SPACE}` — vous n'avez qu'**une seule valeur** à configurer :

```markdown
# Memory Bank — Hivemind MCP

Hivemind est ma mémoire partagée canonique entre agents et sessions. Claude
Code dispose aussi du contexte local `CLAUDE.md` et de l'auto-memory ; je les
traite comme un contexte local non autoritatif et ne les utilise jamais pour
contourner le gate de démarrage Hivemind. Les fichiers du dépôt restent
l'autorité finale. Voir la [documentation mémoire officielle de Claude
Code](https://code.claude.com/docs/en/memory).

## 🔌 Configuration (à personnaliser par projet)

Ma mémoire persistante est gérée par le serveur MCP **Hivemind** (`hivemind`).

> **⚙️ La seule valeur à personnaliser :**
>
> - **SPACE** = `mon-projet`       ← Remplacez par votre space_id
>
> Toutes les instructions ci-dessous utilisent `{SPACE}` — je le remplace automatiquement par la valeur ci-dessus.
> Le nom d'agent est **auto-détecté** depuis le token d'authentification (aucune configuration nécessaire).

## 📖 Au début de CHAQUE tâche (OBLIGATOIRE)

1. Appeler `space_rules("{SPACE}")` pour lire les rules (structure de la bank)
2. Appeler `mid_read_all("{SPACE}")` pour charger TOUT le contexte consolidé
3. Appeler `short_read(space_id="{SPACE}")` pour lire les **notes non consolidées**
4. Lire attentivement le contenu avant de commencer
5. Identifier le focus courant dans `activeContext.md`

> ⚠️ NE JAMAIS commencer à travailler sans avoir lu la bank.
> Si un appel de démarrage échoue, expire, retourne un statut non-OK ou est
> indisponible, arrêter avant toute mutation. Ne pas substituer une mémoire
> locale ou un endpoint legacy.
>
> 💡 **Pourquoi lire les notes live ?** Entre les sessions, des notes peuvent avoir été écrites (par moi ou par d'autres agents) sans avoir été consolidées. Ces notes contiennent du contexte récent qui n'apparaît pas encore dans les fichiers bank. Les ignorer = risque de refaire un travail déjà fait ou de manquer une décision récente.

## 📝 Pendant le travail

Écrire des notes atomiques fréquentes via `short_note` :

    short_note(space_id="{SPACE}", category="<catégorie>", content="...")

Le paramètre `agent` est **auto-détecté** depuis le token — pas besoin de le passer.

**Catégories** :
- `observation` — constats factuels, résultats de commandes
- `decision` — choix techniques et leur justification
- `progress` — avancement, travail terminé
- `issue` — problèmes rencontrés, bugs
- `todo` — tâches identifiées à faire
- `insight` — apprentissages, patterns découverts
- `question` — points à clarifier, décisions en attente

Utilisez `long_query` pour le contexte historique ou transverse, puis relisez
le fichier canonique du dépôt avant d'agir. La mémoire long est dérivée et non
autoritative. N'exécutez pas `long_push`, ne modifiez pas les bindings et
n'ingérez pas de documents en routine de fin de session. N'ingérez jamais
`activeContext.md`, `progress.md` ni de résumés mid bruts.

## 🧠 En fin de session (ou après un bloc de travail significatif)

Uniquement si de nouvelles notes significatives existent, valider le résumé
avec l'utilisateur sauf si les instructions actives exigent une consolidation
immédiate, puis appeler :

    mid_consolidate(space_id="{SPACE}")

Le LLM va consolider **mes propres notes** (agent auto-détecté depuis le token) en mettant à jour les fichiers bank selon les rules du space.

> ℹ️ `agent` omis/null signifie toujours vos propres notes. Seul un caller
> manage/admin peut explicitement consolider toutes les notes avec `agent=""`.
>
> 🔕 `mid_consolidate` est **fire-and-forget** : il retourne un accusé async (`running` / `queued`) avec `next_action="return_to_user_without_polling"`. **Appelez-le une seule fois et rendez la main à l'utilisateur.** Ne surveillez pas et ne pollez pas. `bank_consolidation_status(job_id)` existe uniquement pour des **checks manuels explicites**.

## ⚠️ Règles strictes

1. **NE JAMAIS écrire directement dans la bank** — seule la consolidation LLM le fait
2. **Toujours passer `space_id="{SPACE}"`** dans chaque appel
3. **Écrire des notes atomiques après chaque étape significative** — 1 note = 1 fait, 1 décision, ou 1 tâche
4. **Consolider uniquement un travail significatif** — après validation utilisateur sauf instruction active explicite, appeler `mid_consolidate` au plus une fois puis rendre la main sans polling ni relecture immédiate
5. **Lire la bank au démarrage** — ne jamais travailler sans contexte
6. **Utiliser un seul endpoint Hivemind** — ne jamais substituer un service legacy
7. **Garder les secrets hors des instructions** — tokens et URLs restent dans la configuration MCP

## 🔄 Quand demander une mise à jour

Si l'utilisateur dit **« update memory bank »** :
1. Écrire des notes `short_note` résumant l'état actuel du travail
2. Appeler `mid_consolidate(space_id="{SPACE}")`
3. Après confirmation explicite de fin, vérifier éventuellement avec `mid_read_all("{SPACE}")`

## 📊 Commandes utiles

| Action                          | Commande                                                                  |
| ------------------------------- | ------------------------------------------------------------------------- |
| Lire le contexte complet        | `mid_read_all("{SPACE}")`                                                |
| Lire les rules                  | `space_rules("{SPACE}")`                                                  |
| Écrire une note                 | `short_note(space_id="{SPACE}", category="...", content="...")`            |
| Consolider                      | `mid_consolidate(space_id="{SPACE}")`                                    |
| Voir les notes récentes         | `short_read(space_id="{SPACE}")`                                           |
| Voir les notes d'un autre agent | `short_read(space_id="{SPACE}", agent="autre-agent")`                      |
| Infos space                     | `space_info("{SPACE}")`                                                   |
```

> 💡 **Pour un nouveau projet** : copiez ce fichier dans `<racine-projet>/CLAUDE.md`, changez la ligne `SPACE`, et c'est tout !

### Version minimaliste (`~/.claude/CLAUDE.md` global)

Si vous préférez ne pas committer les instructions Hivemind dans chaque projet, ajoutez ce court bloc à `~/.claude/CLAUDE.md` :

```
Vous avez accès à Hivemind (serveur MCP « hivemind »).
- Au démarrage : space_rules("{SPACE}"), mid_read_all("{SPACE}"), short_read(space_id="{SPACE}")
- Pendant le travail : short_note(space_id="{SPACE}", category="...", content="...")
- Après des notes significatives et validation utilisateur (sauf instruction active immédiate) : appeler mid_consolidate(space_id="{SPACE}") au plus une fois, puis rendre la main sans polling ni relecture
`{SPACE}` est défini dans le CLAUDE.md du projet courant. L'agent est auto-détecté depuis le token.
```

Chaque projet déclare alors uniquement sa valeur `{SPACE}` dans son propre `CLAUDE.md`.

---

## 🔄 Workflow recommandé

### Workflow type d'une session de développement

```
┌────────────────────────────────────────────────┐
│  1. DÉMARRAGE                                  │
│     space_rules("mon-projet")                  │
│     mid_read_all("mon-projet")                │
│     short_read(space_id="mon-projet")           │
│     → Claude lit rules + bank + notes live     │
├────────────────────────────────────────────────┤
│  2. TRAVAIL (boucle)                           │
│     • Claude code, analyse, répond             │
│     • short_note(space_id="mon-projet", …)       │
├────────────────────────────────────────────────┤
│  3. APRÈS TRAVAIL SIGNIFICATIF VALIDÉ          │
│     mid_consolidate(space_id="mon-projet")    │
│     → Le LLM synthétise les notes dans la bank │
│     → Les notes live sont supprimées si OK     │
└────────────────────────────────────────────────┘
```

### Décision de consolidation

| Situation | Recommandation |
| --- | --- |
| Aucune nouvelle note significative | Ne pas consolider |
| Notes significatives, sans instruction immédiate explicite | Valider le résumé avec l'utilisateur, puis enqueue au plus une fois |
| Une instruction active exige explicitement la consolidation immédiate | Enqueue au plus une fois, puis rendre la main sans polling ni relecture |
| L'utilisateur demande explicitement le statut du job | Faire un seul check avec `bank_consolidation_status(job_id)` |

### Visualisation en temps réel

Pendant que Claude Code travaille, ouvrez l'interface web pour suivre en direct :

```
http://localhost:8080/live
```

Les notes apparaîtront en temps réel dans la **Live Timeline** et la **Bank** se mettra à jour après chaque consolidation.

---

## 👥 Multi-agent : Claude Code + Cline + autres clients supportés

Hivemind permet à **plusieurs agents** de collaborer sur le même espace mémoire.

### Scénario : Claude Code (développement) + Cline (review)

Pour que plusieurs agents collaborent, créez **un token par identité** :

1. Avec un manager, appeler `token_create` pour `claude-code-dev` et
   `cline-review`.
2. Conserver chaque secret et son hash complet exact de la réponse one-shot.
3. Inviter chaque hash au space partagé via `space_invite_token`.
4. Configurer chaque agent avec son propre token `read,write`.

L'identité de l'agent est **automatiquement dérivée de son token** chaque fois qu'il appelle `short_note` ou `mid_consolidate`. Aucun paramètre `agent` à passer.

### Communication inter-agents

Les agents ne se parlent pas directement. Ils communiquent **via l'espace partagé** :

```
Claude Code   → short_note(space_id="mon-projet", category="question", content="Faut-il supporter le CSV ?")
Cline         → short_read(space_id="mon-projet", category="question")   ← voit la question
Cline         → short_note(space_id="mon-projet", category="decision", content="Non, JSON uniquement")
Claude Code   → short_read(space_id="mon-projet", category="decision")   ← voit la réponse
```

### Consolidation par agent

Chaque agent consolide **ses propres notes** sans interférer avec les autres. Un
agent manage/admin peut explicitement consolider toutes les notes avec
`mid_consolidate(space_id="mon-projet", agent="")` ; l'appel par défaut reste
limité au caller.

---

## 🔍 Troubleshooting

### `claude mcp list` n'affiche pas hivemind

1. Vérifiez que le serveur est lancé : `curl http://localhost:8080/health`
2. Vérifiez le `.mcp.json` projet, ou `~/.claude.json` pour les portées
   local/user (pas de virgule traînante, accolades fermées)
3. Quittez complètement Claude Code et relancez — le fichier n'est lu qu'au démarrage
4. Inspectez les logs : `claude --debug` puis lancez une session courte

### Erreur « 401 Unauthorized »

- Le token est incorrect, expiré ou révoqué
- Vérifiez que `HIVEMIND_TOKEN` est défini dans l'environnement qui a lancé
  Claude Code et commence par `lm_`
- Exécutez `claude mcp list`, inspectez `/mcp`, puis appelez `system_whoami`
- N'utilisez jamais la clé bootstrap pour l'accès agent courant

### Erreur « Access denied to space »

Le token est restreint à certains spaces (`space_ids`). Soit :
- Demandez à un manager ayant accès d'appeler
  `space_invite_token(space_id="mon-projet", token_hash="sha256:<64 hex minuscules>")`
  avec le hash canonique complet exact.
- Ou demandez à un admin de mettre le token à jour globalement.

### Claude Code demande une permission à chaque appel

Whitelistez les outils via `.claude/settings.local.json` (voir Étape 3.4), ou tapez `/permissions` en session pour les ajouter interactivement.

### Claude Code n'utilise pas Hivemind tout seul

Sans un `CLAUDE.md` explicite, Claude Code ne sait pas qu'il doit appeler ces outils au démarrage d'une session. Ajoutez le template de l'étape 5 dans `<racine-projet>/CLAUDE.md` ou `~/.claude/CLAUDE.md`.

### MCP ne se connecte pas derrière un VPN ou un proxy

Si Hivemind est sur un serveur distant, vérifiez que :
- Le port 443 (HTTPS) ou 8080 (HTTP) est accessible
- L'URL dans la config Claude Code est correcte (avec `/mcp` à la fin)
- Confirmez le serveur dans `/mcp`, puis appelez `system_whoami` ; `/health`
  seul ne teste pas l'authentification

### Suivre une consolidation en cours

Ne la suivez pas automatiquement. `mid_consolidate` renvoie un accusé
asynchrone avec `job_id` et `next_action="return_to_user_without_polling"` ;
appelez-le une fois et rendez la main. Utilisez
`bank_consolidation_status(job_id)` uniquement pour une demande explicite de
statut. Si l'accusé lui-même time-out, diagnostiquez la connexion ou le serveur
et ne soumettez pas aveuglément un job en double.

---

## 🖥️ Avec Claude Desktop

Claude Desktop est une surface distincte de Claude Code :

- **UI de connector distant :** **Settings → Connectors → Add custom
  connector** passe par le cloud Anthropic et attend un serveur MCP public avec
  un OAuth supporté. Le mode bearer statique actuel de Hivemind ne peut pas être
  saisi comme header Authorization personnalisé dans cette UI.
- **Configuration Desktop locale :** `claude_desktop_config.json` est un
  mécanisme MCP local séparé. Anthropic ne documente pas la substitution de
  variables d'environnement dans ses headers bearer. Ne copiez pas de token
  Hivemind dans un dépôt et ne publiez pas d'exemple porteur d'un token.

Jusqu'à ce que Hivemind supporte le flux OAuth du connector ou qu'Anthropic
documente un fournisseur de secret sûr pour le HTTP local de Desktop, utilisez
Claude Code pour l'accès Hivemind par bearer. Ne réutilisez pas le JSON Claude
Code dans Desktop : une URL sans type de transport explicite est invalide, et
les champs `timeout` de `.mcp.json` côté Claude Code sont exprimés en
millisecondes (`600000` signifie dix minutes), pas en secondes. Ne supposez pas
qu'un champ Desktop non documenté suit le même contrat.

---

## 📊 Récapitulatif

| Étape     | Action                                                  | Temps      |
| --------- | ------------------------------------------------------- | ---------- |
| 1         | Démarrer Hivemind (`docker compose up -d`)           | 1 min      |
| 2         | Créer un token (`mcp_cli.py token create`)              | 30 sec     |
| 3         | Configurer Claude Code (`claude mcp add`)               | 1 min      |
| 3.4       | Whitelister les outils (`.claude/settings.local.json`)  | 1 min      |
| 4         | Le manager crée le space et invite le hash exact du token | 30 sec   |
| 5         | Ajouter le `CLAUDE.md` du projet                        | 2 min      |
| **Total** | **Prêt à l'emploi**                                     | **~6 min** |

---

*Guide d'intégration Hivemind ↔ Claude Code — [Documentation complète](README.fr.md)*
