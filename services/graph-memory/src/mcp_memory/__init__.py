# -*- coding: utf-8 -*-
"""
MCP Memory Service
==================

Service de mémoire à long terme basé sur Knowledge Graph pour agents IA.
Exposé via le protocole MCP (Model Context Protocol) sur HTTP/SSE.

Architecture:
- Neo4j pour le stockage graphe (entités, relations)
- S3 pour le stockage des documents originaux
- LLMaaS pour l'extraction d'entités/relations

Usage:
    python -m src.mcp_memory.server --port 8002
"""

__version__ = "3.2.0"
__author__ = "Cloud Temple"
