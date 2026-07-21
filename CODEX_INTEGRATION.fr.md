# 🔌 Guide d'intégration Hivemind pour OpenAI Codex

> **Révision documentaire** : 2026-07-19

Ce guide connecte **OpenAI Codex** aux tiers unifiés short, mid et long de
Hivemind via un seul endpoint MCP. Le contrat réutilisable multi-client est
documenté dans [Configure agents for unified Hivemind
memory](docs/AGENT_MEMORY_SETUP.md).

---

## 📋 Sommaire

- [Prérequis](#-prérequis)
- [Étape 1 — Obtenir un token Hivemind](#-étape-1--obtenir-un-token-hivemind)
- [Étape 2 — Configurer Codex via `.codex/config.toml`](#-étape-2--configurer-codex-via-codexconfigtoml)
- [Étape 3 — Créer un espace mémoire](#-étape-3--créer-un-espace-mémoire)
- [Étape 4 — Donner les instructions à Codex](#-étape-4--donner-les-instructions-à-codex)
- [Workflow recommandé](#-workflow-recommandé)
- [Troubleshooting](#-troubleshooting)

---

## 📦 Prérequis

| Composant          | Détail                                                              |
| ------------------ | ------------------------------------------------------------------- |
| **OpenAI Codex**   | CLI ou environnement supportant les serveurs MCP                    |
| **Hivemind**       | Instance Hivemind opérationnelle (auto-hébergée ou hébergée)        |
| **Bearer Token**   | Token `read,write` créé sur votre instance Hivemind              |

---

## 🔑 Étape 1 — Obtenir un token Hivemind

Codex a besoin d'un **nouveau Bearer Token dédié à cette identité agent**, avec
au minimum les permissions `read,write`. Ne réutilisez jamais un token legacy
Live Memory ou Graph Memory, un token administrateur, ou un token partagé par
plusieurs agents.

### Option A — Via la CLI

```bash
cd /chemin/vers/hivemind
export MCP_TOKEN=<token_manage_ou_admin_de_confiance>

# Créer un token « write » pour Codex
uv run python scripts/mcp_cli.py token create codex-agent -p read,write
```

La CLI affichera quelque chose comme :

```
Token created successfully!
  Name   : codex-agent
  Token  : lm_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2
  Perms  : read, write

⚠️  This token will NEVER be displayed again. Copy it now!
```

> **⚠️ IMPORTANT** : copiez ce token immédiatement ! Il ne sera plus jamais affiché (seul le hash SHA-256 est stocké).
> Conservez aussi le `token_hash` exact affiché. Le token démarre sans accès à
> aucun space ; un manager doit inviter ce hash complet à l'étape 3.

### Option B — Via la console d'administration

1. Ouvrez `https://<votre-instance-hivemind>/admin` dans votre navigateur
2. Connectez-vous avec un identifiant manage ou admin
3. Ouvrez **Access**
4. Cliquez sur **Create Token**, renseignez le nom (`codex-agent`), définissez les permissions sur `read,write`
5. Copiez le token affiché

### Option C — Instance Hivemind hébergée

Si vous utilisez une instance Hivemind hébergée, votre token a déjà été provisionné par l'opérateur. Utilisez-le directement — il ressemble à :

```
lm_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> ⚠️ Votre token est confidentiel. Ne l'incluez jamais dans la documentation et ne le commitez jamais dans un dépôt.

---

## ⚙️ Étape 2 — Configurer Codex via `.codex/config.toml`

Codex lit la configuration MCP depuis `~/.codex/config.toml` ou depuis
`.codex/config.toml` à la racine du projet. La configuration projet n'est
chargée qu'après avoir approuvé le projet ; relisez donc un dépôt avant de lui
faire confiance. Voir la [documentation MCP officielle de
Codex](https://learn.chatgpt.com/docs/extend/mcp) à jour.

### 2.1 Créer ou éditer le fichier de configuration

```bash
mkdir -p ~/.codex
# ou au niveau projet :
mkdir -p .codex
```

### 2.2 Ajouter le serveur Hivemind

Ouvrez `.codex/config.toml` et ajoutez la section suivante :

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "https://hivemind.example.com/mcp"
```

Exposez le token dédié dans l'environnement qui lance Codex, sans le saisir
dans l'historique du shell :

```bash
printf 'Token Hivemind : '
IFS= read -r -s HIVEMIND_TOKEN
printf '\n'
export HIVEMIND_TOKEN
codex
```

Ne placez pas le token lui-même dans `config.toml`, le dépôt ou les logs.
`bearer_token_env_var` demande à Codex de lire sa valeur à l'exécution et de
l'envoyer dans le header `Authorization: Bearer ...`.

> Conservez une seule entrée mémoire Hivemind pour ce projet. Le tier `long`
> passe par Hivemind ; n'ajoutez pas un second serveur MCP Graph Memory.

### 2.3 Exemple avec une instance hébergée

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "https://hivemind.example.com/mcp"
```

### 2.4 Exemple en instance auto-hébergée

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "https://hivemind.votre-domaine.com/mcp"
```

Pour une instance de développement locale :

```toml
[mcp_servers.my-hivemind]
bearer_token_env_var = "HIVEMIND_TOKEN"
enabled = true
url = "http://localhost:8080/mcp"
```

### 2.5 Où placer `config.toml`

| Portée          | Emplacement                          | Quand l'utiliser                       |
| --------------- | ------------------------------------ | -------------------------------------- |
| **Global**      | `~/.codex/config.toml`               | Tous les projets partagent le serveur  |
| **Par projet**  | `<racine-projet>/.codex/config.toml` | Configuration MCP par projet           |

> **Priorité** : la config par projet l'emporte sur la config globale si les deux existent.
> Un fichier projet n'est considéré que pour un projet approuvé. L'exemple peut
> être commité car il ne contient que le nom d'une variable d'environnement ;
> sa valeur reste un secret et ne doit jamais être commitée.

### 2.6 Vérifier la connexion

Après avoir enregistré `config.toml`, relancez Codex depuis un shell où
`HIVEMIND_TOKEN` est défini, puis vérifiez les trois niveaux :

```bash
codex mcp list
```

1. Dans Codex, utilisez `/mcp` et confirmez que `my-hivemind` est connecté.
2. Demandez à Codex d'appeler `system_whoami` sur `my-hivemind`. Vérifiez le
   nom de l'agent, les permissions `read,write` et le space attribué.
3. Demandez-lui d'appeler `space_rules`, `mid_read_all` et `short_read` sur ce
   space. Un `/health` vert prouve seulement que le serveur est joignable ; il
   ne valide **pas** le token.

Pour un test d'authentification négatif, un token invalide doit être refusé par
l'endpoint MCP :

```bash
curl -i -H 'Authorization: Bearer invalid' \
  https://hivemind.example.com/mcp
# Attendu : HTTP 401
```

---

## 📁 Étape 3 — Créer un espace mémoire

Avant que Codex puisse écrire des notes, un provisioner de confiance doit créer
un **espace mémoire** avec ses **rules** et inviter le token Codex. Les sessions
ordinaires `read,write` ne découvrent pas `space_create` ; une session de
provisionnement `manage` ou `admin` distincte découvre et peut invoquer le flux
complet `space_create` → `token_create` → `space_invite_token`. La CLI ou la
console Admin authentifiée permettent également ce provisionnement.

### Via la CLI Hivemind

```bash
uv run python scripts/mcp_cli.py space create mon-projet \
  --rules-file ./RULES/live-mem.standard.memory.bank.md \
  -d "Mon projet Codex"

uv run python scripts/mcp_cli.py space invite mon-projet \
  sha256:<64-hex-minuscules-exacts-de-l-etape-1>
```

Vous pouvez aussi utiliser le workflow de spaces de la console Admin
authentifiée, puis inviter le hash exact du token Codex. Gardez les sessions
Codex courantes sur l'identifiant `read,write` invité.

### Template de rules standard

```markdown
# Memory Bank Rules

## Fichiers à maintenir

### projectbrief.md
Vision, objectifs, périmètre du projet.

### activeContext.md
Focus courant, travail en cours, décisions récentes, prochaines étapes.

### progress.md
Ce qui marche, ce qui reste à construire, problèmes connus.

### techContext.md
Technologies utilisées, configuration, contraintes techniques.

### systemPatterns.md
Architecture, patterns, décisions techniques, composants.

### productContext.md
Pourquoi ce projet existe, problèmes résolus, expérience utilisateur.
```

---

## 📝 Étape 4 — Donner les instructions à Codex

Hivemind inclut déjà le tier `long` d'ontologie/graphe de connaissances derrière
le même endpoint et le même `space_id`. Pour la hiérarchie des sources, le gate
de démarrage fail-closed, la politique de lookup long et la réécriture des
workflows, utilisez le [guide agent canonique](docs/AGENT_MEMORY_SETUP.md).

Pour que Codex utilise automatiquement Hivemind, ajoutez des instructions dans un fichier `AGENTS.md` à la racine de votre projet (Codex le charge automatiquement comme instructions agent).

### 4.1 Template `AGENTS.md` recommandé

````markdown
# Instructions agent Codex — Hivemind MCP

Hivemind est ma mémoire partagée canonique entre agents et sessions. Codex peut
aussi maintenir une mémoire produit locale ; je la traite comme un contexte
local non autoritatif et ne l'utilise jamais pour contourner le gate de
démarrage Hivemind. Les fichiers du dépôt restent l'autorité finale.

## Configuration du serveur MCP

Ma mémoire persistante est gérée par le serveur MCP **Hivemind** (`my-hivemind`).

> **La seule valeur à personnaliser :**
> - **SPACE** = `mon-projet`  ← Remplacez par votre space_id
>
> Toutes les instructions ci-dessous utilisent `{SPACE}`. Le nom d'agent est auto-détecté depuis le token.

## Au début de CHAQUE tâche (OBLIGATOIRE)

1. Appeler `space_rules("{SPACE}")` pour lire les rules (structure de la bank)
2. Appeler `mid_read_all("{SPACE}")` pour charger TOUT le contexte consolidé
3. Appeler `short_read(space_id="{SPACE}")` pour lire les **notes non consolidées**
4. Lire attentivement le contenu avant de commencer
5. Identifier le focus courant dans `activeContext.md`

> ⚠️ NE JAMAIS commencer à travailler sans avoir lu la bank.
> Si un appel de démarrage échoue, expire, retourne un statut non-OK ou est
> indisponible, arrêter avant toute mutation. Ne pas substituer une mémoire
> locale ou un endpoint legacy.

## Pendant le travail

Écrire des notes atomiques fréquentes avec `short_note` :

```
short_note(space_id="{SPACE}", category="<catégorie>", content="...")
```

**Catégories** : `observation`, `decision`, `progress`, `issue`, `todo`, `insight`, `question`

Utilisez `long_query` pour le contexte historique ou transverse, puis relisez
le fichier canonique du dépôt avant d'agir. La mémoire long est dérivée et non
autoritative. N'exécutez pas `long_push`, ne modifiez pas les bindings et
n'ingérez pas de documents en routine de fin de session. N'ingérez jamais
`activeContext.md`, `progress.md` ni de résumés mid bruts.

## Après un travail significatif

Uniquement si de nouvelles notes significatives existent, valider le résumé
avec l'utilisateur sauf si les instructions actives exigent une consolidation
immédiate, puis appeler :

```
mid_consolidate(space_id="{SPACE}")
```

Cet appel par défaut consolide uniquement les notes du token courant. Un caller
manage/admin doit passer explicitement `agent=""` pour consolider toutes les notes.

> 🔕 `mid_consolidate` est **fire-and-forget** : il retourne un accusé async (`running` / `queued`) avec `next_action="return_to_user_without_polling"`. **Appelez-le une seule fois et rendez la main à l'utilisateur.** Ne surveillez pas et ne pollez pas. `bank_consolidation_status(job_id)` existe uniquement pour des **checks manuels explicites**.

## Règles obligatoires

1. **NE JAMAIS écrire directement dans la bank** — seule la consolidation LLM le fait
2. **Toujours passer `space_id="{SPACE}"`** dans chaque appel
3. **Écrire des notes atomiques après chaque étape significative** — 1 note = 1 fait, 1 décision, ou 1 tâche
4. **Consolider uniquement un travail significatif** — après validation utilisateur sauf instruction active explicite, appeler `mid_consolidate` au plus une fois puis rendre la main sans polling ni relecture immédiate
5. **Lire la bank au démarrage** — ne jamais travailler sans contexte
6. **Utiliser un seul endpoint Hivemind** — ne jamais substituer un service legacy
7. **Garder les secrets hors des instructions** — tokens et URLs restent dans la configuration MCP
````

### 4.2 Version minimaliste (prompt inline)

```
Vous avez accès à Hivemind (serveur MCP : my-hivemind).
- Au démarrage : space_rules("mon-projet"), mid_read_all("mon-projet"), short_read(space_id="mon-projet")
- Pendant le travail : short_note(space_id="mon-projet", category="...", content="...")
- Après des notes significatives et validation utilisateur (sauf instruction active immédiate) : appeler mid_consolidate(space_id="mon-projet") au plus une fois, puis rendre la main sans polling ni relecture
Le nom d'agent est auto-détecté depuis le token d'authentification.
```

---

## 🔄 Workflow recommandé

```
┌────────────────────────────────────────────────┐
│  1. DÉMARRAGE                                  │
│     space_rules("mon-projet")                  │
│     mid_read_all("mon-projet")                │
│     short_read(space_id="mon-projet")           │
│     → Codex lit rules + bank + notes live      │
├────────────────────────────────────────────────┤
│  2. TRAVAIL (boucle)                           │
│     • Codex code, analyse, répond              │
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

---

## 👥 Multi-agent : Codex + Cline + autres

Hivemind permet à **plusieurs agents** de collaborer sur le même espace mémoire :

1. Créer un token par agent (`codex-agent`, `cline-agent`, `claude-agent`, etc.)
2. Configurer chaque agent avec son propre token
3. Tous les agents partagent le même `space_id`

L'identité de l'agent est **automatiquement inférée depuis le token** — aucune spécification manuelle nécessaire.

La communication inter-agents passe **par l'espace partagé** :

```
Codex  → short_note(space_id="mon-projet", category="todo", content="Ajouter la pagination à /users")
Cline  → short_read(space_id="mon-projet", category="todo")  ← voit la tâche de Codex
Cline  → short_note(space_id="mon-projet", category="progress", content="Pagination implémentée")
Codex  → short_read(space_id="mon-projet", category="progress")  ← reprend là où Cline s'est arrêté
```

---

## 🔍 Troubleshooting

### Codex ne voit pas les outils Hivemind

1. Exécutez `codex mcp list`, puis inspectez `/mcp` dans Codex.
2. Vérifiez l'emplacement et la syntaxe TOML de `config.toml`.
3. Vérifiez que le projet est approuvé si vous utilisez `.codex/config.toml`.
4. Vérifiez que `HIVEMIND_TOKEN` existe dans l'environnement qui a lancé Codex.
5. Confirmez le suffixe `/mcp`, puis appelez `system_whoami` pour valider l'auth.

### Erreur « 401 Unauthorized »

- Le token est incorrect, expiré ou révoqué
- Vérifiez que `HIVEMIND_TOKEN` existe dans l'environnement qui a lancé Codex
  et commence par `lm_`
- Vérifiez si le token a été révoqué via la console d'administration

### Erreur « Access Denied to Space »

Le token est restreint à certains spaces (`space_ids`). Soit :
- Demandez à un manager ayant accès d'inviter le hash canonique complet :
  ```
  space_invite_token(space_id="mon-projet", token_hash="sha256:<64 hex minuscules>")
  ```
- Ou demandez à un admin d'ajouter globalement le space :
  ```
  admin_update_token(token_hash, space_ids_add="mon-projet")
  ```

### La consolidation est lente ou time-out

`mid_consolidate` renvoie rapidement un accusé asynchrone contenant un `job_id`
et `next_action="return_to_user_without_polling"`. Appelez-le une fois et rendez
la main ; n'attendez pas le travail LLM sur la même requête MCP et ne lancez pas
de boucle automatique de statut. Utilisez
`bank_consolidation_status(job_id)` uniquement si l'utilisateur demande
explicitement le statut de ce job. Un timeout avant l'accusé indique un problème
de connexion ou de serveur, pas une raison de répéter la consolidation.

### Erreurs de syntaxe TOML

Erreurs fréquentes dans `config.toml` :

```toml
# ✅ CORRECT
bearer_token_env_var = "HIVEMIND_TOKEN"

# ❌ FAUX (met le secret en clair dans le fichier)
http_headers = { "Authorization" = "Bearer lm_abc123" }

# ❌ FAUX (Codex lirait le nom littéral comme token)
bearer_token_env_var = "lm_abc123"
```

---

## 📊 Récapitulatif

| Étape     | Action                                                       | Temps      |
| --------- | ------------------------------------------------------------ | ---------- |
| 1         | Obtenir un token (`token create codex-agent`)                | 1 min      |
| 2         | Exposer le token et configurer l'URL MCP                     | 2 min      |
| 3         | Le manager crée le space et invite le hash exact du token    | 30 sec     |
| 4         | Ajouter `AGENTS.md` avec les instructions Memory Bank        | 2 min      |
| **Total** | **Prêt à l'emploi**                                          | **~6 min** |

---

*Guide d'intégration Hivemind pour OpenAI Codex — [Documentation complète](README.fr.md)*
