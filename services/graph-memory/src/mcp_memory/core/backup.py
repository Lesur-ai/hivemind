# -*- coding: utf-8 -*-
"""
BackupService - Orchestrateur de backup/restore pour les mémoires.

Coordonne l'export/import des 3 couches de données :
- Neo4j (graphe : Memory, Document, Entity, relations)
- Qdrant (vecteurs : embeddings + payloads des chunks)
- S3 (documents originaux : PDF, DOCX, etc.)

Format de backup sur S3 :
    _backups/{memory_id}/{timestamp}/
    ├── manifest.json          # Métadonnées, version, checksums, stats
    ├── graph_data.json        # Export complet Neo4j (nœuds + relations)
    ├── qdrant_vectors.jsonl   # Points Qdrant (embedding + payload), 1 par ligne
    └── document_keys.json     # Références S3 des documents originaux

Politique de rétention : configurable via BACKUP_RETENTION_COUNT (.env)
"""

import io
import json
import sys
import tarfile
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any

from ..config import get_settings


# Version du format de backup (pour compatibilité future)
BACKUP_FORMAT_VERSION = "1.0"

# Taille max d'une archive tar.gz en bytes (100 MB)
MAX_ARCHIVE_SIZE_BYTES = 100 * 1024 * 1024

# Regex pour valider les composants d'un backup_id (pas de path traversal)
import re
_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')


