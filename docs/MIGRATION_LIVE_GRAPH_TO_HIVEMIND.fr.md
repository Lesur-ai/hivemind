# Migrer Live Memory et Graph Memory vers Hivemind

Cette page résume en français le playbook public de migration. Le
[playbook anglais space par space](MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md) est la
procédure canonique et contient les préconditions, appels, contrôles et règles
de rollback détaillés.

La migration remplace deux serveurs MCP (Live Memory et Graph Memory) par un
seul endpoint Hivemind. Un `space_id` Hivemind porte :

- `short` : notes récentes append-only ;
- `mid` : fichiers Markdown de mémoire projet consolidée ;
- `long` : ontologie/graphe de connaissances dérivé ;
- `hive` : état de coordination Project Mesh.

Les fichiers canoniques du dépôt restent l'autorité finale. La mémoire long
sert à localiser des documents et relations ; elle n'est jamais une vérité de
commit, rollback, audit, membership, tombstone, watermark ou recovery.

## Données couvertes

| Source | Destination Hivemind | Méthode |
| --- | --- | --- |
| Notes live | Tier `short` du même `space_id` | Préservation du préfixe S3 ou restauration d'un backup |
| Rules, fichiers bank, synthesis, métadonnées | Tier `mid` du même `space_id` | Même chemin que les notes |
| Documents, entités et relations Graph | Tier `long` du même `space_id` | Reconstruction comme projection dérivée |
| Identités agents | Registre de tokens Hivemind | Nouveau token distinct pour chaque agent |
| Instructions agents | `AGENTS.md`, `CLAUDE.md`, `.clinerules/`, workflows | Réécriture vers un endpoint et les outils canoniques |

Un backup Hivemind couvre les données short/mid et les métadonnées du space. Il
ne copie pas les datastores Graph externes.

## Déployer Hivemind

Suivez le [guide de déploiement](DEPLOYMENT.md). La stack Compose par défaut
inclut WAF, Hivemind, le runtime Graph Memory embarqué, Neo4j et Qdrant. Les
agents se connectent uniquement à Hivemind.

```bash
install -m 600 .env.example .env
# Configurer S3, LLM, ADMIN_BOOTSTRAP_KEY, NEO4J_PASSWORD, TLS et secrets.
# Project Mesh est actif par défaut : fournir son identité, ou définir
# HIVEMIND_MESH_ENABLED=false pour un déploiement volontairement non-Mesh.

docker compose up --build -d --wait
docker compose ps
```

Créez ensuite un premier admin avec la clé bootstrap. N'utilisez jamais cette
clé ou un token admin comme credential courant d'un agent.

## Créer un nouveau token pour chaque agent

Chaque identité agent reçoit un **nouveau token Hivemind dédié**. Ne réutilisez
pas les tokens Live Memory ou Graph Memory, et ne partagez pas un token entre
plusieurs agents : l'identité du token devient la provenance des notes short.

```bash
uv run python scripts/mcp_cli.py token create <nom-agent-unique> -p read,write
```

Conservez le secret et le hash canonique complet retournés une seule fois.
Vous pouvez créer les tokens avant la bascule, mais n'exécutez `space invite`
qu'après l'existence du space cible :

- **In-place** : invitez les hashes après que Hivemind sait lire le space
  existant.
- **Nouveau backend** : terminez d'abord `backup_restore`, puis invitez les
  hashes. `space_invite_token` refuse un space absent ; une invitation avant le
  restore échoue donc sur ce parcours.

Au moment applicable, accordez chaque accès :

```bash
uv run python scripts/mcp_cli.py space invite <space-id> \
  sha256:<hash-canonique-retourné-par-token-create>
```

Répétez `space invite` pour chaque space attribué. Réservez `manage` aux
provisioners de confiance : ce droit peut créer des spaces et déléguer de
nouveaux managers. Gardez le token clair dans la configuration MCP ou un
gestionnaire de secrets, jamais dans les instructions ou le dépôt.

## Procédure space par space

Terminez toutes les étapes pour le space A avant de commencer le space B.

### 1. Inventorier et sauvegarder

- enregistrer les rules exactes (`space_rules`) ;
- enregistrer compteurs et timestamps de métadonnées (`space_info`) ;
- inventorier les notes short et les fichiers mid (`mid_list`) ;
- créer et télécharger un `backup_create`, puis conserver son hash ;
- inventorier l'ontologie, les source paths et des requêtes représentatives du
  Graph legacy ;
- inventorier tous les agents et tous les fichiers d'instructions chargés.

### 2. Mettre le space en lecture seule

Arrêtez agents, automations, consolidations, repairs, GC, restores, pushes et
mutations peer sur ce space. Attendez la fin des opérations déjà acceptées,
puis refaites l'inventaire et le backup finaux.

### 3. Migrer short et mid

Choisissez un seul chemin :

- **In-place** : Hivemind utilise le bucket existant, conserve exactement le
  `space_id`, et **ne rappelle pas `space_create`**. Le `_meta.json`, les rules,
  notes et fichiers bank existants deviennent short/mid.
