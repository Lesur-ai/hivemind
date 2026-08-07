# -*- coding: utf-8 -*-
"""
AuthMiddleware - Middleware ASGI pour l'authentification Bearer Token.

Vérifie le header Authorization et valide le token via TokenManager.
"""

import hmac
import json
import os
import sys
from typing import Optional

from ..config import get_settings
from .context import current_auth


AUTH_COOKIE_NAME = "graphmem_auth"


# NOTE: HostNormalizerMiddleware supprimé (migration SSE → Streamable HTTP).
# L'ancien transport SSE nécessitait une normalisation du Host header pour les reverse
# proxies (HTTP 421). Streamable HTTP n'a plus ce problème.


class AuthMiddleware:
    """
    Middleware ASGI pour l'authentification.
    
    Vérifie le header `Authorization: Bearer <token>` et valide le token.
    Pour le bootstrap initial, accepte aussi ADMIN_BOOTSTRAP_KEY.
    """
    
    def __init__(self, app, debug: bool = False):
        """
        Initialise le middleware.
        
        Args:
            app: Application ASGI à wrapper
            debug: Mode debug (logs détaillés)
        """
        self.app = app
        self.debug = debug
        self._settings = get_settings()
        self._token_manager = None
    
    @property
    def token_manager(self):
        """Lazy-load du TokenManager."""
        if self._token_manager is None:
            from .token_manager import get_token_manager
            self._token_manager = get_token_manager()
        return self._token_manager
    
    async def __call__(self, scope, receive, send):
        """Point d'entrée ASGI."""
        if scope["type"] != "http":
            # Passer directement pour WebSocket, lifespan, etc.
            await self.app(scope, receive, send)
            return
        
        path = scope.get("path", "")
        
        # Endpoints publics (pas d'auth requise)
        # Note: /api/ N'EST PLUS public — sauf login/logout web.
        public_paths = {
            "/health",
            "/healthz",
            "/ready",
            "/graph",
            "/graph/",
            "/admin",
            "/admin/",
            "/api/login",
            "/api/logout",
        }
        public_prefixes = ("/static/",)
        if path in public_paths or any(path.startswith(p) for p in public_prefixes):
            await self.app(scope, receive, send)
            return
        
        # Requêtes internes (localhost) : pas d'auth pour MCP Streamable HTTP
        # MAIS les endpoints /api/ exigent toujours un token (pour le client web)
        # Sécurité v2.1.0 : bypass désactivable via LOCALHOST_AUTH_BYPASS=false
        # P7-4 (ADR-0019): default OFF — the embedded GM fails closed; the
        # localhost MCP bypass is an opt-in escape hatch, never the default.
        localhost_bypass = os.getenv("LOCALHOST_AUTH_BYPASS", "false").lower() == "true"
        client = scope.get("client", ("", 0))
        client_ip = client[0] if client else ""
        if localhost_bypass and client_ip in ("127.0.0.1", "::1") and not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        
        token = self._extract_token(scope)

        if not token:
            if self.debug:
                print(f"❌ [Auth] Header Authorization manquant pour {path}", file=sys.stderr)
            await self._send_error(send, 401, "Authorization header required")
            return
        
        # Vérifier si c'est la clé bootstrap admin
        # Sécurité v2.1.0 : comparaison constant-time (anti timing attack)
        bootstrap_key = self._settings.admin_bootstrap_key
        if bootstrap_key and hmac.compare_digest(token, bootstrap_key):
            if self.debug:
                print(f"✅ [Auth] Authentification avec clé bootstrap admin", file=sys.stderr)
            # Ajouter info d'auth au scope
            scope["auth"] = {
                "type": "bootstrap",
                "client_name": "admin",
                "permissions": ["admin", "read", "write"],
                "memory_ids": []  # Accès à toutes
            }
            # Propager le contexte d'auth pour les outils MCP
            # P7-4: reset the contextvar after the request (avoid cross-session bleed).
            _tok = current_auth.set(scope["auth"])
            try:
                await self.app(scope, receive, send)
            finally:
                current_auth.reset(_tok)
            return
        
        # Valider le token client
        try:
            # P7-4 (ADR-0019, Model B): validate against Hivemind's S3 token
            # store, NOT the local Neo4j token_manager — one token system
            # end-to-end. token_manager stays vendored-but-dormant for admin CRUD.
            from .s3_token_validator import get_s3_token_validator
            token_info = await get_s3_token_validator().validate_token(token)
            
            if not token_info:
                if self.debug:
                    print(f"❌ [Auth] Token invalide ou expiré", file=sys.stderr)
                await self._send_error(send, 401, "Invalid or expired token")
                return
            
            if self.debug:
                print(f"✅ [Auth] Client '{token_info.client_name}' authentifié", file=sys.stderr)
            
            # Ajouter info d'auth au scope
            scope["auth"] = {
                "type": "token",
                "client_name": token_info.client_name,
                "permissions": token_info.permissions,
                "memory_ids": token_info.memory_ids,
                "token_hash": token_info.token_hash
            }
            
            # Propager le contexte d'auth pour les outils MCP
            # P7-4: reset the contextvar after the request (avoid cross-session bleed).
            _tok = current_auth.set(scope["auth"])
            try:
                await self.app(scope, receive, send)
            finally:
                current_auth.reset(_tok)

        except Exception as e:
            if self.debug:
                print(f"❌ [Auth] Erreur validation: {e}", file=sys.stderr)
            await self._send_error(send, 500, "Authentication error")

    def _extract_token(self, scope) -> Optional[str]:
        """Extrait un token depuis Bearer, cookie HttpOnly ou query string legacy."""
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8")
        if auth_header:
            if auth_header.startswith("Bearer "):
                return auth_header[7:]
            return None

        cookie_header = headers.get(b"cookie", b"").decode("utf-8", errors="ignore")
        if cookie_header:
            for raw in cookie_header.split(";"):
                pair = raw.strip().split("=", 1)
                if len(pair) == 2 and pair[0].strip() == AUTH_COOKIE_NAME:
                    value = pair[1].strip()
                    if value:
                        return value

        qs = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        for param in qs.split("&"):
            if param.startswith("token="):
                return param[6:]
        return None
    
    async def _send_error(self, send, status: int, message: str):
        """Envoie une réponse d'erreur HTTP."""
        body = json.dumps({"error": message}).encode()
        
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


