# -*- coding: utf-8 -*-
"""
TokenManager - Gestion des tokens d'authentification clients.

Les tokens sont stockés dans Neo4j sous forme de nœuds :Token.
Seul le hash du token est stocké (pas le token en clair).
"""

import sys
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List

from ..config import get_settings
from ..core.models import TokenInfo, TokenCreateRequest


class TokenManager:
    """
    Gestionnaire de tokens clients.
    
    Les tokens permettent aux clients (QuoteFlow, Vela, etc.) de s'authentifier
    auprès du service MCP Memory.
    
    Structure du token dans Neo4j:
    (:Token {
        hash: "sha256_du_token",
        client_name: "quoteflow",
        permissions: ["read", "write"],
        memory_ids: ["mem1", "mem2"],  # vide = accès à toutes
        created_at: datetime,
        expires_at: datetime,
        is_active: true
    })
    """
    
    def __init__(self, graph_service=None):
        """
        Initialise le TokenManager.
        
        Args:
            graph_service: Instance de GraphService (lazy-loaded si None)
        """
        self._graph_service = graph_service
        self._settings = get_settings()
    
    @property
    def graph(self):
        """Lazy-load du GraphService."""
        if self._graph_service is None:
            from ..core.graph import get_graph_service
            self._graph_service = get_graph_service()
        return self._graph_service
    
    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash un token avec SHA256."""
        return hashlib.sha256(token.encode()).hexdigest()
    
    @staticmethod
    def _generate_token() -> str:
        """Génère un token sécurisé."""
        return secrets.token_urlsafe(32)
    
    async def create_token(
        self,
        client_name: str,
        permissions: List[str] = None,
        memory_ids: List[str] = None,
        expires_in_days: Optional[int] = None,
        email: Optional[str] = None
    ) -> str:
        """
        Crée un nouveau token pour un client.
        
        Args:
            client_name: Nom du client (ex: "quoteflow")
            permissions: Liste des permissions (défaut: ["read", "write"])
            memory_ids: IDs des mémoires autorisées (vide = toutes)
            expires_in_days: Durée de validité en jours (None = pas d'expiration)
            email: Adresse email du propriétaire du token (optionnel)
            
        Returns:
            Le token en clair (à fournir au client, ne sera plus accessible ensuite)
        """
        if permissions is None:
            permissions = ["read", "write"]
        if memory_ids is None:
            memory_ids = []
        
        # Générer le token
        token = self._generate_token()
        token_hash = self._hash_token(token)
        
        # Calculer l'expiration
        expires_at = None
        if expires_in_days:
            expires_at = datetime.utcnow() + timedelta(days=expires_in_days)
        
        # Stocker dans Neo4j
        async with self.graph.session() as session:
            await session.run(
                """
                CREATE (t:Token {
                    hash: $hash,
                    client_name: $client_name,
                    email: $email,
                    permissions: $permissions,
                    memory_ids: $memory_ids,
                    created_at: datetime(),
                    expires_at: $expires_at,
                    is_active: true
                })
                """,
                hash=token_hash,
                client_name=client_name,
                email=email,
                permissions=permissions,
                memory_ids=memory_ids,
                expires_at=expires_at.isoformat() if expires_at else None
            )
        
        print(f"🔑 [Auth] Token créé pour client '{client_name}'", file=sys.stderr)
        
        # Retourner le token en clair (seule fois où il est accessible)
        return token
    
    async def validate_token(self, token: str) -> Optional[TokenInfo]:
        """
        Valide un token et retourne ses informations.
        
        Args:
            token: Le token en clair
            
        Returns:
            TokenInfo si valide, None sinon
        """
        token_hash = self._hash_token(token)
        
        async with self.graph.session() as session:
            result = await session.run(
                """
                MATCH (t:Token {hash: $hash, is_active: true})
                RETURN t
                """,
                hash=token_hash
            )
            
            record = await result.single()
            
            if not record:
                return None
            
            node = record["t"]
            
            # Vérifier l'expiration
            expires_at = None
            if node.get("expires_at"):
                try:
                    expires_at = datetime.fromisoformat(node["expires_at"])
                    if expires_at < datetime.utcnow():
                        print(f"⚠️ [Auth] Token expiré pour '{node['client_name']}'", file=sys.stderr)
                        return None
                except:
                    pass
            
            return TokenInfo(
                token_hash=node["hash"],
                client_name=node["client_name"],
                email=node.get("email"),
                permissions=node.get("permissions", []),
                memory_ids=node.get("memory_ids", []),
                created_at=node["created_at"].to_native() if node.get("created_at") else datetime.utcnow(),
                expires_at=expires_at,
                is_active=node.get("is_active", True)
            )
    
    async def revoke_token(self, token_hash: str) -> bool:
        """
        Révoque un token (le désactive).
        
        Args:
            token_hash: Hash du token à révoquer
            
        Returns:
            True si révoqué, False si non trouvé
        """
        async with self.graph.session() as session:
            result = await session.run(
                """
                MATCH (t:Token {hash: $hash})
                SET t.is_active = false, t.revoked_at = datetime()
                RETURN t
                """,
                hash=token_hash
            )
            
            record = await result.single()
            
            if record:
                print(f"🚫 [Auth] Token révoqué: {token_hash[:8]}...", file=sys.stderr)
                return True
            return False
    
    async def list_tokens(self, include_revoked: bool = False) -> List[TokenInfo]:
        """
        Liste tous les tokens.
        
        Args:
            include_revoked: Inclure les tokens révoqués
            
        Returns:
            Liste des TokenInfo
        """
        async with self.graph.session() as session:
            query = "MATCH (t:Token) "
            if not include_revoked:
                query += "WHERE t.is_active = true "
            query += "RETURN t ORDER BY t.created_at DESC"
            
            result = await session.run(query)
            
            tokens = []
            async for record in result:
                node = record["t"]
                
                expires_at = None
                if node.get("expires_at"):
                    try:
                        expires_at = datetime.fromisoformat(node["expires_at"])
                    except:
                        pass
                
                tokens.append(TokenInfo(
                    token_hash=node["hash"],
                    client_name=node["client_name"],
                    email=node.get("email"),
                    permissions=node.get("permissions", []),
                    memory_ids=node.get("memory_ids", []),
                    created_at=node["created_at"].to_native() if node.get("created_at") else datetime.utcnow(),
                    expires_at=expires_at,
                    is_active=node.get("is_active", True)
                ))
            
            return tokens
    
    async def update_token_memories(
        self,
        token_hash: str,
        add_memories: Optional[List[str]] = None,
        remove_memories: Optional[List[str]] = None,
        set_memories: Optional[List[str]] = None
    ) -> Optional[dict]:
        """
        Met à jour les memory_ids autorisés d'un token.
        
        Trois modes d'utilisation (mutuellement exclusifs avec set_memories) :
        - add_memories: Ajoute des mémoires à la liste existante
        - remove_memories: Retire des mémoires de la liste existante
        - set_memories: Remplace toute la liste (None = pas de changement, [] = accès à toutes)
        
        Args:
            token_hash: Hash complet du token
            add_memories: Mémoires à ajouter
            remove_memories: Mémoires à retirer
            set_memories: Remplacement complet de la liste
            
        Returns:
            Dict avec les nouvelles memory_ids, ou None si token non trouvé
        """
        async with self.graph.session() as session:
            # Récupérer le token
            result = await session.run(
                "MATCH (t:Token {hash: $hash, is_active: true}) RETURN t",
                hash=token_hash
            )
            record = await result.single()
            
            if not record:
                return None
            
            node = record["t"]
            current_memories = list(node.get("memory_ids", []))
            
            if set_memories is not None:
                # Mode remplacement total
                new_memories = set_memories
            else:
                # Mode ajout/retrait incrémental
                memory_set = set(current_memories)
                
                if add_memories:
                    memory_set.update(add_memories)
                if remove_memories:
                    memory_set -= set(remove_memories)
                
                new_memories = sorted(memory_set)
            
            # Mettre à jour dans Neo4j
            await session.run(
                """
                MATCH (t:Token {hash: $hash})
                SET t.memory_ids = $memory_ids, t.updated_at = datetime()
                """,
                hash=token_hash,
                memory_ids=new_memories
            )
            
            print(f"🔑 [Auth] Token {token_hash[:8]}... mémoires mises à jour: {new_memories}", file=sys.stderr)
            
            return {
                "token_hash": token_hash,
                "client_name": node["client_name"],
                "previous_memories": current_memories,
                "current_memories": new_memories
            }
    
    async def update_token_permissions(
        self,
        token_hash: str,
        permissions: List[str]
    ) -> Optional[dict]:
        """
        Met à jour les permissions d'un token.
        
        Permet de promouvoir un token en admin ou de le rétrograder.
        Permissions valides : 'read', 'write', 'admin'.
        
        Args:
            token_hash: Hash complet du token
            permissions: Nouvelle liste de permissions (ex: ["admin", "read", "write"])
            
        Returns:
            Dict avec les anciennes et nouvelles permissions, ou None si token non trouvé
        """
        # Valider les permissions
        valid_permissions = {"read", "write", "admin"}
        invalid = set(permissions) - valid_permissions
        if invalid:
            raise ValueError(f"Permissions invalides: {invalid}. Valides: {valid_permissions}")
        
        async with self.graph.session() as session:
            # Récupérer le token
            result = await session.run(
                "MATCH (t:Token {hash: $hash, is_active: true}) RETURN t",
                hash=token_hash
            )
            record = await result.single()
            
            if not record:
                return None
            
            node = record["t"]
            previous_permissions = list(node.get("permissions", []))
            
            # Mettre à jour dans Neo4j
            await session.run(
                """
                MATCH (t:Token {hash: $hash})
                SET t.permissions = $permissions, t.updated_at = datetime()
                """,
                hash=token_hash,
                permissions=permissions
            )
            
            action = "promu admin" if "admin" in permissions and "admin" not in previous_permissions else "mis à jour"
            print(f"🔑 [Auth] Token {token_hash[:8]}... permissions {action}: {permissions}", file=sys.stderr)
            
            return {
                "token_hash": token_hash,
                "client_name": node["client_name"],
                "previous_permissions": previous_permissions,
                "current_permissions": permissions
            }
    
    async def update_token_email(
        self,
        token_hash: str,
        email: str
    ) -> Optional[dict]:
        """Met à jour l'email d'un token."""
        async with self.graph.session() as session:
            result = await session.run(
                "MATCH (t:Token {hash: $hash, is_active: true}) RETURN t",
                hash=token_hash
            )
            record = await result.single()
            if not record:
                return None
            node = record["t"]
            previous_email = node.get("email")
            await session.run(
                "MATCH (t:Token {hash: $hash}) SET t.email = $email, t.updated_at = datetime()",
                hash=token_hash, email=email
            )
            print(f"🔑 [Auth] Token {token_hash[:8]}... email mis à jour: {email}", file=sys.stderr)
            return {
                "token_hash": token_hash,
                "client_name": node["client_name"],
                "previous_email": previous_email,
                "current_email": email
            }

    async def check_permission(
        self,
        token_info: TokenInfo,
        required_permission: str,
        memory_id: Optional[str] = None
    ) -> bool:
        """
        Vérifie si un token a la permission requise.
        
        Args:
            token_info: Informations du token
            required_permission: Permission requise ("read", "write", "admin")
            memory_id: ID de la mémoire (pour vérifier l'accès)
            
        Returns:
            True si autorisé
        """
        # Vérifier la permission
        if required_permission not in token_info.permissions:
            if "admin" not in token_info.permissions:  # admin a toutes les permissions
                return False
        
        # Vérifier l'accès à la mémoire (si spécifié)
        if memory_id and token_info.memory_ids:
            if memory_id not in token_info.memory_ids:
                return False
        
        return True


# Singleton pour usage global
_token_manager: Optional[TokenManager] = None


def get_token_manager() -> TokenManager:
    """Retourne l'instance singleton du TokenManager."""
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager()
    return _token_manager
