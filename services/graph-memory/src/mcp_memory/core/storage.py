# -*- coding: utf-8 -*-
"""
StorageService - Client S3 pour le stockage des documents.

Gère le stockage et la récupération des documents originaux sur S3 Cloud Temple.
"""

import asyncio
import os
import hashlib
import sys
from typing import Optional, BinaryIO
from datetime import datetime, timedelta
from urllib.parse import quote as url_quote
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError

from ..config import get_settings
from .egress import (
    botocore_proxies,
    redact_proxy_errors_async,
    redact_proxy_secrets,
)
from .maintenance import (
    MAX_REINDEX_SOURCE_OBJECTS,
    MAX_REINDEX_SOURCE_TOTAL_BYTES,
    ReindexSourceLimitExceeded,
)
from .validators import MAX_INGEST_SIZE_BYTES


# P12-3 (Hivemind #268) : frontière de redaction partagée (voir egress.py) —
# les erreurs botocore de connexion proxy embarquent l'URL proxy BRUTE.
_redact_proxy_errors = redact_proxy_errors_async


class StorageService:
    """
    Service de stockage S3 pour les documents.
    
    Responsabilités:
    - Upload de documents vers S3
    - Download de documents depuis S3
    - Génération d'URLs signées
    - Vérification d'existence
    - Suppression de documents
    """
    
    def __init__(self):
        """Initialise les clients S3 avec signatures adaptées."""
        settings = get_settings()

        # Désactiver le calcul du checksum par le SDK
        os.environ["AWS_REQUEST_CHECKSUM_CALCULATION"] = "when_required"

        # Région Dell ECS Cloud Temple
        region = settings.s3_region_name if settings.s3_region_name else "fr1"

        # Hivemind patch (P7-9, #135): mirror Hivemind's ``S3_SIGNATURE_MODE``
        # exactly like auth/s3_token_validator.py does for the token-store
        # read. The vendored baseline hardcoded SigV2 for every data operation
        # (Dell ECS default), which breaks providers that reject SigV2 (MinIO,
        # AWS S3) with SignatureDoesNotMatch on the first document upload.
        #   dual  (default) -> SigV2 data client + SigV4 metadata client
        #                      (byte-identical to the vendored baseline).
        #   sigv4           -> the SigV4 client serves EVERY operation.
        self.signature_mode = self._resolve_signature_mode()

        # ── Proxy HTTP sortant (P12-3, Hivemind #268) ─────────
        # Même règle uniforme que le StorageService du cœur Hivemind : quand
        # PROXY_URL est définie, TOUS les clients S3 du service la suivent —
        # classification statique par classe de client, jamais d'heuristique
        # DNS/IP sur l'endpoint. La topologie dev (MinIO même-stack) ne
        # définit pas PROXY_URL et reste donc directe.
        _proxies = botocore_proxies(settings.proxy_url)

        # Client SigV4 — toujours instancié. HEAD/LIST (métadonnées) en mode
        # 'dual' ; TOUTES les opérations en mode 'sigv4'.
        config_v4 = Config(
            region_name=region,
            signature_version='s3v4',
            s3={'addressing_style': 'path', 'payload_signing_enabled': False},
            retries={'max_attempts': 3, 'mode': 'adaptive'},
            **({"proxies": _proxies} if _proxies else {})
        )

        self._client_v4 = boto3.client(
            's3',
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=region,
            config=config_v4
        )

        if self.signature_mode == "dual":
            # Client SigV2 pour PUT/GET/DELETE (opérations sur objets)
            # Tests validés: PUT ✅, GET ✅, DELETE ✅ (Dell ECS Cloud Temple)
            config_v2 = Config(
                region_name=region,
                signature_version='s3',  # SigV2 legacy
                s3={'addressing_style': 'path'},
                retries={'max_attempts': 3, 'mode': 'adaptive'},
                **({"proxies": _proxies} if _proxies else {})
            )

            self._client_v2 = boto3.client(
                's3',
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key_id,
                aws_secret_access_key=settings.s3_secret_access_key,
                region_name=region,
                config=config_v2
            )
        else:
            # Mode 'sigv4' : les opérations de données réutilisent le client
            # SigV4. L'attribut garde son nom vendored historique pour ne
            # toucher aucun site d'appel.
            self._client_v2 = self._client_v4

        # Client par défaut pour les opérations de données
        self._client = self._client_v2

        self._bucket = settings.s3_bucket_name
        self._endpoint_url = settings.s3_endpoint_url

    @staticmethod
    def _resolve_signature_mode() -> str:
        """Mirror Hivemind's ``S3_SIGNATURE_MODE`` exactly (single source of truth).

        Same contract as ``auth.s3_token_validator.S3TokenValidator.
        _default_signature_mode``: the embedded GM shares Hivemind's ``.env``,
        so reading the SAME env var keeps the document-storage clients on the
        mode the operator set for the whole stack. Unknown values fall back to
        ``dual`` (the legacy Dell ECS behavior), matching the validator.
        """
        mode = os.getenv("S3_SIGNATURE_MODE", "dual").strip().lower()
        return mode if mode in ("dual", "sigv4") else "dual"
    
    def _get_key(self, memory_id: str, filename: str, doc_hash: Optional[str] = None) -> str:
        """
        Construit la clé S3 pour un document.
        
        Format: {memory_id}/documents/{hash}_{filename}
        ou si pas de hash: {memory_id}/documents/{filename}
        """
        if doc_hash:
            # Utilise les 8 premiers caractères du hash pour unicité
            return f"{memory_id}/documents/{doc_hash[:8]}_{filename}"
        return f"{memory_id}/documents/{filename}"
    
    @staticmethod
    def compute_hash(content: bytes) -> str:
        """Calcule le hash SHA256 d'un contenu."""
        return hashlib.sha256(content).hexdigest()
    
    @_redact_proxy_errors
    async def upload_document(
        self,
        memory_id: str,
        filename: str,
        content: bytes,
        content_type: Optional[str] = None,
        metadata: Optional[dict] = None
    ) -> dict:
        """
        Upload un document vers S3.
        
        Args:
            memory_id: ID de la mémoire
            filename: Nom du fichier
            content: Contenu binaire du document
            content_type: Type MIME (optionnel, détecté sinon)
            metadata: Métadonnées additionnelles
            
        Returns:
            dict avec uri, hash, size_bytes
        """
        doc_hash = self.compute_hash(content)
        key = self._get_key(memory_id, filename, doc_hash)
        
        # Détection du content-type si non fourni
        if not content_type:
            content_type = self._guess_content_type(filename)
        
        # Métadonnées S3 - doivent être ASCII uniquement
        # On URL-encode les valeurs contenant des caractères non-ASCII
        s3_metadata = {}
        if metadata:
            for k, v in metadata.items():
                s3_metadata[k] = self._sanitize_metadata_value(str(v))
        # Ownership and retained-source evidence are authoritative outputs of
        # this method, never caller-overridable user metadata.
        s3_metadata.update({
            'memory_id': memory_id,
            'original_filename': self._sanitize_metadata_value(filename),
            'doc_hash': doc_hash,
            'uploaded_at': datetime.utcnow().isoformat()
        })
        
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                Metadata=s3_metadata
            )
            
            uri = f"s3://{self._bucket}/{key}"
            
            print(f"📤 [S3] Document uploaded: {uri}", file=sys.stderr)
            
            return {
                "uri": uri,
                "key": key,
                "hash": doc_hash,
                "size_bytes": len(content),
                "content_type": content_type
            }
            
        except ClientError as e:
            print(f"❌ [S3] Upload error: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            raise
    
    @_redact_proxy_errors
    async def download_document(self, memory_id: str, key_or_uri: str) -> bytes:
        """
        Télécharge un document depuis S3.
        
        Args:
            memory_id: ID de la mémoire (pour vérification)
            key_or_uri: Clé S3 ou URI complète (s3://bucket/key)
            
        Returns:
            Contenu binaire du document
        """
        key = self._parse_key(key_or_uri)
        
        # Vérification que le document appartient à la mémoire
        if not key.startswith(f"{memory_id}/"):
            raise PermissionError(f"Document does not belong to memory {memory_id}")
        
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            content = response['Body'].read()
            
            print(f"📥 [S3] Document downloaded: {key} ({len(content)} bytes)", file=sys.stderr)
            return content
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                raise FileNotFoundError(f"Document not found: {key}")
            raise
    
    @_redact_proxy_errors
    async def delete_document(self, memory_id: str, key_or_uri: str) -> bool:
        """
        Supprime un document de S3.
        
        Args:
            memory_id: ID de la mémoire (pour vérification)
            key_or_uri: Clé S3 ou URI complète
            
        Returns:
            True si supprimé, False si n'existait pas
        """
        key = self._parse_key(key_or_uri)
        
        # Vérification que le document appartient à la mémoire
        if not key.startswith(f"{memory_id}/"):
            raise PermissionError(f"Document does not belong to memory {memory_id}")
        
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            print(f"🗑️ [S3] Document deleted: {key}", file=sys.stderr)
            return True
            
        except ClientError as e:
            print(f"❌ [S3] Deletion error: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            return False
    
    @_redact_proxy_errors
    async def document_exists(self, key_or_uri: str) -> bool:
        """Vérifie si un document existe dans S3."""
        key = self._parse_key(key_or_uri)
        
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False
    
    @_redact_proxy_errors
    async def get_signed_url(
        self,
        key_or_uri: str,
        expires_in_seconds: int = 3600
    ) -> str:
        """
        Génère une URL signée pour accéder au document.
        
        Args:
            key_or_uri: Clé S3 ou URI
            expires_in_seconds: Durée de validité (défaut: 1 heure)
            
        Returns:
            URL signée
        """
        key = self._parse_key(key_or_uri)
        
        url = self._client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self._bucket, 'Key': key},
            ExpiresIn=expires_in_seconds
        )
        
        return url
    
    @_redact_proxy_errors
    async def list_documents(self, memory_id: str, prefix: str = "") -> list:
        """
        Liste les documents d'une mémoire.
        
        Utilise SigV4 pour LIST (compatible Dell ECS).
        
        Args:
            memory_id: ID de la mémoire
            prefix: Préfixe additionnel (optionnel)
            
        Returns:
            Liste des objets S3
        """
        full_prefix = f"{memory_id}/documents/{prefix}"
        
        try:
            # SigV4 pour LIST (Dell ECS)
            response = self._client_v4.list_objects_v2(
                Bucket=self._bucket,
                Prefix=full_prefix
            )
            
            objects = []
            for obj in response.get('Contents', []):
                objects.append({
                    'key': obj['Key'],
                    'size': obj['Size'],
                    'last_modified': obj['LastModified'].isoformat()
                })
            
            return objects
            
        except ClientError as e:
            print(f"❌ [S3] Listing error: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            return []
    
    @_redact_proxy_errors
    async def check_documents(self, uris: list) -> dict:
        """
        Vérifie l'accessibilité de documents S3 à partir d'une liste d'URIs.
        
        Pour chaque URI, tente un HEAD pour vérifier l'existence et récupérer
        la taille. Utilise le client SigV4 pour HEAD (compatible Dell ECS).
        
        Args:
            uris: Liste d'URIs S3 (format s3://bucket/key)
            
        Returns:
            dict avec:
              - total: nombre total de documents vérifiés
              - accessible: nombre de documents accessibles
              - missing: nombre de documents manquants
              - errors: nombre d'erreurs
              - total_size_bytes: taille totale des documents accessibles
              - details: liste de {uri, status, size_bytes, error}
        """
        details = []
        accessible = 0
        missing = 0
        errors = 0
        total_size = 0
        
        for uri in uris:
            key = self._parse_key(uri)
            try:
                # HEAD avec SigV4 (plus fiable pour les métadonnées sur Dell ECS)
                response = self._client_v4.head_object(Bucket=self._bucket, Key=key)
                size = response.get('ContentLength', 0)
                details.append({
                    "uri": uri,
                    "key": key,
                    "status": "ok",
                    "size_bytes": size,
                    "content_type": response.get('ContentType', ''),
                    "last_modified": response.get('LastModified', '').isoformat() if hasattr(response.get('LastModified', ''), 'isoformat') else str(response.get('LastModified', '')),
                    "error": None
                })
                accessible += 1
                total_size += size
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                if error_code in ('404', 'NoSuchKey', 'Not Found'):
                    details.append({
                        "uri": uri,
                        "key": key,
                        "status": "missing",
                        "size_bytes": 0,
                        "error": "Document not found in S3"
                    })
                    missing += 1
                else:
                    details.append({
                        "uri": uri,
                        "key": key,
                        "status": "error",
                        "size_bytes": 0,
                        # P12-3 R1 : chemin d'erreur RÉCUPÉRÉ (jamais re-levé),
                        # donc hors de portée du décorateur — redaction locale.
                        "error": redact_proxy_secrets(
                            f"S3 error [{error_code}]: "
                            f"{e.response.get('Error', {}).get('Message', str(e))}"
                        )
                    })
                    errors += 1
            except Exception as e:
                details.append({
                    "uri": uri,
                    "key": key,
                    "status": "error",
                    "size_bytes": 0,
                    # P12-3 R1 : une ProxyConnectionError récupérée ici porte
                    # l'URL proxy BRUTE (credentials) — jamais dans le payload.
                    "error": redact_proxy_secrets(str(e))
                })
                errors += 1
        
        return {
            "total": len(uris),
            "accessible": accessible,
            "missing": missing,
            "errors": errors,
            "total_size_bytes": total_size,
            "details": details
        }
    
    @_redact_proxy_errors
    async def list_all_objects(self, prefix: str = "") -> list:
        """
        Liste TOUS les objets du bucket (avec pagination).
        
        Utilise le client SigV4 (compatible Dell ECS pour LIST).
        
        Args:
            prefix: Préfixe pour filtrer (optionnel)
            
        Returns:
            Liste de {key, uri, size, last_modified}
        """
        objects = []
        continuation_token = None
        
        try:
            while True:
                params = {
                    'Bucket': self._bucket,
                    'Prefix': prefix,
                    'MaxKeys': 1000
                }
                if continuation_token:
                    params['ContinuationToken'] = continuation_token
                
                # SigV4 pour LIST (Dell ECS)
                response = self._client_v4.list_objects_v2(**params)
                
                for obj in response.get('Contents', []):
                    objects.append({
                        'key': obj['Key'],
                        'uri': f"s3://{self._bucket}/{obj['Key']}",
                        'size': obj['Size'],
                        'last_modified': obj['LastModified'].isoformat() if hasattr(obj['LastModified'], 'isoformat') else str(obj['LastModified'])
                    })
                
                # Pagination
                if response.get('IsTruncated'):
                    continuation_token = response.get('NextContinuationToken')
                else:
                    break
            
            return objects
            
        except ClientError as e:
            print(f"❌ [S3] Full listing error: {redact_proxy_secrets(str(e))}", file=sys.stderr)
            return []

    @_redact_proxy_errors
    async def list_reindex_objects(self, memory_id: str) -> list:
        """List and HEAD every retained source object without lossy fallback.

        Unlike the historical operator inventory, any LIST/HEAD ambiguity is
        raised to the maintenance boundary. Returning an empty list on a
        backend error would make an unverifiable namespace look authoritative.
        """
        if type(memory_id) is not str or not memory_id:
            raise ValueError("memory_id is required")
        prefix = f"{memory_id}/documents/"
        objects = []
        listed_size = 0
        continuation_token = None
        seen_continuation_tokens: set[str] = set()
        seen_keys: set[str] = set()
        page_count = 0
        while True:
            page_count += 1
            if page_count > MAX_REINDEX_SOURCE_OBJECTS + 1:
                raise ReindexSourceLimitExceeded(
                    "source inventory limit exceeded"
                )
            params = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if continuation_token is not None:
                params["ContinuationToken"] = continuation_token
            response = await asyncio.to_thread(
                self._client_v4.list_objects_v2,
                **params,
            )
            contents = response.get("Contents", [])
            if type(contents) is not list or len(contents) > params["MaxKeys"]:
                raise RuntimeError("invalid source inventory")
            is_truncated = response.get("IsTruncated")
            if (
                type(is_truncated) is not bool
                or (
                    is_truncated is False
                    and "NextContinuationToken" in response
                )
            ):
                raise RuntimeError("invalid source inventory")
            next_token = None
            if is_truncated is True:
                next_token = response.get("NextContinuationToken")
                if (
                    not contents
                    or type(next_token) is not str
                    or not next_token
                    or next_token in seen_continuation_tokens
                ):
                    raise RuntimeError("invalid source inventory")
            page_objects: list[tuple[str, int]] = []
            page_keys: set[str] = set()
            page_size = 0
            for item in contents:
                if type(item) is not dict:
                    raise RuntimeError("invalid source inventory")
                key = item.get("Key")
                size = item.get("Size")
                if (
                    type(key) is not str
                    or not key.startswith(prefix)
                    or key == prefix
                    or type(size) is not int
                    or size < 0
                    or key in seen_keys
                    or key in page_keys
                ):
                    raise RuntimeError("invalid source inventory")
                if size > MAX_INGEST_SIZE_BYTES:
                    raise ReindexSourceLimitExceeded(
                        "source inventory limit exceeded"
                    )
                page_objects.append((key, size))
                page_keys.add(key)
                page_size += size
            if (
                len(objects) + len(page_objects) > MAX_REINDEX_SOURCE_OBJECTS
                or listed_size + page_size > MAX_REINDEX_SOURCE_TOTAL_BYTES
            ):
                # Refuse an over-limit page before any of its per-object HEADs.
                raise ReindexSourceLimitExceeded(
                    "source inventory limit exceeded"
                )
            listed_size += page_size
            seen_keys.update(page_keys)
            for key, size in page_objects:
                head = await asyncio.to_thread(
                    self._client_v4.head_object,
                    Bucket=self._bucket,
                    Key=key,
                )
                head_size = head.get("ContentLength")
                metadata = head.get("Metadata", {})
                if (
                    type(head_size) is not int
                    or head_size < 0
                    or head_size != size
                    or type(metadata) is not dict
                    or any(
                        type(meta_key) is not str or type(meta_value) is not str
                        for meta_key, meta_value in metadata.items()
                    )
                ):
                    raise RuntimeError("invalid source inventory")
                objects.append(
                    {
                        "key": key,
                        "uri": f"s3://{self._bucket}/{key}",
                        "size_bytes": size,
                        "metadata": dict(metadata),
                    }
                )
            if is_truncated is True:
                continuation_token = next_token
                seen_continuation_tokens.add(continuation_token)
                continue
            break
        return objects

    @_redact_proxy_errors
    async def read_reindex_object(
        self,
        memory_id: str,
        key: str,
        expected_size: int,
    ) -> bytes:
        """Read one exact retained source without emitting its key to logs."""
        if (
            type(memory_id) is not str
            or not memory_id
            or type(key) is not str
            or not key.startswith(f"{memory_id}/documents/")
            or key == f"{memory_id}/documents/"
        ):
            raise PermissionError("source object is outside the memory namespace")
        if (
            type(expected_size) is not int
            or expected_size < 0
            or expected_size > MAX_INGEST_SIZE_BYTES
        ):
            raise ValueError("source object size is invalid")
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=key,
        )
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise RuntimeError("invalid source object response")

        def read_and_close() -> bytes:
            try:
                content_length = response.get("ContentLength")
                if (
                    type(content_length) is not int
                    or content_length != expected_size
                ):
                    raise RuntimeError("invalid source object response")
                return body.read(expected_size + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()

        content = await asyncio.to_thread(read_and_close)
        if type(content) is not bytes or len(content) != expected_size:
            raise RuntimeError("invalid source object response")
        return content
    
    @_redact_proxy_errors
    async def delete_prefix(self, prefix: str) -> dict:
        """
        Supprime tous les objets S3 sous un préfixe donné.
        
        Utilisé pour nettoyer tous les fichiers d'une mémoire.
        
        Args:
            prefix: Préfixe S3 (ex: "quoteflow-legal/")
            
        Returns:
            dict avec deleted_count et errors
        """
        objects = await self.list_all_objects(prefix=prefix)
        deleted_count = 0
        error_count = 0
        
        for obj in objects:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=obj['key'])
                deleted_count += 1
                print(f"🗑️ [S3] Deleted: {obj['key']}", file=sys.stderr)
            except ClientError as e:
                error_count += 1
                print(f"❌ [S3] Error deleting {obj['key']}: {redact_proxy_secrets(str(e))}", file=sys.stderr)
        
        return {
            "deleted_count": deleted_count,
            "error_count": error_count,
            "total_found": len(objects)
        }
    
    @_redact_proxy_errors
    async def delete_objects(self, keys: list) -> dict:
        """
        Supprime une liste d'objets S3 par leurs clés.
        
        Args:
            keys: Liste de clés S3 ou URIs
            
        Returns:
            dict avec deleted_count et errors
        """
        deleted_count = 0
        error_count = 0
        
        for key_or_uri in keys:
            key = self._parse_key(key_or_uri)
            try:
                self._client.delete_object(Bucket=self._bucket, Key=key)
                deleted_count += 1
                print(f"🗑️ [S3] Deleted: {key}", file=sys.stderr)
            except ClientError as e:
                error_count += 1
                print(f"❌ [S3] Error deleting {key}: {redact_proxy_secrets(str(e))}", file=sys.stderr)
        
        return {
            "deleted_count": deleted_count,
            "error_count": error_count
        }
    
    @_redact_proxy_errors
    async def test_connection(self) -> dict:
        """
        Teste la connexion S3 en utilisant PUT/GET (compatible SigV2).
        
        Returns:
            dict avec status, bucket, message
        """
        test_key = "_health_check/test.txt"
        test_content = b"health check"
        
        try:
            # Test avec PUT/GET qui fonctionnent avec SigV2
            self._client_v2.put_object(
                Bucket=self._bucket,
                Key=test_key,
                Body=test_content
            )
            
            # Vérifier qu'on peut lire
            response = self._client_v2.get_object(Bucket=self._bucket, Key=test_key)
            content = response['Body'].read()
            
            # Nettoyer
            self._client_v2.delete_object(Bucket=self._bucket, Key=test_key)
            
            if content == test_content:
                return {
                    "status": "ok",
                    "bucket": self._bucket,
                    "endpoint": self._endpoint_url,
                    "message": "S3 connection succeeded (PUT/GET/DELETE validated)"
                }
            else:
                return {
                    "status": "warning",
                    "bucket": self._bucket,
                    "endpoint": self._endpoint_url,
                    "message": "Connection succeeded but content validation failed"
                }
            
        except NoCredentialsError:
            return {
                "status": "error",
                "bucket": self._bucket,
                "endpoint": self._endpoint_url,
                "message": "S3 credentials are invalid or missing"
            }
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            return {
                "status": "error",
                "bucket": self._bucket,
                "endpoint": self._endpoint_url,
                # P12-3 R7 : chemin RÉCUPÉRÉ (jamais re-levé) copié tel quel
                # par system_health — hors de portée du décorateur, redaction
                # locale du message serveur avant retour.
                "message": redact_proxy_secrets(
                    f"S3 error [{error_code}]: {error_msg}"
                )
            }
    
    def _parse_key(self, key_or_uri: str) -> str:
        """Extrait la clé S3 d'une URI ou retourne la clé directement."""
        if key_or_uri.startswith("s3://"):
            # Format: s3://bucket/key
            parts = key_or_uri[5:].split("/", 1)
            if len(parts) == 2:
                return parts[1]
            raise ValueError(f"Invalid S3 URI: {key_or_uri}")
        return key_or_uri
    
    @staticmethod
    def _sanitize_metadata_value(value: str) -> str:
        """
        Sanitise une valeur pour les métadonnées S3 (ASCII uniquement).
        
        URL-encode les caractères non-ASCII pour compatibilité S3/Dell ECS.
        Ex: "Conditions Générales" → "Conditions%20G%C3%A9n%C3%A9rales"
        """
        try:
            value.encode('ascii')
            return value  # Déjà ASCII, pas besoin d'encoder
        except UnicodeEncodeError:
            return url_quote(value, safe='')
    
    @staticmethod
    def _guess_content_type(filename: str) -> str:
        """Devine le content-type à partir de l'extension."""
        ext = filename.lower().split('.')[-1] if '.' in filename else ''
        
        content_types = {
            'pdf': 'application/pdf',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'doc': 'application/msword',
            'txt': 'text/plain',
            'md': 'text/markdown',
            'json': 'application/json',
            'xml': 'application/xml',
            'html': 'text/html',
            'csv': 'text/csv',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'xls': 'application/vnd.ms-excel',
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
        }
        
        return content_types.get(ext, 'application/octet-stream')


# Singleton pour usage global
_storage_service: Optional[StorageService] = None


def get_storage_service() -> StorageService:
    """Retourne l'instance singleton du StorageService."""
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service