class BackupService:
    """
    Service de backup et restauration des mémoires.
    
    Utilise les services existants (GraphService, VectorStoreService,
    StorageService) pour exporter et importer les données.
    """
    
    def __init__(self, graph_service, vector_store, storage_service):
        """
        Args:
            graph_service: Instance de GraphService
            vector_store: Instance de VectorStoreService
            storage_service: Instance de StorageService
        """
        self._graph = graph_service
        self._vectors = vector_store
        self._storage = storage_service
        self._settings = get_settings()
        self._prefix = self._settings.s3_backup_prefix
        self._retention = self._settings.backup_retention_count
    
    @staticmethod
    def _validate_backup_id(backup_id: str) -> tuple:
        """
        Valide et décompose un backup_id en (memory_id, timestamp).
        
        Sécurité : empêche l'injection de path traversal (../, /, etc.)
        dans le backup_id qui est utilisé pour construire des clés S3.
        
        Raises:
            ValueError si le format est invalide ou contient des caractères dangereux
        """
        if not backup_id or not isinstance(backup_id, str):
            raise ValueError("backup_id requis")
        
        parts = backup_id.split("/", 1)
        if len(parts) != 2:
            raise ValueError(
                f"backup_id invalide: '{backup_id}'. "
                f"Format attendu: 'MEMORY_ID/TIMESTAMP'"
            )
        
        memory_id, timestamp = parts
        
        # Valider chaque composant (alphanumérique + tirets + underscores uniquement)
        if not _SAFE_ID_RE.match(memory_id):
            raise ValueError(
                f"memory_id invalide dans backup_id: '{memory_id}'. "
                f"Caractères autorisés: A-Z, a-z, 0-9, -, _"
            )
        if not _SAFE_ID_RE.match(timestamp):
            raise ValueError(
                f"timestamp invalide dans backup_id: '{timestamp}'. "
                f"Caractères autorisés: A-Z, a-z, 0-9, -, _"
            )
        
        return memory_id, timestamp
    
    def _backup_s3_prefix(self, memory_id: str, timestamp: str) -> str:
        """Construit le préfixe S3 pour un backup."""
        return f"{self._prefix}/{memory_id}/{timestamp}"
    
    # =========================================================================
    # Backup
    # =========================================================================
    
    async def create_backup(
        self,
        memory_id: str,
        description: Optional[str] = None,
        progress_callback=None
    ) -> Dict[str, Any]:
        """
        Crée un backup complet d'une mémoire sur S3.
        
        Exporte :
        1. Données Neo4j (graphe complet) → graph_data.json
        2. Vecteurs Qdrant (embeddings + payloads) → qdrant_vectors.jsonl
        3. Références documents S3 → document_keys.json
        4. Manifest avec checksums et statistiques → manifest.json
        
        Applique ensuite la politique de rétention si configurée.
        
        Args:
            memory_id: ID de la mémoire à sauvegarder
            description: Description optionnelle du backup
            progress_callback: Callback async(msg: str) pour signaler la progression
            
        Returns:
            Dict avec backup_id, stats, manifest
        """
        import time as _time
        _t0 = _time.monotonic()
        
        async def _log(msg):
            print(f"💾 [Backup] {msg}", file=sys.stderr)
            sys.stderr.flush()
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass
        
        # Vérifier que la mémoire existe
        memory = await self._graph.get_memory(memory_id)
        if not memory:
            raise ValueError(f"Mémoire '{memory_id}' non trouvée")
        
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
        backup_prefix = self._backup_s3_prefix(memory_id, timestamp)
        backup_id = f"{memory_id}/{timestamp}"
        
        await _log(f"Démarrage backup: {backup_id}")
        
        # === 1. Export Neo4j ===
        await _log("📊 Export graphe Neo4j...")
        graph_data = await self._graph.export_memory_data(memory_id)
        graph_json = json.dumps(graph_data, ensure_ascii=False, indent=2)
        graph_hash = hashlib.sha256(graph_json.encode()).hexdigest()
        
        graph_stats = {
            "documents": len(graph_data.get("documents", [])),
            "entities": len(graph_data.get("entities", [])),
            "relations": len(graph_data.get("relations", [])),
            "mentions": len(graph_data.get("mentions", []))
        }
        await _log(f"✅ Graphe: {graph_stats['entities']} entités, "
                    f"{graph_stats['relations']} relations, "
                    f"{graph_stats['documents']} docs")
        
        # === 2. Export Qdrant ===
        await _log("🔢 Export vecteurs Qdrant...")
        qdrant_points = await self._vectors.export_collection(memory_id)
        
        # Format JSONL (une ligne JSON par point, économise de la mémoire)
        qdrant_lines = []
        for point in qdrant_points:
            qdrant_lines.append(json.dumps(point, ensure_ascii=False))
        qdrant_jsonl = "\n".join(qdrant_lines)
        qdrant_hash = hashlib.sha256(qdrant_jsonl.encode()).hexdigest()
        
        await _log(f"✅ Qdrant: {len(qdrant_points)} vecteurs exportés")
        
        # === 3. Références documents S3 ===
        await _log("📄 Collecte des références documents S3...")
        document_keys = []
        for doc in graph_data.get("documents", []):
            uri = doc.get("uri", "")
            if uri:
                try:
                    key = self._storage._parse_key(uri)
                    document_keys.append({
                        "doc_id": doc.get("id"),
                        "filename": doc.get("filename"),
                        "uri": uri,
                        "key": key,
                        "hash": doc.get("hash"),
                        "size_bytes": doc.get("size_bytes", 0)
                    })
                except ValueError:
                    pass
        
        doc_keys_json = json.dumps(document_keys, ensure_ascii=False, indent=2)
        doc_keys_hash = hashlib.sha256(doc_keys_json.encode()).hexdigest()
        
        total_doc_size = sum(d.get("size_bytes", 0) for d in document_keys)
        await _log(f"✅ Documents: {len(document_keys)} références "
                    f"({self._human_size(total_doc_size)})")
        
        # === 4. Construire le manifest ===
        elapsed = round(_time.monotonic() - _t0, 1)
        manifest = {
            "version": BACKUP_FORMAT_VERSION,
            "backup_id": backup_id,
            "memory_id": memory_id,
            "memory_name": memory.name,
            "memory_ontology": memory.ontology,
            "description": description,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "elapsed_seconds": elapsed,
            "stats": {
                **graph_stats,
                "qdrant_vectors": len(qdrant_points),
                "document_files": len(document_keys),
                "total_document_size_bytes": total_doc_size,
            },
            "checksums": {
                "graph_data": graph_hash,
                "qdrant_vectors": qdrant_hash,
                "document_keys": doc_keys_hash,
            },
            "files": [
                "manifest.json",
                "graph_data.json",
                "qdrant_vectors.jsonl",
                "document_keys.json",
            ]
        }
        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
        
        # === 5. Upload sur S3 ===
        await _log("📤 Upload sur S3...")
        
        files_to_upload = [
            ("manifest.json", manifest_json.encode("utf-8"), "application/json"),
            ("graph_data.json", graph_json.encode("utf-8"), "application/json"),
            ("qdrant_vectors.jsonl", qdrant_jsonl.encode("utf-8"), "application/x-ndjson"),
            ("document_keys.json", doc_keys_json.encode("utf-8"), "application/json"),
        ]
        
        for filename, content, content_type in files_to_upload:
            key = f"{backup_prefix}/{filename}"
            self._storage._client.put_object(
                Bucket=self._storage._bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                Metadata={
                    "backup_id": backup_id,
                    "memory_id": memory_id,
                }
            )
            await _log(f"  📁 {filename} ({self._human_size(len(content))})")
        
        # === 6. Politique de rétention ===
        deleted_backups = []
        if self._retention > 0:
            deleted_backups = await self._apply_retention(memory_id)
            if deleted_backups:
                await _log(f"🧹 Rétention: {len(deleted_backups)} ancien(s) backup(s) supprimé(s)")
        
        total_elapsed = round(_time.monotonic() - _t0, 1)
        await _log(f"🏁 Backup terminé en {total_elapsed}s: {backup_id}")
        
        return {
            "status": "ok",
            "backup_id": backup_id,
            "memory_id": memory_id,
            "s3_prefix": backup_prefix,
            "created_at": manifest["created_at"],
            "stats": manifest["stats"],
            "elapsed_seconds": total_elapsed,
            "retention_deleted": len(deleted_backups),
        }
    
    # =========================================================================
    # List backups
    # =========================================================================
    
    async def list_backups(self, memory_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Liste les backups disponibles sur S3.
        
        Args:
            memory_id: Si fourni, liste uniquement les backups de cette mémoire.
                       Sinon, liste tous les backups.
            
        Returns:
            Liste de manifests (triés par date décroissante)
        """
        prefix = f"{self._prefix}/"
        if memory_id:
            prefix = f"{self._prefix}/{memory_id}/"
        
        # Lister tous les objets sous le préfixe
        all_objects = await self._storage.list_all_objects(prefix=prefix)
        
        # Trouver les manifest.json
        manifests = []
        for obj in all_objects:
            if obj["key"].endswith("/manifest.json"):
                try:
                    # Télécharger et parser le manifest
                    response = self._storage._client.get_object(
                        Bucket=self._storage._bucket,
                        Key=obj["key"]
                    )
                    content = response["Body"].read()
                    manifest = json.loads(content.decode("utf-8"))
                    manifest["s3_key"] = obj["key"]
                    manifest["s3_size"] = obj["size"]
                    manifests.append(manifest)
                except Exception as e:
                    print(f"⚠️ [Backup] Erreur lecture manifest {obj['key']}: {e}",
                          file=sys.stderr)
        
        # Trier par date décroissante
        manifests.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        
        return manifests
    
    # =========================================================================
    # Restore
    # =========================================================================
    
    async def restore_backup(
        self,
        backup_id: str,
        progress_callback=None
    ) -> Dict[str, Any]:
        """
        Restaure une mémoire depuis un backup S3.
        
        La mémoire NE DOIT PAS exister (erreur sinon).
        Recrée le graphe Neo4j + les vecteurs Qdrant tels qu'ils étaient.
        Les documents S3 ne sont pas copiés (ils sont déjà en place).
        
        Args:
            backup_id: ID du backup (format: "{memory_id}/{timestamp}")
            progress_callback: Callback async(msg: str) pour signaler la progression
            
        Returns:
            Dict avec compteurs de restauration
        """
        import time as _time
        _t0 = _time.monotonic()
        
        async def _log(msg):
            print(f"📥 [Restore] {msg}", file=sys.stderr)
            sys.stderr.flush()
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass
        
        # Valider le backup_id (anti path-traversal)
        memory_id, timestamp = self._validate_backup_id(backup_id)
        
        await _log(f"Démarrage restauration: {backup_id}")
        backup_prefix = self._backup_s3_prefix(memory_id, timestamp)
        
        # === 1. Télécharger et vérifier le manifest ===
        await _log("📋 Lecture du manifest...")
        manifest = await self._download_json(f"{backup_prefix}/manifest.json")
        
        if manifest.get("version") != BACKUP_FORMAT_VERSION:
            raise ValueError(
                f"Version de backup incompatible: {manifest.get('version')} "
                f"(attendue: {BACKUP_FORMAT_VERSION})"
            )
        
        await _log(f"✅ Manifest OK: {manifest.get('memory_name', '?')} "
                    f"({manifest['stats']['entities']} entités, "
                    f"{manifest['stats']['qdrant_vectors']} vecteurs)")
        
        # === 2. Télécharger les données du graphe ===
        await _log("📊 Chargement des données graphe...")
        graph_data = await self._download_json(f"{backup_prefix}/graph_data.json")
        
        # Vérifier le checksum
        graph_json = json.dumps(graph_data, ensure_ascii=False, indent=2)
        actual_hash = hashlib.sha256(graph_json.encode()).hexdigest()
        expected_hash = manifest.get("checksums", {}).get("graph_data")
        if expected_hash and actual_hash != expected_hash:
            raise ValueError(
                f"Checksum graphe invalide ! Attendu: {expected_hash[:16]}..., "
                f"Obtenu: {actual_hash[:16]}... Le backup est peut-être corrompu."
            )
        
        await _log("✅ Données graphe vérifiées (checksum OK)")
        
        # === 3. Télécharger les vecteurs Qdrant ===
        await _log("🔢 Chargement des vecteurs Qdrant...")
        qdrant_jsonl = await self._download_text(f"{backup_prefix}/qdrant_vectors.jsonl")
        
        qdrant_points = []
        if qdrant_jsonl.strip():
            for line in qdrant_jsonl.strip().split("\n"):
                if line.strip():
                    qdrant_points.append(json.loads(line))
        
        # Vérifier le checksum
        actual_hash = hashlib.sha256(qdrant_jsonl.encode()).hexdigest()
        expected_hash = manifest.get("checksums", {}).get("qdrant_vectors")
        if expected_hash and actual_hash != expected_hash:
            raise ValueError(
                f"Checksum vecteurs invalide ! Le backup est peut-être corrompu."
            )
        
        await _log(f"✅ {len(qdrant_points)} vecteurs chargés (checksum OK)")
        
        # === 4. Restaurer le graphe Neo4j ===
        await _log("📊 Restauration du graphe Neo4j...")
        graph_counters = await self._graph.import_memory_data(graph_data)
        await _log(f"✅ Graphe restauré: {graph_counters}")
        
        # === 5. Restaurer les vecteurs Qdrant ===
        await _log("🔢 Restauration des vecteurs Qdrant...")
        vectors_imported = await self._vectors.import_collection(memory_id, qdrant_points)
        await _log(f"✅ Qdrant: {vectors_imported} vecteurs restaurés")
        
        # === 6. Vérifier les documents S3 ===
        await _log("📄 Vérification des documents S3...")
        doc_keys = await self._download_json(f"{backup_prefix}/document_keys.json")
        
        docs_ok = 0
        docs_missing = 0
        for doc in doc_keys:
            uri = doc.get("uri", "")
            if uri:
                exists = await self._storage.document_exists(uri)
                if exists:
                    docs_ok += 1
                else:
                    docs_missing += 1
                    print(f"⚠️ [Restore] Document S3 manquant: {uri}", file=sys.stderr)
        
        await _log(f"📄 Documents S3: {docs_ok} OK, {docs_missing} manquant(s)")
        
        total_elapsed = round(_time.monotonic() - _t0, 1)
        await _log(f"🏁 Restauration terminée en {total_elapsed}s")
        
        return {
            "status": "ok",
            "backup_id": backup_id,
            "memory_id": memory_id,
            "graph": graph_counters,
            "qdrant_vectors_restored": vectors_imported,
            "s3_documents_ok": docs_ok,
            "s3_documents_missing": docs_missing,
            "elapsed_seconds": total_elapsed,
        }
    
    # =========================================================================
    # Download (tar.gz local)
    # =========================================================================
    
    async def download_backup(
        self,
        backup_id: str,
        include_documents: bool = False,
        progress_callback=None
    ) -> bytes:
        """
        Télécharge un backup sous forme d'archive tar.gz.
        
        Par défaut (light) : uniquement les fichiers JSON du backup.
        Avec include_documents=True : inclut aussi les fichiers originaux.
        
        Args:
            backup_id: ID du backup
            include_documents: Si True, inclut les documents originaux
            progress_callback: Callback async(msg: str)
            
        Returns:
            Contenu de l'archive tar.gz en bytes
        """
        async def _log(msg):
            print(f"📦 [Download] {msg}", file=sys.stderr)
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass
        
        # Valider le backup_id (anti path-traversal)
        memory_id, timestamp = self._validate_backup_id(backup_id)
        backup_prefix = self._backup_s3_prefix(memory_id, timestamp)
        
        await _log(f"Préparation archive: {backup_id}")
        
        # Créer l'archive tar.gz en mémoire
        buf = io.BytesIO()
        
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            archive_dir = f"backup-{memory_id}-{timestamp}"
            
            # Ajouter les fichiers JSON du backup
            json_files = [
                "manifest.json",
                "graph_data.json",
                "qdrant_vectors.jsonl",
                "document_keys.json",
            ]
            
            for filename in json_files:
                key = f"{backup_prefix}/{filename}"
                try:
                    response = self._storage._client.get_object(
                        Bucket=self._storage._bucket,
                        Key=key
                    )
                    content = response["Body"].read()
                    
                    # Ajouter au tar
                    info = tarfile.TarInfo(name=f"{archive_dir}/{filename}")
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                    
                    await _log(f"  📁 {filename} ({self._human_size(len(content))})")
                except Exception as e:
                    await _log(f"  ⚠️ {filename} manquant: {e}")
            
            # Optionnel : inclure les documents originaux
            if include_documents:
                await _log("📄 Ajout des documents originaux...")
                
                doc_keys_text = await self._download_text(
                    f"{backup_prefix}/document_keys.json"
                )
                doc_keys = json.loads(doc_keys_text) if doc_keys_text.strip() else []
                
                for i, doc in enumerate(doc_keys):
                    key = doc.get("key", "")
                    filename_orig = doc.get("filename", f"doc_{i}")
                    if not key:
                        continue
                    
                    try:
                        response = self._storage._client.get_object(
                            Bucket=self._storage._bucket,
                            Key=key
                        )
                        content = response["Body"].read()
                        
                        info = tarfile.TarInfo(
                            name=f"{archive_dir}/documents/{filename_orig}"
                        )
                        info.size = len(content)
                        tar.addfile(info, io.BytesIO(content))
                        
                        await _log(f"  📄 {filename_orig} ({self._human_size(len(content))})")
                    except Exception as e:
                        await _log(f"  ⚠️ {filename_orig}: {e}")
        
        archive_bytes = buf.getvalue()
        await _log(f"✅ Archive: {self._human_size(len(archive_bytes))}")
        
        return archive_bytes
    
    # =========================================================================
    # Restore from archive (tar.gz)
    # =========================================================================
    
    async def restore_from_archive(
        self,
        archive_bytes: bytes,
        progress_callback=None
    ) -> Dict[str, Any]:
        """
        Restaure une mémoire depuis une archive tar.gz locale.
        
        L'archive doit contenir :
        - manifest.json (obligatoire)
        - graph_data.json (obligatoire)
        - qdrant_vectors.jsonl (obligatoire)
        - documents/ (optionnel : fichiers originaux à re-uploader sur S3)
        
        La mémoire NE DOIT PAS exister (erreur sinon).
        
        Args:
            archive_bytes: Contenu de l'archive tar.gz
            progress_callback: Callback async(msg: str)
            
        Returns:
            Dict avec compteurs de restauration
        """
        import time as _time
        _t0 = _time.monotonic()
        
        async def _log(msg):
            print(f"📦 [RestoreArchive] {msg}", file=sys.stderr)
            sys.stderr.flush()
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass
        
        # === 0. Vérifier la taille de l'archive (anti DoS) ===
        archive_size = len(archive_bytes)
        if archive_size > MAX_ARCHIVE_SIZE_BYTES:
            raise ValueError(
                f"Archive trop volumineuse: {self._human_size(archive_size)} "
                f"(max: {self._human_size(MAX_ARCHIVE_SIZE_BYTES)})"
            )
        
        await _log(f"Archive reçue: {self._human_size(archive_size)}")
        
        # === 1. Extraire l'archive en mémoire ===
        buf = io.BytesIO(archive_bytes)
        try:
            tar = tarfile.open(fileobj=buf, mode='r:gz')
        except Exception as e:
            raise ValueError(f"Archive tar.gz invalide: {e}")
        
        # Trouver les fichiers dans l'archive (peut être dans un sous-dossier)
        members = tar.getnames()
        
        def _find_member(filename: str) -> Optional[str]:
            """Trouve un fichier dans l'archive (avec ou sans préfixe dossier)."""
            for m in members:
                if m == filename or m.endswith(f"/{filename}"):
                    return m
            return None
        
        def _read_member(member_name: str) -> bytes:
            """Lit le contenu d'un membre de l'archive."""
            f = tar.extractfile(member_name)
            if f is None:
                raise ValueError(f"Impossible de lire '{member_name}' dans l'archive")
            return f.read()
        
        # === 2. Lire et vérifier le manifest ===
        manifest_path = _find_member("manifest.json")
        if not manifest_path:
            tar.close()
            raise ValueError("manifest.json introuvable dans l'archive")
        
        manifest = json.loads(_read_member(manifest_path).decode("utf-8"))
        
        if manifest.get("version") != BACKUP_FORMAT_VERSION:
            tar.close()
            raise ValueError(
                f"Version de backup incompatible: {manifest.get('version')} "
                f"(attendue: {BACKUP_FORMAT_VERSION})"
            )
        
        memory_id = manifest.get("memory_id")
        if not memory_id:
            tar.close()
            raise ValueError("memory_id manquant dans le manifest")
        
        await _log(f"✅ Manifest OK: mémoire '{memory_id}' "
                    f"({manifest['stats']['entities']} entités, "
                    f"{manifest['stats']['qdrant_vectors']} vecteurs)")
        
        # Vérifier que la mémoire n'existe pas
        existing = await self._graph.get_memory(memory_id)
        if existing:
            tar.close()
            raise ValueError(
                f"La mémoire '{memory_id}' existe déjà. "
                f"Supprimez-la d'abord avec memory_delete."
            )
        
        # === 3. Lire les données du graphe ===
        graph_path = _find_member("graph_data.json")
        if not graph_path:
            tar.close()
            raise ValueError("graph_data.json introuvable dans l'archive")
        
        graph_data = json.loads(_read_member(graph_path).decode("utf-8"))
        
        # Vérifier le checksum
        graph_json = json.dumps(graph_data, ensure_ascii=False, indent=2)
        actual_hash = hashlib.sha256(graph_json.encode()).hexdigest()
        expected_hash = manifest.get("checksums", {}).get("graph_data")
        if expected_hash and actual_hash != expected_hash:
            tar.close()
            raise ValueError("Checksum graphe invalide ! Archive corrompue ?")
        
        await _log("✅ Données graphe vérifiées (checksum OK)")
        
        # === 4. Lire les vecteurs Qdrant ===
        qdrant_path = _find_member("qdrant_vectors.jsonl")
        if not qdrant_path:
            tar.close()
            raise ValueError("qdrant_vectors.jsonl introuvable dans l'archive")
        
        qdrant_jsonl = _read_member(qdrant_path).decode("utf-8")
        qdrant_points = []
        if qdrant_jsonl.strip():
            for line in qdrant_jsonl.strip().split("\n"):
                if line.strip():
                    qdrant_points.append(json.loads(line))
        
        # Vérifier le checksum
        actual_hash = hashlib.sha256(qdrant_jsonl.encode()).hexdigest()
        expected_hash = manifest.get("checksums", {}).get("qdrant_vectors")
        if expected_hash and actual_hash != expected_hash:
            tar.close()
            raise ValueError("Checksum vecteurs invalide ! Archive corrompue ?")
        
        await _log(f"✅ {len(qdrant_points)} vecteurs chargés (checksum OK)")
        
        # === 5. Re-uploader les documents S3 ===
        await _log("📄 Re-upload des documents sur S3...")
        
        # Trouver les fichiers documents/ dans l'archive
        doc_members = [m for m in members if "/documents/" in m and not m.endswith("/")]
        
        # Lire aussi document_keys.json pour les métadonnées (clé S3 originale)
        doc_keys_path = _find_member("document_keys.json")
        doc_keys_list = []
        if doc_keys_path:
            doc_keys_list = json.loads(_read_member(doc_keys_path).decode("utf-8"))
        
        # Construire un mapping filename → key S3 original
        filename_to_key = {}
        for dk in doc_keys_list:
            fn = dk.get("filename", "")
            key = dk.get("key", "")
            if fn and key:
                filename_to_key[fn] = key
        
        docs_uploaded = 0
        docs_skipped = 0
        
        for doc_member in doc_members:
            # Extraire le nom de fichier
            doc_filename = doc_member.split("/documents/", 1)[-1]
            if not doc_filename:
                continue
            
            # === SÉCURITÉ : anti path-traversal ===
            # Rejeter les noms contenant ../ ou commençant par /
            if ".." in doc_filename or doc_filename.startswith("/"):
                print(f"🔒 [RestoreArchive] Nom de fichier rejeté (path traversal): "
                      f"'{doc_filename}'", file=sys.stderr)
                docs_skipped += 1
                continue
            # Ne garder que le basename (pas de sous-dossiers inattendus)
            import os.path as _osp
            safe_filename = _osp.basename(doc_filename)
            if safe_filename != doc_filename:
                print(f"🔒 [RestoreArchive] Nom normalisé: '{doc_filename}' → "
                      f"'{safe_filename}'", file=sys.stderr)
                doc_filename = safe_filename
            
            # Lire le contenu
            doc_content = _read_member(doc_member)
            
            # Trouver la clé S3 originale
            s3_key = filename_to_key.get(doc_filename)
            
            if s3_key:
                # Upload directement avec la clé originale
                try:
                    content_type = self._storage._guess_content_type(doc_filename)
                    self._storage._client.put_object(
                        Bucket=self._storage._bucket,
                        Key=s3_key,
                        Body=doc_content,
                        ContentType=content_type,
                        Metadata={
                            "memory_id": memory_id,
                            "original_filename": self._storage._sanitize_metadata_value(doc_filename),
                            "restored_from": "archive",
                        }
                    )
                    docs_uploaded += 1
                    await _log(f"  📄 {doc_filename} ({self._human_size(len(doc_content))})")
                except Exception as e:
                    await _log(f"  ⚠️ {doc_filename}: {e}")
            else:
                # Pas de clé S3 connue, upload avec upload_document
                try:
                    await self._storage.upload_document(
                        memory_id=memory_id,
                        filename=doc_filename,
                        content=doc_content
                    )
                    docs_uploaded += 1
                    await _log(f"  📄 {doc_filename} (nouvelle clé S3)")
                except Exception as e:
                    await _log(f"  ⚠️ {doc_filename}: {e}")
        
        if not doc_members:
            await _log("⚠️ Aucun document dans l'archive (backup léger)")
        else:
            await _log(f"✅ {docs_uploaded} documents uploadés sur S3")
        
        tar.close()
        
        # === 6. Restaurer le graphe Neo4j ===
        await _log("📊 Restauration du graphe Neo4j...")
        graph_counters = await self._graph.import_memory_data(graph_data)
        await _log(f"✅ Graphe restauré: {graph_counters}")
        
        # === 7. Restaurer les vecteurs Qdrant ===
        await _log("🔢 Restauration des vecteurs Qdrant...")
        vectors_imported = await self._vectors.import_collection(memory_id, qdrant_points)
        await _log(f"✅ Qdrant: {vectors_imported} vecteurs restaurés")
        
        total_elapsed = round(_time.monotonic() - _t0, 1)
        await _log(f"🏁 Restauration depuis archive terminée en {total_elapsed}s")
        
        return {
            "status": "ok",
            "source": "archive",
            "memory_id": memory_id,
            "graph": graph_counters,
            "qdrant_vectors_restored": vectors_imported,
            "s3_documents_uploaded": docs_uploaded,
            "s3_documents_skipped": docs_skipped,
            "elapsed_seconds": total_elapsed,
        }
    
    # =========================================================================
    # Delete backup
    # =========================================================================
    
    async def delete_backup(self, backup_id: str) -> Dict[str, Any]:
        """
        Supprime un backup de S3.
        
        Args:
            backup_id: ID du backup (format: "memory_id/timestamp")
            
        Returns:
            Dict avec le nombre de fichiers supprimés
        """
        # Valider le backup_id (anti path-traversal)
        memory_id, timestamp = self._validate_backup_id(backup_id)
        prefix = self._backup_s3_prefix(memory_id, timestamp)
        
        result = await self._storage.delete_prefix(f"{prefix}/")
        
        print(f"🗑️ [Backup] Supprimé: {backup_id} "
              f"({result['deleted_count']} fichiers)", file=sys.stderr)
        
        return {
            "status": "ok",
            "backup_id": backup_id,
            "files_deleted": result["deleted_count"],
            "errors": result.get("error_count", 0)
        }
    
    # =========================================================================
    # Rétention
    # =========================================================================
    
    async def _apply_retention(self, memory_id: str) -> List[str]:
        """
        Applique la politique de rétention pour une mémoire.
        
        Garde les N derniers backups (BACKUP_RETENTION_COUNT) et supprime les plus anciens.
        
        Args:
            memory_id: ID de la mémoire
            
        Returns:
            Liste des backup_ids supprimés
        """
        if self._retention <= 0:
            return []
        
        backups = await self.list_backups(memory_id=memory_id)
        
        if len(backups) <= self._retention:
            return []
        
        # Les backups sont triés par date décroissante, supprimer les plus anciens
        to_delete = backups[self._retention:]
        deleted_ids = []
        
        for backup in to_delete:
            bid = backup.get("backup_id", "")
            if bid:
                try:
                    await self.delete_backup(bid)
                    deleted_ids.append(bid)
                except Exception as e:
                    print(f"⚠️ [Retention] Erreur suppression {bid}: {e}",
                          file=sys.stderr)
        
        return deleted_ids
    
    # =========================================================================
    # Helpers
    # =========================================================================
    
    async def _download_json(self, key: str) -> dict:
        """Télécharge et parse un fichier JSON depuis S3."""
        text = await self._download_text(key)
        return json.loads(text)
    
    async def _download_text(self, key: str) -> str:
        """Télécharge un fichier texte depuis S3."""
        try:
            response = self._storage._client.get_object(
                Bucket=self._storage._bucket,
                Key=key
            )
            return response["Body"].read().decode("utf-8")
        except Exception as e:
            raise FileNotFoundError(f"Fichier S3 non trouvé: {key} ({e})")
    
    @staticmethod
    def _human_size(size_bytes: int) -> str:
        """Convertit des bytes en taille lisible."""
        size = float(size_bytes)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


# Singleton
_backup_service: Optional[BackupService] = None


def get_backup_service() -> BackupService:
    """Retourne l'instance singleton du BackupService."""
    global _backup_service
    if _backup_service is None:
        from .graph import get_graph_service
        from .vector_store import get_vector_store
        from .storage import get_storage_service
        
        _backup_service = BackupService(
            graph_service=get_graph_service(),
            vector_store=get_vector_store(),
            storage_service=get_storage_service(),
        )
    return _backup_service