class LoggingMiddleware:
    """
    Middleware ASGI pour le logging des requêtes (mode debug).
    """
    
    def __init__(self, app, debug: bool = False):
        self.app = app
        self.debug = debug
    
    async def __call__(self, scope, receive, send):
        if not self.debug or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        path = scope.get("path", "")
        method = scope.get("method", "?")
        query = scope.get("query_string", b"").decode()
        
        full_path = f"{path}?{query}" if query else path
        
        # Sécurité v2.1.0 : masquer les headers sensibles dans les logs (M7)
        headers = dict(scope.get("headers", []))
        auth_h = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        if auth_h:
            masked = f"Bearer ***...{auth_h[-4:]}" if len(auth_h) > 12 else "***"
            print(f"📥 [HTTP] {method} {full_path} [Auth: {masked}]", file=sys.stderr)
        else:
            print(f"📥 [HTTP] {method} {full_path}", file=sys.stderr)
        
        # Wrapper pour logger la réponse
        status_code = [None]
        
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code[0] = message.get("status")
            await send(message)
        
        await self.app(scope, receive, send_wrapper)
        
        if status_code[0]:
            emoji = "✅" if status_code[0] < 400 else "❌"
            print(f"{emoji} [HTTP] {method} {path} -> {status_code[0]}", file=sys.stderr)


