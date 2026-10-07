# `hivemind-ingest` — CLI d'ingestion massive pour Hivemind Graph Memory

Outil en ligne de commande autonome développé en **Go (Golang)** pour l'ingestion par lots de fichiers et documentations dans **Graph Memory** (`long`) de Hivemind.

La livraison 1.6.0 couvre l'ontologie choisie, les lots, le suivi et la reprise.
Le mode documentaire `auto` est déjà implémenté pour la qualification
expérimentale ; sa prise en charge est prévue en **1.7.0**. Ce calendrier ne
concerne pas l'ontologie automatique des archives MID, livrée avec leur
projection vers LONG en 1.6.0.

## Fonctionnalités Clés

* **Double ergonomie** :
  * **Mode Interactif (TUI)** : Détecté automatiquement sur terminal interactif si des paramètres sont manquants (saisies guidées, barres d'avancement dynamiques).
  * **Mode Scriptable (CLI pur)** : Pilotable via drapeaux, idéal pour les scripts shell, cron et pipelines CI/CD.
* **Formatage Machine-Readable** : Support du drapeau `--json` pour intégration avec `jq`.
* **Codes retour POSIX stricts** :
  * `0` : Succès terminal vérifié, fichiers ignorés sans erreur, ou simulation locale réussie
  * `1` : Erreur de syntaxe / arguments
  * `2` : Erreur réseau / authentification / serveur
  * `3` : Échec partiel d'ingestion ou dépassement de seuil d'ontologie
  * `4` : Soumission acceptée avec des tâches encore en attente, résultat d'ingestion non vérifié
* **Découpage par lots** : Maximum de 200 fichiers et de 50 Mio de sources par lot ; `--batch-size-mb` peut réduire cette taille. En mode suivi, chaque lot se termine avant le suivant.
* **Optimisation par hachage SHA-256** : Évite de réingérer les documents inchangés (sauf si `--replace` est spécifié).
* **Évaluation d'adéquation d'ontologie (`test-ontology`)** : Détection des schémas inadaptés avec alerte sur le pourcentage d'entités non typées (*Other*).
* **Protocole Streamable HTTP exclusif** vers les serveurs MCP.
* **Zéro valeur en dur** : Configuration par fichier `~/.config/hivemind/config.yaml` (0600) ou variables d'environnement (`HIVEMIND_*`).

---

## Installation & Compilation

Depuis la racine du dépôt, le script reprend la version du fichier `VERSION`
et ajoute le commit et la date de compilation au binaire :

```bash
./scripts/build-hivemind-ingest.sh
./bin/hivemind-ingest version
```

Une compilation directe avec `go build` dans `tools/hivemind-ingest` reste
possible ; elle annonce `dev`, sans métadonnées de livraison injectées.

---

## Guide d'Utilisation

### 1. Configuration (`config`)

```bash
# Définir l'endpoint et le token d'authentification
./bin/hivemind-ingest config set --endpoint https://hivemind.internal/mcp --token secret-token-12345 --space my-team-space

# Afficher la configuration active (token masqué)
./bin/hivemind-ingest config get

# Tester la connexion au serveur
./bin/hivemind-ingest config test
```

### 2. Ingestion de documents (`run`)

```bash
# Ingestion standard d'un dossier avec suivi des tâches en arrière-plan
./bin/hivemind-ingest run \
  --path ./docs \
  --space my-team-space \
  --ontology software \
  --watch

# Mode simulation (dry-run) avec sortie JSON pour pipeline CI
./bin/hivemind-ingest run \
  --path ./docs \
  --space my-team-space \
  --dry-run \
  --json

# Création automatique de l'espace s'il est manquant et forçage de remplacement
./bin/hivemind-ingest run \
  --path ./docs \
  --space new-space \
  --create-space-if-missing \
  --rules standard \
  --replace
```

Avec `--no-poll` ou `--watch=false`, la commande ne suit pas les tâches après
leur soumission. Les statuts `queued`, `running`, `processing`, `pending` et
`in_progress` restent non terminaux : `--json` expose leur nombre dans
`total_pending`, conserve leurs statuts et `job_id` dans `jobs`, et renvoie
`success: false` tant que ce nombre est positif. `total_succeeded` ne compte
que les succès terminaux, une seule fois. Le texte indique une soumission,
pas une ingestion terminée. Le code `3` est prioritaire sur `4` si un échec
réel coexiste avec des tâches en attente. `--no-poll` prévaut sur `--watch`.
Le suivi reste activé par défaut ; `--dry-run` reste local, sans appel serveur,
et renvoie `0` si la simulation réussit.

### Ontologie automatique et reprise

**Parcours expérimental, prise en charge prévue en 1.7.0.** Les modalités
ci-dessous décrivent le code existant ; elles ne constituent pas une
qualification de capacité ou de qualité sur un corpus arbitraire.

```bash
# Le CLI choisit le pré-corpus dans tout le répertoire, puis ingère l’ensemble.
./bin/hivemind-ingest run \
  --path ./corpus \
  --space document-memory \
  --ontology auto \
  --extensions .md,.txt \
  --watch --json
```

`auto` sélectionne de façon déterministe jusqu’à **12 fichiers entiers** dans
l’inventaire complet, avant de retirer les documents déjà indexés. La sélection
privilégie les groupes du premier niveau, puis les empreintes de contenu,
les dossiers et les extensions encore non représentés ; elle ne prend pas
simplement le début du répertoire. Elle respecte le budget d’un lot, donc peut
retenir moins de 12 sources. Le serveur analyse toute la surface de ces fichiers,
choisit ses passages D1/D2, construit une seule ontologie et la gèle. Ce pré-corpus
est ensuite réellement indexé sous ses chemins et empreintes d’origine ; ce ne
sont pas des extraits déguisés en documents complets. `automatic_sources` dans
la sortie JSON indique les fichiers sélectionnés, y compris en `--dry-run`.

La construction initiale exige une **mémoire documentaire vide** : aucun document,
entité ou relation, tant qu’aucun catalogue automatique n’a été gelé. Elle ne
convertit pas un graphe déjà peuplé sous une ontologie choisie ; utiliser un autre
espace vide pour cette première construction.

**Chaque source du pré-corpus doit fournir du texte extractible.** Un PDF sans
couche texte exploitable, par exemple, peut faire échouer tout le bootstrap avec
`automatic_ontology_document_has_no_text`. Le `--dry-run` montre la sélection et
les tailles, mais ne valide pas l’extraction. Un tel échec déterministe n’est pas
corrigé par une relance identique : examiner les sources sélectionnées, préparer
une version lisible ou restreindre `--extensions` (par exemple `.md,.txt`) ou
`--path`. Si cette préparation change le pré-corpus d’une construction déjà
commencée, utiliser un nouvel espace vide : le checkpoint existant reste lié au
lot initial et ne doit pas être remplacé par une sélection différente.

Le premier lot automatique doit terminer avec succès avant l’envoi des suivants.
Ils omettent l’option ontology et réutilisent le catalogue persistant. Si le
bootstrap échoue, les autres sources portent `not_submitted` et
`total_not_submitted` indique leur nombre ; le CLI renvoie un échec. Il n’existe
aucun repli automatique sur `general`. `--ontology auto` exige `--watch` :
`--no-poll` et `--watch=false` sont refusés avant tout appel serveur, sauf en
simulation locale. Le mode choisi conserve son comportement sans suivi.

Après une interruption, relancer **la même commande sur le même corpus inchangé**,
avec les mêmes chemins et la même taille de lot. Le pré-corpus reste identique,
les checkpoints réussis du constructeur sont repris, le catalogue gelé n’est
pas recalculé et les documents déjà réussis sont ignorés. La queue serveur reste
`in_memory_best_effort` : ses jobs ne survivent pas au redémarrage ; la reprise
est obtenue par resoumission, pas par une nouvelle queue durable. Ne modifier ni
le pré-corpus ni le profil d’inférence pendant une construction interrompue.
Le délai `--timeout` vaut par défaut **600 secondes par job suivi** ; une première
construction peut durer davantage. Par exemple, `--timeout 3600` laisse jusqu’à
une heure de suivi par job. Ce délai ne constitue pas un budget d’inférence et
son expiration côté CLI n’annule pas le travail serveur. Vérifier le statut des
jobs avant une reprise, puis conserver les mêmes entrées pour réutiliser les
checkpoints.

Pour des ajouts ultérieurs au corpus, omettre `--ontology` pour utiliser le
catalogue existant (ou passer `--ontology ""` si une ontologie est définie dans
la configuration ou l’environnement). Son évolution automatique est une
fonctionnalité séparée.

En mode suivi, un refus explicite `queue_full` sans `job_id` est rejoué au plus
trois fois, après les jobs admis et des délais de 1, 2 et 4 secondes. Les sources
déjà admises ne sont pas renvoyées ; une erreur d’authentification, de transport
ou de configuration ne déclenche pas de rejeu automatique. Les refus globaux du
bootstrap restent explicites : resoumettre après diagnostic et retour à l’état
idle. Sans suivi, aucun polling ni backoff n’est ajouté.

Chaque fichier reste limité à 50 Mio et les lots à 50 Mio de sources (ou la
valeur inférieure choisie avec `--batch-size-mb`). Une valeur supérieure est
ramenée à 50 Mio avec une notification. L’enveloppe JSON-RPC réellement
sérialisée, base64 et échappements compris, est vérifiée contre les 75 Mio
acceptés par le serveur avant son envoi. Les limites du serveur et la capacité
restante sont toujours appliquées. Aucune troncature de document n’est effectuée.
Un pré-corpus borné favorise la diversité ; il ne prouve pas une couverture
sémantique exhaustive d’un corpus arbitraire.

### 3. Test d’adéquation d’ontologie (`test-ontology`)

```bash
# Évaluer une ontologie sur un échantillon de 5 documents avec seuil max de 20% d'entités "Other"
./bin/hivemind-ingest test-ontology \
  --path ./docs \
  --ontology cloud \
  --sample-size 5 \
  --threshold-other 20
```

---

## Variables d'Environnement

| Variable | Description |
|---|---|
| `HIVEMIND_ENDPOINT` | URL de l'endpoint MCP Streamable HTTP |
| `HIVEMIND_TOKEN` | Jeton d'authentification Bearer |
| `HIVEMIND_SPACE` | Espace Hivemind par défaut |
| `HIVEMIND_ONTOLOGY` | Ontologie par défaut |
| `HIVEMIND_BATCH_SIZE_MB` | Taille maximale d'un lot en Mo (défaut : 50) |
| `HIVEMIND_TIMEOUT_SECONDS` | Timeout des requêtes HTTP (défaut : 600) |
| `HIVEMIND_THRESHOLD_OTHER` | Seuil d'alerte en % d'entités "Other" (défaut : 25) |
| `HIVEMIND_EXTENSIONS` | Liste d'extensions autorisées séparées par virgule |
| `HIVEMIND_CONFIG` | Chemin personnalisé vers le fichier de configuration YAML |