- **Nouveau backend** : copiez le préfixe `_backups/<space-id>/` vers le bucket
  Hivemind, vérifiez que le target n'existe pas, ne lancez pas `space_create`,
  puis authentifiez le restore avec l'identité bootstrap, un admin global ou
  un token `manage` qu'un admin global a explicitement pré-scopé à ce
  `space_id`. Un manager fraîchement créé sans scope ne peut pas restaurer une
  cible absente. Appelez ensuite
  `backup_restore(backup_id="<space-id>/<timestamp>", confirm=True)`, attendez
  que le space restauré existe, puis invitez les hashes agents avant la
  vérification.

`backup_restore` restaure vers le `space_id` contenu dans le backup. Il ne
renomme pas un space. N'improvisez pas une réécriture de `_meta.json`.

### 4. Vérifier short et mid

Comparez rules, compteurs de notes, puis le timestamp de dernière note depuis
l'inventaire short/objets (`space_info` n'expose pas ce champ). Comparez aussi
liste et taille des fichiers mid, contenu de plusieurs fichiers et échantillon
de notes. Testez
ensuite `mid_read_all` avec le nouveau token agent. Une liste partielle,
ambiguë, refusée ou corrompue bloque la migration ; elle ne prouve jamais qu'un
tier est vide.

### 5. Reconstruire long

Gardez le Graph legacy en lecture seule comme référence. Préservez les
documents canoniques à l'origine des faits du graphe. Pour le bootstrap unique
d'une bank stabilisée :

```text
long_push(space_id="<space-id>", include_volatile=False)
long_status(space_id="<space-id>")
long_query(space_id="<space-id>", query="<requête de validation>")
```

Le premier `long_push` lie automatiquement le space au runtime embarqué.
Vérifiez zéro erreur et que `activeContext.md`/`progress.md` sont listés dans
`skipped_volatile` s'ils existent. Ce push est une exception de bootstrap, pas
une synchronisation de fin de session. Le mode `apply` de `long_ingest` est
différé en V1 : un plan `dry-run` ou `check-remote` ne prouve aucune écriture.

Un fait présent uniquement dans l'ancien graphe n'est pas transféré
automatiquement. Conservez le Graph legacy et récupérez son document source ;
ne déclarez pas la migration terminée tant que ce point reste ouvert.

### 6. Réécrire les instructions agents

Pour `AGENTS.md`, `CLAUDE.md`, `.clinerules/`, prompts globaux et workflows :

| Avant | Après |
| --- | --- |
| Deux serveurs Live/Graph | Un `HIVEMIND_MCP_SERVER` |
| Live `space_id` + Graph `memory_id` | Un `space_id` Hivemind |
| `bank_read_all`, `live_read` | `mid_read_all`, `short_read` |
| `live_note`, `bank_consolidate` | `short_note`, `mid_consolidate` |
| Query sur un serveur Graph séparé | `long_query` via Hivemind |
| Push Graph automatique en fin de session | Aucune ingestion long de routine |

Utilisez le [contrat agent unifié](AGENT_MEMORY_SETUP.md). Préservez les règles
projet de test, Git, sécurité et review. Ne remplacez pas les rules de
consolidation du space par les instructions agent génériques.

### 7. Valider et basculer

Avec chaque nouveau token et uniquement l'endpoint Hivemind, vérifiez
`system_whoami`, `space_rules`, `mid_read_all`, `short_read`, l'attribution
d'une note test, `long_status`, une requête `long_query`, et le chargement réel
du fichier d'instructions. Vérifiez également que le token est refusé sur un
space non attribué.

N'activez les écritures normales et ne passez au space suivant qu'après la
validation de short, mid, long, scopes token et instructions.

## Restore d'un space Project Mesh existant

Le chemin normal restaure un target inexistant. Sur un space portant déjà
l'état Project Mesh, `backup_restore` refuse par défaut. `unsafe_recovery=True`
est une opération de disaster recovery : elle force l'état vers l'avant,
publie la bank restaurée comme nouvel historique, réduit le membership au nœud
local et laisse `resync_required`. Elle ne termine pas la migration et ne
restaure pas long. Un état corrompu reste refusé.

## Rollback et retrait des anciens services

Avant la première écriture Hivemind, corrigez simplement le target en gardant
le legacy read-only. Après une écriture Hivemind, les historiques divergent :
ne basculez pas les writers dans les deux sens et n'essayez pas de fusionner les
historiques. Utilisez un plan de restore/recovery revu avec le backup retenu.

Ne révoquez les anciens tokens et ne retirez les services legacy qu'après
validation de tous les spaces et agents, conservation des backups/hashes,
suppression de toutes les références legacy, et accord opérateur.

## Limites V1

<!-- non-claims -->
Ce playbook respecte les limites V1 : pas de multi-space merge, pas de parallel
consolidation sur un space, pas de quorum, hub topology, permanent master,
leader runtime, CRDT/offline-first, ni isolation multi-tenant dans l'édition
OSS. Project Mesh V1 / Mesh Sync V1 utilise full-mesh all-ACK. La mémoire long
reste dérivée et non autoritative.
<!-- /non-claims -->

## Références

- [Playbook anglais canonique](MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md)
- [Configuration unifiée des agents](AGENT_MEMORY_SETUP.md)
- [Déploiement](DEPLOYMENT.md)
- [Mapping des outils](TOOL_MAPPING.md)
- [Référence MCP](MCP_TOOLS_SPEC.md)