class StaticFilesMiddleware:
    """
    Middleware ASGI pour servir les fichiers statiques et l'API REST simple.
    
    Routes:
    - GET /graph -> Page de visualisation
    - GET /admin -> Console d'administration
    - POST /api/login -> Login web avec cookie HttpOnly
    - POST /api/logout -> Logout web
    - POST /api/tool -> Proxy d'outils MCP pour la console admin
    - GET /api/memories -> Liste des mémoires (JSON)
    - GET /api/graph/<memory_id> -> Graphe complet (JSON)
    """
    
    def __init__(self, app):
        self.app = app
        self._static_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "static"
        )
        self._graph_service = None
        self._extractor_service = None
    
    @property
    def graph_service(self):
        """Lazy-load GraphService."""
        if self._graph_service is None:
            from ..core.graph import get_graph_service
            self._graph_service = get_graph_service()
        return self._graph_service
    
    @property
    def extractor_service(self):
        """Lazy-load ExtractorService."""
        if self._extractor_service is None:
            from ..core.extractor import get_extractor_service
            self._extractor_service = get_extractor_service()
        return self._extractor_service
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        
        # Page de visualisation
        if path == "/graph" or path == "/graph/":
            await self._serve_file(send, "graph.html", "text/html")
            return

        # Console admin
        if path == "/admin" or path == "/admin/":
            await self._serve_file(send, "admin.html", "text/html; charset=utf-8")
            return
        
        # Fichiers statiques (CSS, JS)
        if path.startswith("/static/"):
            rel_path = path[len("/static/"):]
            # Sécurité : pas de traversée de répertoire
            if ".." not in rel_path and rel_path:
                ct = self._guess_content_type(rel_path)
                await self._serve_file(send, rel_path, ct)
                return
        
        # Health check
        if path in ("/health", "/healthz", "/ready"):
            await self._api_health(send, require_readiness=path == "/ready")
            return

        # API REST - Login admin web
        if path == "/api/login" and method == "POST":
            try:
                body = await self._read_body_limited(receive, max_bytes=8192)
            except ValueError as e:
                await self._send_json(send, {"status": "error", "message": str(e)}, 413)
                return
            await self._api_login(scope, send, body)
            return

        # API REST - Logout admin web
        if path == "/api/logout" and method == "POST":
            await self._api_logout(send)
            return

        # API REST - Proxy outils MCP (console admin)
        if path == "/api/tool" and method == "POST":
            try:
                max_body = max(2 * 1024 * 1024, int(get_settings().max_document_size_bytes * 1.5))
                body = await self._read_body_limited(receive, max_bytes=max_body)
            except ValueError as e:
                await self._send_json(send, {"status": "error", "message": str(e)}, 413)
                return
            await self._api_tool(send, body)
            return
        
        # API REST - Liste des mémoires
        if path == "/api/memories" and method == "GET":
            await self._api_memories(send)
            return
        
        # API REST - Graphe d'une mémoire
        if path.startswith("/api/graph/") and method == "GET":
            memory_id = path[len("/api/graph/"):]
            if memory_id:
                await self._api_graph(send, memory_id)
                return
        
        # API REST - Question/Réponse (POST)
        if path == "/api/ask" and method == "POST":
            body = await self._read_body(receive)
            await self._api_ask(send, body)
            return
        
        # API REST - Query structuré (POST) — données brutes sans LLM
        if path == "/api/query" and method == "POST":
            body = await self._read_body(receive)
            await self._api_query(send, body)
            return
        
        # Passer au handler suivant
        await self.app(scope, receive, send)
    
    async def _read_body(self, receive) -> bytes:
        """Lit le corps complet d'une requête ASGI."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        return body

    async def _read_body_limited(self, receive, max_bytes: int) -> bytes:
        """Lit le corps d'une requête avec limite anti-DoS."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > max_bytes:
                raise ValueError("Request body too large")
            if not message.get("more_body", False):
                break
        return body

    async def _api_login(self, scope, send, body: bytes):
        """Valide un token et pose un cookie HttpOnly pour l'interface web."""
        try:
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                await self._send_json(send, {"status": "error", "message": "Body JSON invalide"}, 400)
                return

            token = (payload.get("token") or "").strip()
            if not token:
                await self._send_json(send, {"status": "error", "message": "Champ 'token' requis"}, 400)
                return

            token_info = None
            bootstrap_key = get_settings().admin_bootstrap_key
            if bootstrap_key and hmac.compare_digest(token, bootstrap_key):
                token_info = {
                    "type": "bootstrap",
                    "client_name": "admin",
                    "permissions": ["admin", "read", "write"],
                    "memory_ids": [],
                }
            else:
                try:
                    # P7-4 (Model B): web login validates against Hivemind's S3
                    # token store, not the local Neo4j token_manager.
                    from .s3_token_validator import get_s3_token_validator
                    token_obj = await get_s3_token_validator().validate_token(token)
                    if token_obj:
                        token_info = {
                            "type": "token",
                            "client_name": token_obj.client_name,
                            "permissions": token_obj.permissions,
                            "memory_ids": token_obj.memory_ids,
                            "token_hash": token_obj.token_hash,
                        }
                except Exception:
                    token_info = None

            if token_info is None:
                await self._send_json(send, {"status": "error", "message": "Token invalide"}, 401)
                return

            headers = dict(scope.get("headers", []))
            forwarded_proto = headers.get(b"x-forwarded-proto", b"").decode().lower()
            scheme = scope.get("scheme", "http").lower()
            is_https = scheme == "https" or forwarded_proto == "https"
            cookie_parts = [
                f"{AUTH_COOKIE_NAME}={token}",
                "Path=/",
                "HttpOnly",
                "SameSite=Strict",
            ]
            if is_https:
                cookie_parts.append("Secure")

            response = {
                "status": "ok",
                "client_name": token_info.get("client_name", "?"),
                "permissions": token_info.get("permissions", []),
                "memory_ids": token_info.get("memory_ids", []),
                "auth_type": token_info.get("type", "?"),
            }
            response_body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(response_body)).encode()),
                    (b"set-cookie", "; ".join(cookie_parts).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": response_body})
        except ValueError as e:
            await self._send_json(send, {"status": "error", "message": str(e)}, 413)
        except Exception as e:
            await self._send_json(send, {"status": "error", "message": str(e)}, 500)

    def _get_token_manager(self):
        """Lazy-load TokenManager pour les handlers statiques."""
        if not hasattr(self, "_token_manager") or self._token_manager is None:
            from .token_manager import get_token_manager
            self._token_manager = get_token_manager()
        return self._token_manager

    async def _api_logout(self, send):
        """Supprime le cookie d'authentification web."""
        expired_cookie = f"{AUTH_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        body = json.dumps({"status": "ok"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"set-cookie", expired_cookie.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _api_tool(self, send, body: bytes):
        """Proxy REST vers les outils MCP, utilisé par /admin."""
        try:
            # La console admin sert à gérer : read-only peut utiliser /graph.
            from .context import check_write_permission
            perm_err = check_write_permission()
            if perm_err:
                await self._send_json(send, perm_err, 403)
                return

            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                await self._send_json(send, {"status": "error", "message": "Body JSON invalide"}, 400)
                return

            tool_name = (payload.get("tool") or "").strip()
            arguments = payload.get("arguments") or {}
            if not tool_name:
                await self._send_json(send, {"status": "error", "message": "Champ 'tool' requis"}, 400)
                return
            if not isinstance(arguments, dict):
                await self._send_json(send, {"status": "error", "message": "'arguments' doit être un objet"}, 400)
                return

            result = await self._call_tool_direct(tool_name, arguments)
            await self._send_json(send, result)
        except ValueError as e:
            await self._send_json(send, {"status": "error", "message": str(e)}, 413)
        except Exception as e:
            print(f"❌ [/api/tool] {e}", file=sys.stderr)
            await self._send_json(send, {"status": "error", "message": "Erreur interne /api/tool"}, 500)

    async def _call_tool_direct(self, tool_name: str, arguments: dict) -> dict:
        """Appelle directement un outil enregistré dans FastMCP."""
        from ..server import mcp

        tool_manager = mcp._tool_manager
        tools = getattr(tool_manager, "_tools", {})
        if tool_name not in tools:
            return {"status": "error", "message": f"Outil inconnu: {tool_name}"}

        tool_obj = tools[tool_name]
        fn = None
        for attr in ("fn", "func", "handler", "_fn", "run", "callback"):
            candidate = getattr(tool_obj, attr, None)
            if candidate and callable(candidate):
                fn = candidate
                break

        if fn is None:
            return {"status": "error", "message": f"Outil {tool_name}: handler introuvable"}

        result = await fn(**arguments)
        return result if isinstance(result, dict) else {"status": "ok", "data": result}
    
    def _read_version(self) -> str:
        """Lit la version depuis le fichier VERSION."""
        try:
            # auth/middleware.py → auth → mcp_memory → src → project root
            version_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "VERSION"
            )
            if os.path.exists(version_path):
                with open(version_path) as f:
                    return f.read().strip()
        except Exception:
            pass
        return "unknown"

    async def _api_health(self, send, *, require_readiness: bool = False):
        """Return stable liveness or the value-free mutation readiness gate."""
        version = self._read_version()
        try:
            ready = True
            if require_readiness:
                from ..core.maintenance import get_maintenance_coordinator

                schema = self.graph_service.document_schema_status()
                maintenance = get_maintenance_coordinator().health_status()
                ready = (
                    schema.get("status") == "ok"
                    and maintenance.get("status") == "ok"
                )
            data = {
                "status": "healthy" if ready else "error",
                "service": "graph-memory",
                "version": version,
                "transport": "streamable-http",
            }
            await self._send_json(send, data, 200 if ready else 503)
        except Exception:
            await self._send_json(send, {
                "status": "error",
                "service": "graph-memory",
                "version": version,
                "transport": "streamable-http",
            }, 503 if require_readiness else 500)
    
    async def _api_memories(self, send):
        """Retourne la liste des mémoires en JSON."""
        import json
        try:
            memories = await self.graph_service.list_memories()
            data = {
                "status": "ok",
                "count": len(memories),
                "memories": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "description": m.description,
                        "ontology": m.ontology,
                        "ontology_uri": m.ontology_uri,
                        "created_at": m.created_at.isoformat() if m.created_at else None
                    }
                    for m in memories
                ]
            }
            await self._send_json(send, data)
        except Exception as e:
            await self._send_json(send, {"status": "error", "message": str(e)}, 500)
    
    async def _api_graph(self, send, memory_id: str):
        """Retourne le graphe complet d'une mémoire en JSON."""
        import json
        try:
            graph_data = await self.graph_service.get_full_graph(memory_id)
            data = {
                "status": "ok",
                "memory_id": memory_id,
                "node_count": len(graph_data["nodes"]),
                "edge_count": len(graph_data["edges"]),
                "document_count": len(graph_data["documents"]),
                "nodes": graph_data["nodes"],
                "edges": graph_data["edges"],
                "documents": graph_data["documents"]
            }
            await self._send_json(send, data)
        except Exception as e:
            await self._send_json(send, {"status": "error", "message": str(e)}, 500)
    
    async def _api_ask(self, send, body: bytes):
        """
        Traite une question sur une mémoire et retourne la réponse.
        
        Délègue à question_answer() de server.py (source unique de logique).
        Body JSON: {memory_id, question, limit?}
        Retourne: {status, answer, entities, source_documents}
        """
        import json
        try:
            payload = json.loads(body.decode('utf-8'))
            memory_id = payload.get("memory_id")
            question = payload.get("question")
            limit = payload.get("limit", 10)
            
            if not memory_id or not question:
                await self._send_json(send, {
                    "status": "error",
                    "message": "memory_id et question sont requis"
                }, 400)
                return
            
            print(f"💬 [ASK] {memory_id}: {question}", file=sys.stderr)
            
            # Appel direct à la fonction MCP (source unique de logique)
            from ..server import question_answer
            result = await question_answer(memory_id, question, limit)
            
            # Retirer context_used de la réponse API (pas utile pour le front)
            result.pop("context_used", None)
            
            await self._send_json(send, result)
            
        except json.JSONDecodeError:
            await self._send_json(send, {
                "status": "error",
                "message": "JSON invalide dans le body"
            }, 400)
        except Exception as e:
            print(f"❌ [ASK] Erreur: {e}", file=sys.stderr)
            await self._send_json(send, {
                "status": "error",
                "message": str(e)
            }, 500)
    
    async def _api_query(self, send, body: bytes):
        """
        Interroge une mémoire et retourne les données structurées (sans LLM).
        
        Délègue à memory_query() de server.py (source unique de logique).
        Body JSON: {memory_id, query, limit?}
        Retourne: {status, entities, rag_chunks, source_documents, stats}
        """
        import json
        try:
            payload = json.loads(body.decode('utf-8'))
            memory_id = payload.get("memory_id")
            query = payload.get("query")
            limit = payload.get("limit", 10)
            
            if not memory_id or not query:
                await self._send_json(send, {
                    "status": "error",
                    "message": "memory_id et query sont requis"
                }, 400)
                return
            
            print(f"📊 [Query] {memory_id}: {query}", file=sys.stderr)
            
            from ..server import memory_query
            result = await memory_query(memory_id, query, limit)
            
            await self._send_json(send, result)
            
        except json.JSONDecodeError:
            await self._send_json(send, {
                "status": "error",
                "message": "JSON invalide dans le body"
            }, 400)
        except Exception as e:
            print(f"❌ [Query] Erreur: {e}", file=sys.stderr)
            await self._send_json(send, {
                "status": "error",
                "message": str(e)
            }, 500)
    
    async def _send_json(self, send, data: dict, status: int = 200):
        """Envoie une réponse JSON."""
        import json
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                # P7-4 (ADR-0019): wildcard CORS removed — the admin API is
                # same-origin behind the WAF; no cross-origin access is granted.
            ],
        })
        await send({"type": "http.response.body", "body": body})
    
    async def _serve_file(self, send, filename: str, content_type: str):
        """Sert un fichier statique."""
        filename = filename.split("?", 1)[0]
        filepath = os.path.join(self._static_dir, filename)
        
        if not os.path.exists(filepath):
            await self._send_404(send, f"File not found: {filename}")
            return
        
        try:
            with open(filepath, "rb") as f:
                body = f.read()
            
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", content_type.encode()),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store, no-cache, must-revalidate, max-age=0"),
                    (b"pragma", b"no-cache"),
                    (b"expires", b"0"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
        except Exception as e:
            await self._send_500(send, str(e))
    
    async def _send_404(self, send, message: str):
        """Envoie une erreur 404."""
        body = f"<h1>404 Not Found</h1><p>{message}</p>".encode()
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"text/html"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
    
    async def _send_500(self, send, message: str):
        """Envoie une erreur 500."""
        body = f"<h1>500 Internal Server Error</h1><p>{message}</p>".encode()
        await send({
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"text/html"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
    
    @staticmethod
    def _guess_content_type(filename: str) -> str:
        """Devine le content-type à partir de l'extension."""
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        return {
            'html': 'text/html; charset=utf-8',
            'css': 'text/css; charset=utf-8',
            'js': 'application/javascript; charset=utf-8',
            'json': 'application/json',
            'png': 'image/png',
            'svg': 'image/svg+xml',
            'ico': 'image/x-icon',
        }.get(ext, 'application/octet-stream')
