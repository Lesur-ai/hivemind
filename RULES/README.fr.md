# RULES — Modèles Mid-Memory Hivemind

Ce répertoire contient des modèles pour créer un space Hivemind avec
`space_create`. Les rules définissent la structure Markdown mid-memory souhaitée
et guident l'organisation des notes short par le LLM consolidateur. Le même
`space_id` porte aussi le tier long dérivé et non autoritaire.

## Autorité et sécurité

La bank mid-memory est un contexte de travail partagé, pas l'unique source de
vérité. Les fichiers du dépôt, documents signés, sources originales, systèmes
métier approuvés et autres systèmes de référence restent autoritaires. La
consolidation est une transformation par LLM : elle peut omettre, résumer,
classer ou comprendre incorrectement un contenu. Toute information importante
doit être vérifiée dans sa source canonique avant d'agir.

Les rules sont insérées dans la requête envoyée au LLM configuré pendant
`mid_consolidate`. Traitez-les comme des instructions de prompt exécutables :
n'y placez ni secret, ni identifiant, ni donnée personnelle ou réglementée sans
validation préalable de toute la chaîne de stockage et de traitement LLM.

## Les rules sont-elles modifiables ?

Les rules restent normalement stables après la création afin que les
consolidations suivent une structure cohérente. Elles ne sont pas techniquement
immuables : un appelant ayant accès au space et la permission `manage` peut les
remplacer avec `space_update_rules` (CLI : `space update-rules`). Traitez cette
opération comme une migration opérateur : relisez le modèle, conservez un backup
et validez la consolidation suivante avant de vous fier à la bank mise à jour.

## Modèles disponibles

| Fichier | Domaine | Objectif |
| --- | --- | --- |
| `live-mem.standard.memory.bank.md` | Général | Workspace logiciel/projet en six fichiers : `projectbrief.md`, `productContext.md`, `activeContext.md`, `systemPatterns.md`, `techContext.md` et `progress.md`. |
| `book.memory.bank.md` | Écriture | Planification éditoriale avec sources, architecture narrative, contexte actif et progression. |
| `medical.memory.bank.md` | Démonstration non clinique | Organisation de notes de santé synthétiques ou correctement désidentifiées pour évaluer le classement et la vérification humaine. Ce modèle n'est ni un dossier clinique ni une aide à la décision et ne doit pas servir aux soins. |
| `presales.memory.bank.md` | Avant-vente | Analyse de proposition, personas, contradictions, arguments réutilisables et suivi d'avancement. |
| `product.management.memory.bank.md` | Product management | Vision, portfolio, recherche, design, contexte technique, discovery, communication parties prenantes, features et décisions roadmap. |

## Créer un space

Utilisez le chemin exact du modèle et l'option nommée de description :

```bash
uv run python scripts/mcp_cli.py space create mon-projet \
  --description "Mon projet" \
  --rules-file RULES/live-mem.standard.memory.bank.md
```

L'appel MCP équivalent passe le contenu du fichier dans `rules` :

```python
space_create(
    space_id="mon-projet",
    description="Mon projet",
    rules=rules_markdown,
)
```

## Mettre à jour les rules existantes

`space_update_rules` exige l'accès au space et la permission `manage` :

```bash
uv run python scripts/mcp_cli.py space update-rules mon-projet \
  --rules-file RULES/live-mem.standard.memory.bank.md
```

Cette opération remplace `_rules.md` ; elle ne réécrit ni ne valide
automatiquement les fichiers mid-memory existants. Relisez la bank après la
consolidation.

## Créer un modèle

Définissez :

- les fichiers souhaités, leur rôle, leur taille et leur cycle de vie ;
- le mapping des catégories de notes vers ces fichiers ;
- les sources externes autoritaires et le traitement des contradictions ;
- ce que le consolidateur peut résumer, remplacer ou supprimer ;
- les exigences de vérification humaine et de traitement des données du domaine.

Un bon modèle donne une structure précise sans promettre une mémoire parfaite
ni remplacer les preuves canoniques du projet.
